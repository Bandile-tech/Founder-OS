"""
CRM — clients, contacts, deals, projects, payments, recurring revenue and communications
(Phase 5C).

Primitives: find_client, find_deal, find_project, find_contact, client_health,
            client_extras, client_detail, add_contact, log_communication, create_deal,
            update_deal, record_payment, mark_paid, add_recurring, generate_due,
            refresh_statuses, reconcile, refresh_health, housekeeping, summary
Endpoints:  GET /clients/{id}, POST /clients/{id}/contacts, PATCH /contacts/{id},
            GET/POST /deals, PATCH /deals/{id}, GET/POST /communications,
            GET/POST /payments, POST /payments/{id}/paid, POST /payments/{id}/cancel,
            GET/POST /recurring, PATCH /recurring/{id}, GET /crm/summary
            (GET /clients and PATCH /clients/{id} stay in main.py and are extended there)

A paid payment always has exactly one linked ``revenue`` row, so the revenue totals and
the business progress bar keep reading ``revenue`` and nothing downstream changes.

Facts arrive in conversation or by one tap; every write is in the change log. The existing
``clients`` and ``revenue`` tables are extended, never replaced.
"""

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

import business_common
import change_log
import models
from database import get_db

router = APIRouter()

CLIENT_STATUSES = ("prospect", "active", "paused", "closed", "lost")
DEAL_STAGES = ("lead", "proposal", "negotiation", "won", "lost")
STAGE_PROBABILITY = {"lead": 10, "proposal": 40, "negotiation": 70, "won": 100, "lost": 0}
CHANNELS = ("whatsapp", "email", "call", "meeting", "linkedin", "other")
OPEN_PAYMENT = ("due", "overdue")
MONTHS_PER_INTERVAL = {"monthly": 1, "quarterly": 3, "annual": 12}

RED_OVERDUE_DAYS = 14
RED_SILENCE_DAYS = 45
AMBER_SILENCE_DAYS = 21
AMBER_NEGOTIATION_DAYS = 30


# ── Small helpers ────────────────────────────────────────────

def tag_list(tags: str | None) -> list:
    return [t.strip() for t in (tags or "").split(",") if t.strip()]


def _currency(value, default: str = "ZMW") -> str:
    import main
    return main._normalise_currency(value) if value else default


def _usd(amount, currency) -> float:
    import main
    return round(main._to_usd(float(amount or 0), currency), 2)


def _totals(rows, amount_of=lambda r: r.amount) -> dict:
    """{by_currency, total_usd} for a list of money rows."""
    by_currency, total = {}, 0.0
    for row in rows:
        amount = float(amount_of(row) or 0)
        cur = (row.currency or "ZMW").upper()
        by_currency[cur] = round(by_currency.get(cur, 0.0) + amount, 2)
        total += _usd(amount, cur)
    return {"by_currency": by_currency, "total_usd": round(total, 2)}


def monthly_amount(rec: models.RecurringRevenue) -> float:
    return float(rec.amount or 0) / MONTHS_PER_INTERVAL.get(rec.interval or "monthly", 1)


def _client_name(db: Session, client_id) -> str | None:
    if client_id is None:
        return None
    c = db.query(models.Client).filter(models.Client.id == client_id).first()
    return c.name if c else None


# ── Lookups ──────────────────────────────────────────────────

def find_client(db: Session, fragment):
    """(client | None, candidates) — an id, or a fuzzy match over name and company."""
    if isinstance(fragment, int) or (isinstance(fragment, str) and fragment.strip().isdigit()):
        c = db.query(models.Client).filter(models.Client.id == int(fragment)).first()
        return c, ([(1.0, c)] if c else [])
    rows = db.query(models.Client).all()
    return business_common.fuzzy_pick(
        rows, lambda c: [c.name, c.company, f"{c.name} {c.company or ''}"], str(fragment or ""))


def find_deal(db: Session, fragment, client: models.Client | None = None):
    q = db.query(models.Deal)
    if client is not None:
        q = q.filter(models.Deal.client_id == client.id)
    if isinstance(fragment, int) or (isinstance(fragment, str) and fragment.strip().isdigit()):
        d = q.filter(models.Deal.id == int(fragment)).first()
        return d, ([(1.0, d)] if d else [])
    names = {c.id: c.name for c in db.query(models.Client).all()}
    return business_common.fuzzy_pick(
        q.all(), lambda d: [d.title, f"{names.get(d.client_id, '')} {d.title}", names.get(d.client_id)],
        str(fragment or ""))


def find_project(db: Session, fragment):
    if isinstance(fragment, int) or (isinstance(fragment, str) and fragment.strip().isdigit()):
        p = db.query(models.Project).filter(models.Project.id == int(fragment)).first()
        return p, ([(1.0, p)] if p else [])
    names = {c.id: c.name for c in db.query(models.Client).all()}
    return business_common.fuzzy_pick(
        db.query(models.Project).all(),
        lambda p: [p.title, f"{names.get(p.client_id, '')} {p.title}"], str(fragment or ""))


def find_contact(db: Session, fragment):
    return business_common.fuzzy_pick(db.query(models.Contact).all(), lambda c: c.name, str(fragment or ""))


# ── Serialisation ────────────────────────────────────────────

def serialize_contact(c: models.Contact) -> dict:
    return {"id": c.id, "client_id": c.client_id, "name": c.name, "role": c.role, "email": c.email,
            "phone": c.phone, "whatsapp": c.whatsapp, "is_primary": bool(c.is_primary),
            "notes": c.notes, "created_at": c.created_at, "updated_at": c.updated_at}


def serialize_deal(db: Session, d: models.Deal) -> dict:
    return {"id": d.id, "client_id": d.client_id, "client_name": _client_name(db, d.client_id),
            "title": d.title, "stage": d.stage, "amount": d.amount, "currency": d.currency,
            "amount_usd": _usd(d.amount, d.currency) if d.amount is not None else None,
            "probability": d.probability, "expected_close": d.expected_close, "won_at": d.won_at,
            "lost_reason": d.lost_reason, "notes": d.notes, "project_id": d.project_id,
            "created_at": d.created_at, "updated_at": d.updated_at}


def serialize_project(db: Session, p: models.Project) -> dict:
    return {"id": p.id, "title": p.title, "kind": p.kind, "client_id": p.client_id,
            "client_name": _client_name(db, p.client_id), "deal_id": p.deal_id, "status": p.status,
            "priority": p.priority, "expected_value": p.expected_value, "currency": p.currency,
            "expected_value_usd": (_usd(p.expected_value, p.currency) if p.expected_value is not None else None),
            "effort_hours_week": p.effort_hours_week, "start_date": p.start_date,
            "due_date": p.due_date, "delivered_at": p.delivered_at, "next_action": p.next_action,
            "blocked_reason": p.blocked_reason, "last_activity_at": p.last_activity_at,
            "health": p.health, "notes": p.notes, "created_at": p.created_at,
            "updated_at": p.updated_at}


def serialize_payment(db: Session, p: models.Payment) -> dict:
    return {"id": p.id, "client_id": p.client_id, "client_name": _client_name(db, p.client_id),
            "deal_id": p.deal_id, "project_id": p.project_id, "title": p.title, "amount": p.amount,
            "currency": p.currency, "amount_usd": _usd(p.amount, p.currency), "status": p.status,
            "due_date": p.due_date, "paid_at": p.paid_at, "revenue_id": p.revenue_id,
            "recurring_id": p.recurring_id, "period_start": p.period_start, "notes": p.notes,
            "created_at": p.created_at, "updated_at": p.updated_at}


def serialize_recurring(db: Session, r: models.RecurringRevenue) -> dict:
    return {"id": r.id, "client_id": r.client_id, "client_name": _client_name(db, r.client_id),
            "title": r.title, "amount": r.amount, "currency": r.currency, "interval": r.interval,
            "day_of_month": r.day_of_month, "next_due": r.next_due, "active": bool(r.active),
            "ended_at": r.ended_at, "monthly_amount": round(monthly_amount(r), 2),
            "created_at": r.created_at, "updated_at": r.updated_at}


def serialize_communication(c: models.Communication) -> dict:
    return {"id": c.id, "client_id": c.client_id, "contact_id": c.contact_id, "lead_id": c.lead_id,
            "channel": c.channel, "direction": c.direction, "summary": c.summary,
            "occurred_at": c.occurred_at, "outreach_message_id": c.outreach_message_id,
            "source": c.source, "created_at": c.created_at}


# ── Client state ─────────────────────────────────────────────

def last_contact_at(db: Session, client: models.Client):
    row = db.query(models.Communication).filter(
        models.Communication.client_id == client.id,
    ).order_by(models.Communication.occurred_at.desc()).first()
    return row.occurred_at if row else None


def client_health(db: Session, client: models.Client, today: date | None = None) -> str:
    """green | amber | red.

    red:   a payment more than 14 days overdue, or an active project that is blocked, or an
           active client with no communication in 45 days
    amber: any overdue payment, or an active client with no communication in 21 days, or a
           deal in negotiation for more than 30 days
    Silence is measured from the last communication, or from when the client was created."""
    today = today or date.today()
    now = datetime.utcnow()
    overdue_days = [
        (today - p.due_date).days
        for p in db.query(models.Payment).filter(
            models.Payment.client_id == client.id, models.Payment.status.in_(OPEN_PAYMENT)).all()
        if p.due_date is not None and p.due_date < today
    ]
    blocked = any(
        (p.blocked_reason or "").strip()
        for p in db.query(models.Project).filter(
            models.Project.client_id == client.id, models.Project.status == "active").all()
    )
    silence = None
    if client.status == "active":
        last = last_contact_at(db, client) or client.created_at
        silence = (now - last).days if last else None
    stale_negotiation = any(
        d.updated_at is not None and (now - d.updated_at).days > AMBER_NEGOTIATION_DAYS
        for d in db.query(models.Deal).filter(
            models.Deal.client_id == client.id, models.Deal.stage == "negotiation").all()
    )
    if (any(d > RED_OVERDUE_DAYS for d in overdue_days) or blocked
            or (silence is not None and silence >= RED_SILENCE_DAYS)):
        return "red"
    if overdue_days or stale_negotiation or (silence is not None and silence >= AMBER_SILENCE_DAYS):
        return "amber"
    return "green"


def client_extras(db: Session, client: models.Client) -> dict:
    """The CRM fields added to every client in ``GET /clients``."""
    open_payments = db.query(models.Payment).filter(
        models.Payment.client_id == client.id, models.Payment.status.in_(OPEN_PAYMENT)).all()
    recurring = db.query(models.RecurringRevenue).filter(
        models.RecurringRevenue.client_id == client.id, models.RecurringRevenue.active == True).all()   # noqa: E712
    return {
        "currency": client.currency or models.default_currency(),
        "health": client_health(db, client),
        "tags": tag_list(client.tags),
        "lead_id": client.lead_id,
        "contacts_count": db.query(models.Contact).filter(models.Contact.client_id == client.id).count(),
        "open_deals": db.query(models.Deal).filter(
            models.Deal.client_id == client.id, models.Deal.stage.notin_(("won", "lost"))).count(),
        "outstanding": _totals(open_payments),
        "mrr": _totals(recurring, amount_of=monthly_amount),
        "last_contact_at": last_contact_at(db, client),
    }


def client_detail(db: Session, client: models.Client) -> dict:
    cid = client.id
    return {
        "id": cid, "name": client.name, "company": client.company, "status": client.status,
        "value": client.value, "service": client.service, "notes": client.notes,
        "created_at": client.created_at, "updated_at": client.updated_at,
        **client_extras(db, client),
        "contacts": [serialize_contact(c) for c in db.query(models.Contact).filter(
            models.Contact.client_id == cid).order_by(models.Contact.is_primary.desc(), models.Contact.id).all()],
        "deals": [serialize_deal(db, d) for d in db.query(models.Deal).filter(
            models.Deal.client_id == cid).order_by(models.Deal.id.desc()).all()],
        "projects": [serialize_project(db, p) for p in db.query(models.Project).filter(
            models.Project.client_id == cid).order_by(models.Project.id.desc()).all()],
        "payments": [serialize_payment(db, p) for p in db.query(models.Payment).filter(
            models.Payment.client_id == cid).order_by(models.Payment.id.desc()).all()],
        "recurring": [serialize_recurring(db, r) for r in db.query(models.RecurringRevenue).filter(
            models.RecurringRevenue.client_id == cid).order_by(models.RecurringRevenue.id.desc()).all()],
        "communications": [serialize_communication(c) for c in db.query(models.Communication).filter(
            models.Communication.client_id == cid,
        ).order_by(models.Communication.occurred_at.desc(), models.Communication.id.desc()).limit(50).all()],
    }


def touch_projects(db: Session, client_id: int | None, when: datetime | None = None) -> None:
    """Bump ``last_activity_at`` on the client's active projects (a communication or a
    payment counts as activity). Not logged: it is bookkeeping, not a statement."""
    if client_id is None:
        return
    when = when or datetime.utcnow()
    for p in db.query(models.Project).filter(
            models.Project.client_id == client_id, models.Project.status == "active").all():
        if p.last_activity_at is None or p.last_activity_at < when:
            p.last_activity_at = when


# ── Write primitives (the caller commits) ────────────────────

def add_contact(db: Session, client: models.Client, *, name: str, role: str | None = None,
                email: str | None = None, phone: str | None = None, whatsapp: str | None = None,
                is_primary: bool = False, notes: str | None = None) -> dict:
    import leads
    name = (name or "").strip()
    if not name:
        raise ValueError("a contact needs a name")
    changes = []
    if is_primary:
        for other in db.query(models.Contact).filter(
                models.Contact.client_id == client.id, models.Contact.is_primary == True).all():   # noqa: E712
            before = change_log.snapshot(other)
            other.is_primary = False
            changes.append(change_log.record_update(db, other, before, f"{other.name} is no longer the primary contact"))
    number = leads.normalise_phone(phone)
    contact = models.Contact(
        client_id=client.id, name=name, role=(role or "").strip() or None,
        email=leads.normalise_email(email), phone=number,
        whatsapp=leads.normalise_phone(whatsapp) or number, is_primary=bool(is_primary),
        notes=(notes or "").strip() or None, created_at=datetime.utcnow(), updated_at=datetime.utcnow())
    db.add(contact)
    change = change_log.record_create(db, contact, f"Added contact {name[:50]} at {client.name[:30]}")
    return {**serialize_contact(contact), "client_name": client.name, "change": change,
            "changes": [c for c in changes + [change] if c]}


def log_communication(db: Session, *, client: models.Client | None = None, lead=None,
                      contact: models.Contact | None = None, channel: str = "whatsapp",
                      direction: str = "out", summary: str, occurred_at: datetime | None = None,
                      source: str = "conversation", outreach_message_id: int | None = None) -> dict:
    """Record one conversation. At least one of client, lead or contact is required."""
    summary = (summary or "").strip()
    if not summary:
        raise ValueError("a communication needs a summary")
    if client is None and contact is not None:
        client = db.query(models.Client).filter(models.Client.id == contact.client_id).first()
    if client is None and lead is None:
        raise ValueError("a communication needs a client, a contact or a lead")
    channel = channel if channel in CHANNELS else "other"
    direction = "in" if direction == "in" else "out"
    when = occurred_at or datetime.utcnow()
    row = models.Communication(
        client_id=client.id if client else None, contact_id=contact.id if contact else None,
        lead_id=lead.id if lead is not None else None, channel=channel, direction=direction,
        summary=summary, occurred_at=when, outreach_message_id=outreach_message_id,
        source=source, created_at=datetime.utcnow())
    db.add(row)
    who = client.name if client else lead.name
    change = change_log.record_create(db, row, f"Logged {channel} with {who[:40]}: {summary[:40]}")
    if client is not None:
        touch_projects(db, client.id, when)
    try:
        import retrieval
        retrieval.index_one(db, "crm", row.id, summary)
    except Exception as exc:                # noqa: BLE001 — search is optional, the record is not
        print(f"[crm] communication index failed: {exc}")
    return {**serialize_communication(row), "with": who, "change": change}


def create_deal(db: Session, client: models.Client, title: str, amount: float | None = None,
                currency: str | None = None, stage: str = "lead",
                expected_close: date | None = None) -> dict:
    title = (title or "").strip()
    if not title:
        raise ValueError("a deal needs a title")
    if stage not in DEAL_STAGES:
        raise ValueError(f"stage must be one of: {', '.join(DEAL_STAGES)}")
    deal = models.Deal(
        client_id=client.id, title=title, stage="lead",
        amount=float(amount) if amount is not None else None,
        currency=_currency(currency, client.currency or models.default_currency()),
        probability=STAGE_PROBABILITY["lead"], expected_close=expected_close,
        created_at=datetime.utcnow(), updated_at=datetime.utcnow())
    db.add(deal)
    change = change_log.record_create(db, deal, f"New deal: {title[:50]} ({client.name[:30]})")
    result = {"change": change, "changes": [change]}
    if stage != "lead":
        moved = update_deal(db, deal, stage=stage)
        result["changes"] += moved["changes"]
        if moved.get("project"):
            result["project"] = moved["project"]
    return {**serialize_deal(db, deal), **result}


def update_deal(db: Session, deal: models.Deal, *, stage: str | None = None,
                amount: float | None = None, currency: str | None = None,
                probability: int | None = None, expected_close: date | None = None,
                lost_reason: str | None = None, notes: str | None = None) -> dict:
    """Change a deal. A stage change resets the probability to the stage default unless one
    is given. Winning a deal stamps ``won_at`` and creates its project once."""
    if stage is not None and stage not in DEAL_STAGES:
        raise ValueError(f"stage must be one of: {', '.join(DEAL_STAGES)}")
    before = change_log.snapshot(deal)
    changes, project = [], None
    if amount is not None:
        deal.amount = float(amount)
    if currency:
        deal.currency = _currency(currency)
    if expected_close is not None:
        deal.expected_close = expected_close
    if lost_reason is not None:
        deal.lost_reason = lost_reason.strip() or None
    if notes is not None:
        deal.notes = notes.strip() or None
    if stage is not None and stage != deal.stage:
        deal.stage = stage
        if probability is None:
            deal.probability = STAGE_PROBABILITY[stage]
        if stage == "won" and deal.won_at is None:
            deal.won_at = datetime.utcnow()
    if probability is not None:
        deal.probability = max(0, min(100, int(probability)))

    if deal.stage == "won" and deal.project_id is None:
        project = models.Project(
            title=deal.title, kind="client", client_id=deal.client_id, deal_id=deal.id,
            status="active", expected_value=deal.amount, currency=deal.currency or "ZMW",
            start_date=date.today(), last_activity_at=datetime.utcnow(),
            created_at=datetime.utcnow(), updated_at=datetime.utcnow())
        db.add(project)
        db.flush()
        deal.project_id = project.id
        changes.append(change_log.record_create(db, project, f"New project from won deal: {deal.title[:50]}"))

    if change_log.snapshot(deal) != before:
        deal.updated_at = datetime.utcnow()
    change = change_log.record_update(db, deal, before, f"Deal '{deal.title[:50]}' → {deal.stage}")
    out = {**serialize_deal(db, deal), "change": change, "changes": [c for c in [change] + changes if c]}
    if project is not None:
        out["project"] = serialize_project(db, project)
    return out


# ── Endpoints ────────────────────────────────────────────────

class ContactCreate(BaseModel):
    name: str
    role: str | None = None
    email: str | None = None
    phone: str | None = None
    whatsapp: str | None = None
    is_primary: bool = False


class ContactPatch(BaseModel):
    name: str | None = None
    role: str | None = None
    email: str | None = None
    phone: str | None = None
    whatsapp: str | None = None
    is_primary: bool | None = None
    notes: str | None = None


class DealCreate(BaseModel):
    client_id: int
    title: str
    amount: float | None = None
    currency: str | None = None
    stage: str = "lead"
    expected_close: date | None = None


class DealPatch(BaseModel):
    stage: str | None = None
    amount: float | None = None
    currency: str | None = None
    probability: int | None = None
    expected_close: date | None = None
    lost_reason: str | None = None
    notes: str | None = None


class CommunicationCreate(BaseModel):
    client_id: int | None = None
    lead_id: int | None = None
    contact_id: int | None = None
    channel: str
    direction: str = "out"
    summary: str
    occurred_at: datetime | None = None


def _client_or_404(db: Session, client_id: int) -> models.Client:
    client = db.query(models.Client).filter(models.Client.id == client_id).first()
    if client is None:
        raise HTTPException(404, "Client not found")
    return client


@router.get("/clients/{client_id}")
def get_client(client_id: int, db: Session = Depends(get_db)):
    return client_detail(db, _client_or_404(db, client_id))


@router.post("/clients/{client_id}/contacts", status_code=201)
def post_contact(client_id: int, payload: ContactCreate, db: Session = Depends(get_db)):
    client = _client_or_404(db, client_id)
    with change_log.batch_scope(db, "ui"):
        try:
            result = add_contact(db, client, **payload.model_dump())
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        db.commit()
    return result


@router.patch("/contacts/{contact_id}")
def patch_contact(contact_id: int, payload: ContactPatch, db: Session = Depends(get_db)):
    import leads
    contact = db.query(models.Contact).filter(models.Contact.id == contact_id).first()
    if contact is None:
        raise HTTPException(404, "Contact not found")
    data = payload.model_dump(exclude_unset=True)
    with change_log.batch_scope(db, "ui"):
        before = change_log.snapshot(contact)
        for key, value in data.items():
            if key == "email":
                value = leads.normalise_email(value)
            elif key in ("phone", "whatsapp"):
                value = leads.normalise_phone(value)
            elif key == "is_primary":
                value = bool(value)
            elif key == "name":
                value = (value or "").strip() or contact.name
            elif isinstance(value, str):
                value = value.strip() or None
            setattr(contact, key, value)
        contact.updated_at = datetime.utcnow()
        change = change_log.record_update(db, contact, before, f"Updated contact: {contact.name[:60]}")
        db.commit()
    return {**serialize_contact(contact), "change": change}


@router.get("/deals")
def list_deals(stage: str | None = None, client_id: int | None = None, db: Session = Depends(get_db)):
    q = db.query(models.Deal)
    if stage:
        q = q.filter(models.Deal.stage == stage)
    if client_id is not None:
        q = q.filter(models.Deal.client_id == client_id)
    return [serialize_deal(db, d) for d in q.order_by(models.Deal.updated_at.desc(), models.Deal.id.desc()).all()]


@router.post("/deals", status_code=201)
def post_deal(payload: DealCreate, db: Session = Depends(get_db)):
    client = _client_or_404(db, payload.client_id)
    with change_log.batch_scope(db, "ui"):
        try:
            result = create_deal(db, client, payload.title, payload.amount, payload.currency,
                                 stage=payload.stage, expected_close=payload.expected_close)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        db.commit()
    return result


@router.patch("/deals/{deal_id}")
def patch_deal(deal_id: int, payload: DealPatch, db: Session = Depends(get_db)):
    deal = db.query(models.Deal).filter(models.Deal.id == deal_id).first()
    if deal is None:
        raise HTTPException(404, "Deal not found")
    with change_log.batch_scope(db, "ui"):
        try:
            result = update_deal(db, deal, **payload.model_dump(exclude_unset=True))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        db.commit()
    return result


@router.get("/communications")
def list_communications(client_id: int | None = None, lead_id: int | None = None, limit: int = 50,
                        db: Session = Depends(get_db)):
    q = db.query(models.Communication)
    if client_id is not None:
        q = q.filter(models.Communication.client_id == client_id)
    if lead_id is not None:
        q = q.filter(models.Communication.lead_id == lead_id)
    rows = q.order_by(models.Communication.occurred_at.desc(), models.Communication.id.desc()).limit(
        max(1, min(int(limit), 200))).all()
    return [serialize_communication(c) for c in rows]


@router.post("/communications", status_code=201)
def post_communication(payload: CommunicationCreate, db: Session = Depends(get_db)):
    client = _client_or_404(db, payload.client_id) if payload.client_id is not None else None
    lead = contact = None
    if payload.lead_id is not None:
        lead = db.query(models.Lead).filter(models.Lead.id == payload.lead_id).first()
        if lead is None:
            raise HTTPException(404, "Lead not found")
    if payload.contact_id is not None:
        contact = db.query(models.Contact).filter(models.Contact.id == payload.contact_id).first()
        if contact is None:
            raise HTTPException(404, "Contact not found")
    with change_log.batch_scope(db, "ui"):
        try:
            result = log_communication(
                db, client=client, lead=lead, contact=contact, channel=payload.channel,
                direction=payload.direction, summary=payload.summary,
                occurred_at=payload.occurred_at, source="ui")
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        db.commit()
    return result


# ════════════════════════════════════════════════════════════════
# Payments, recurring revenue, reconciliation and the summary
# ════════════════════════════════════════════════════════════════

RECONCILE_TOLERANCE = 0.05
INTERVALS = tuple(MONTHS_PER_INTERVAL)


def money_label(amount, currency) -> str:
    """K2,000 for Kwacha, $50 for dollars."""
    value = float(amount or 0)
    text = f"{value:,.0f}" if value == int(value) else f"{value:,.2f}"
    return f"K{text}" if (currency or "ZMW").upper() == "ZMW" else f"${text}"


def _add_months(d: date, months: int, day_of_month: int | None = None) -> date:
    import calendar
    index = d.year * 12 + (d.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    day = min(day_of_month or d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def find_open_payment(db: Session, client_id: int, amount: float, currency: str):
    """The oldest open payment of this client in this currency within 5 % of the amount."""
    rows = db.query(models.Payment).filter(
        models.Payment.client_id == client_id, models.Payment.status.in_(OPEN_PAYMENT),
        models.Payment.currency == currency).all()
    rows = [p for p in rows if p.amount and abs(p.amount - float(amount)) <= RECONCILE_TOLERANCE * p.amount]
    rows.sort(key=lambda p: (p.due_date is None, p.due_date or date.max, p.id))
    return rows[0] if rows else None


def mark_paid(db: Session, payment: models.Payment, paid_at: date | None = None,
              revenue: models.Revenue | None = None) -> dict:
    """Mark a payment paid and make sure it has exactly one revenue row.

    Creates the revenue row (amount, currency, source = the client's name, client_id,
    date = paid_at, notes "payment #id") unless ``revenue`` is an existing row to link.
    Idempotent: a payment that is already paid and linked is returned untouched."""
    if payment.status == "paid" and payment.revenue_id is not None:
        return {**serialize_payment(db, payment), "changes": [], "already_paid": True}
    if payment.status == "cancelled":
        raise ValueError("a cancelled payment cannot be marked paid")
    client = db.query(models.Client).filter(models.Client.id == payment.client_id).first()
    name = client.name if client else f"client #{payment.client_id}"
    day = paid_at or (revenue.date if revenue is not None else None) or date.today()
    changes = []
    if revenue is None:
        revenue = models.Revenue(amount=payment.amount, currency=payment.currency, source=name,
                                 client_id=payment.client_id, date=day, notes=f"payment #{payment.id}",
                                 created_at=datetime.utcnow())
        db.add(revenue)
        changes.append(change_log.record_create(
            db, revenue, f"Revenue {money_label(payment.amount, payment.currency)} from {name[:40]}"))
    db.flush()
    before = change_log.snapshot(payment)
    payment.status, payment.paid_at, payment.revenue_id = "paid", day, revenue.id
    payment.updated_at = datetime.utcnow()
    changes.append(change_log.record_update(
        db, payment, before, f"Paid: {money_label(payment.amount, payment.currency)} from {name[:40]}"))
    touch_projects(db, payment.client_id)
    return {**serialize_payment(db, payment), "changes": [c for c in changes if c]}


def record_payment(db: Session, client: models.Client, amount: float, currency: str | None = None, *,
                   status: str = "paid", title: str | None = None, due_date: date | None = None,
                   paid_at: date | None = None, deal: models.Deal | None = None,
                   project: models.Project | None = None) -> dict:
    """Record money a client owes (``due``) or has paid (``paid`` — which also writes the
    revenue row). The caller commits."""
    if amount is None or float(amount) <= 0:
        raise ValueError("a payment needs an amount above zero")
    if status not in ("paid", "due"):
        raise ValueError("status must be paid or due")
    payment = models.Payment(
        client_id=client.id, deal_id=deal.id if deal else None,
        project_id=project.id if project else None, title=(title or "").strip() or None,
        amount=float(amount), currency=_currency(currency, "ZMW"), status="due", due_date=due_date,
        created_at=datetime.utcnow(), updated_at=datetime.utcnow())
    db.add(payment)
    label = money_label(payment.amount, payment.currency)
    change = change_log.record_create(db, payment, f"Payment due: {label} from {client.name[:40]}")
    changes = [change]
    if status == "paid":
        changes += mark_paid(db, payment, paid_at)["changes"]
    else:
        touch_projects(db, client.id)
    return {**serialize_payment(db, payment), "change": change, "changes": [c for c in changes if c]}


def add_recurring(db: Session, client: models.Client, title: str, amount: float,
                  currency: str | None = None, interval: str = "monthly",
                  day_of_month: int | None = None, start: date | None = None) -> dict:
    title = (title or "").strip()
    if not title:
        raise ValueError("recurring revenue needs a title")
    if amount is None or float(amount) <= 0:
        raise ValueError("recurring revenue needs an amount above zero")
    if interval not in INTERVALS:
        raise ValueError(f"interval must be one of: {', '.join(INTERVALS)}")
    today = date.today()
    if start is not None:
        day = int(day_of_month or start.day)
        next_due = start
    else:
        day = int(day_of_month or 1)
        next_due = _add_months(today.replace(day=1), 0, day)
        if next_due < today:
            next_due = _add_months(next_due, 1, day)
    day = max(1, min(31, day))
    rec = models.RecurringRevenue(
        client_id=client.id, title=title, amount=float(amount), currency=_currency(currency, "ZMW"),
        interval=interval, day_of_month=day, next_due=next_due, active=True,
        created_at=datetime.utcnow(), updated_at=datetime.utcnow())
    db.add(rec)
    change = change_log.record_create(
        db, rec, f"Recurring: {money_label(rec.amount, rec.currency)} {interval} from {client.name[:30]}")
    return {**serialize_recurring(db, rec), "change": change}


def generate_due(db: Session, today: date | None = None) -> int:
    """Create the due payment for every recurring period that has arrived, and move
    ``next_due`` on. Idempotent: a period that already has its payment is not created again
    (checked here, and guaranteed by the unique index). The caller commits."""
    today = today or date.today()
    created = 0
    for rec in db.query(models.RecurringRevenue).filter(
            models.RecurringRevenue.active == True, models.RecurringRevenue.next_due <= today).all():   # noqa: E712
        client = db.query(models.Client).filter(models.Client.id == rec.client_id).first()
        step = MONTHS_PER_INTERVAL.get(rec.interval or "monthly", 1)
        before = change_log.snapshot(rec)
        for _ in range(60):                      # bounded: at most five years of monthly catch-up
            if rec.next_due > today or (rec.ended_at is not None and rec.next_due > rec.ended_at):
                break
            period = rec.next_due
            exists = db.query(models.Payment).filter(
                models.Payment.recurring_id == rec.id, models.Payment.period_start == period).first()
            if exists is None:
                payment = models.Payment(
                    client_id=rec.client_id, title=f"{rec.title} — {period.strftime('%b %Y')}",
                    amount=rec.amount, currency=rec.currency, status="due", due_date=period,
                    recurring_id=rec.id, period_start=period,
                    created_at=datetime.utcnow(), updated_at=datetime.utcnow())
                db.add(payment)
                change_log.record_create(
                    db, payment,
                    f"Payment due: {money_label(rec.amount, rec.currency)} from {(client.name if client else '?')[:40]}")
                created += 1
            rec.next_due = _add_months(period, step, rec.day_of_month)
        if change_log.snapshot(rec) != before:
            rec.updated_at = datetime.utcnow()
            change_log.record_update(db, rec, before, f"Recurring '{rec.title[:40]}' next due {rec.next_due}")
    return created


def refresh_statuses(db: Session, today: date | None = None) -> int:
    """due → overdue once the due date has passed (and back if the date was moved). The
    status is derived from the date, so the flip is not written to the change log."""
    today = today or date.today()
    flipped = 0
    for p in db.query(models.Payment).filter(models.Payment.status.in_(OPEN_PAYMENT)).all():
        want = "overdue" if (p.due_date is not None and p.due_date < today) else "due"
        if p.status != want:
            p.status = want
            flipped += 1
    return flipped


def reconcile(db: Session) -> int:
    """Link stray revenue rows to open payments: a revenue row that names a client and is
    not yet tied to a payment marks the client's oldest open payment in the same currency
    within 5 % of the amount as paid. Idempotent. The caller commits."""
    linked = {r[0] for r in db.query(models.Payment.revenue_id).filter(
        models.Payment.revenue_id.isnot(None)).all()}
    matched = 0
    for rev in db.query(models.Revenue).filter(
            models.Revenue.client_id.isnot(None)).order_by(models.Revenue.date, models.Revenue.id).all():
        if rev.id in linked:
            continue
        payment = find_open_payment(db, rev.client_id, rev.amount, (rev.currency or "USD").upper())
        if payment is None:
            continue
        mark_paid(db, payment, rev.date, revenue=rev)
        linked.add(rev.id)
        matched += 1
    return matched


def refresh_health(db: Session) -> int:
    """Store each client's computed health for listing. Not logged (derived)."""
    changed = 0
    for client in db.query(models.Client).all():
        health = client_health(db, client)
        if client.health != health:
            client.health = health
            changed += 1
    return changed


def housekeeping(db: Session, source: str = "job:crm") -> dict:
    """generate_due, refresh_statuses, reconcile and refresh_health in one logged batch.
    Commits. Cheap when there is nothing to do."""
    with change_log.batch_scope(db, source):
        out = {"generated": generate_due(db), "status_changes": refresh_statuses(db),
               "reconciled": reconcile(db), "health_changes": refresh_health(db)}
        db.commit()
    return out


def summary(db: Session, today: date | None = None) -> dict:
    """The numbers behind GET /crm/summary. Read-only (the endpoint runs housekeeping first)."""
    today = today or date.today()
    since = today - timedelta(days=30)
    clients = db.query(models.Client).all()
    deals = db.query(models.Deal).all()
    payments = db.query(models.Payment).all()
    by_stage = {}
    weighted = 0.0
    for d in deals:
        usd = _usd(d.amount, d.currency) if d.amount is not None else 0.0
        entry = by_stage.setdefault(d.stage, {"count": 0, "amount_usd": 0.0})
        entry["count"] += 1
        entry["amount_usd"] = round(entry["amount_usd"] + usd, 2)
        if d.stage not in ("won", "lost"):
            prob = d.probability if d.probability is not None else STAGE_PROBABILITY.get(d.stage, 0)
            weighted += usd * prob / 100
    overdue = [p for p in payments if p.status == "overdue"]
    recurring = db.query(models.RecurringRevenue).filter(models.RecurringRevenue.active == True).all()   # noqa: E712
    health = {"green": 0, "amber": 0, "red": 0}
    for c in clients:
        if c.status not in ("closed", "lost"):
            health[client_health(db, c, today)] += 1
    revenue_rows = db.query(models.Revenue).filter(models.Revenue.date >= since).all()
    return {
        "clients": {"active": sum(1 for c in clients if c.status == "active"),
                    "paused": sum(1 for c in clients if c.status == "paused"), "total": len(clients)},
        "deals": {"by_stage": by_stage, "weighted_pipeline_usd": round(weighted, 2)},
        "payments": {
            "due_usd": round(sum(_usd(p.amount, p.currency) for p in payments if p.status == "due"), 2),
            "overdue_usd": round(sum(_usd(p.amount, p.currency) for p in overdue), 2),
            "overdue_count": len(overdue),
            "paid_30d_usd": round(sum(_usd(p.amount, p.currency) for p in payments
                                      if p.status == "paid" and p.paid_at and p.paid_at >= since), 2),
        },
        "mrr": _totals(recurring, amount_of=monthly_amount),
        "revenue_30d_usd": round(sum(_usd(r.amount, r.currency) for r in revenue_rows), 2),
        "health": health,
    }


# ── Endpoints ────────────────────────────────────────────────

class PaymentCreate(BaseModel):
    client_id: int
    amount: float
    currency: str | None = None
    status: str = "due"
    title: str | None = None
    due_date: date | None = None
    deal_id: int | None = None
    project_id: int | None = None


class PaidBody(BaseModel):
    paid_at: date | None = None


class RecurringCreate(BaseModel):
    client_id: int
    title: str
    amount: float
    currency: str | None = None
    interval: str = "monthly"
    day_of_month: int | None = None
    start: date | None = None


class RecurringPatch(BaseModel):
    active: bool | None = None
    amount: float | None = None
    currency: str | None = None
    interval: str | None = None
    day_of_month: int | None = None
    ended_at: date | None = None


def _payment_or_404(db: Session, payment_id: int) -> models.Payment:
    payment = db.query(models.Payment).filter(models.Payment.id == payment_id).first()
    if payment is None:
        raise HTTPException(404, "Payment not found")
    return payment


@router.get("/payments")
def list_payments(status: str | None = "due,overdue", client_id: int | None = None,
                  from_: date | None = Query(None, alias="from"), to: date | None = None,
                  db: Session = Depends(get_db)):
    q = db.query(models.Payment)
    wanted = [s.strip() for s in (status or "").split(",") if s.strip()]
    if wanted and wanted != ["all"]:
        q = q.filter(models.Payment.status.in_(wanted))
    if client_id is not None:
        q = q.filter(models.Payment.client_id == client_id)
    rows = q.all()

    def day(p):
        return p.due_date or p.paid_at

    if from_ is not None:
        rows = [p for p in rows if day(p) is not None and day(p) >= from_]
    if to is not None:
        rows = [p for p in rows if day(p) is not None and day(p) <= to]
    rows.sort(key=lambda p: (day(p) is None, day(p) or date.max, p.id))
    return [serialize_payment(db, p) for p in rows]


@router.post("/payments", status_code=201)
def post_payment(payload: PaymentCreate, db: Session = Depends(get_db)):
    client = _client_or_404(db, payload.client_id)
    deal = project = None
    if payload.deal_id is not None:
        deal = db.query(models.Deal).filter(models.Deal.id == payload.deal_id).first()
        if deal is None:
            raise HTTPException(404, "Deal not found")
    if payload.project_id is not None:
        project = db.query(models.Project).filter(models.Project.id == payload.project_id).first()
        if project is None:
            raise HTTPException(404, "Project not found")
    with change_log.batch_scope(db, "ui"):
        try:
            result = record_payment(db, client, payload.amount, payload.currency, status=payload.status,
                                    title=payload.title, due_date=payload.due_date, deal=deal, project=project)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        refresh_statuses(db)
        db.commit()
    return {**serialize_payment(db, _payment_or_404(db, result["id"])),
            "change": result["change"], "changes": result["changes"]}


@router.post("/payments/{payment_id}/paid")
def pay_payment(payment_id: int, payload: PaidBody | None = None, db: Session = Depends(get_db)):
    payment = _payment_or_404(db, payment_id)
    with change_log.batch_scope(db, "ui"):
        try:
            result = mark_paid(db, payment, payload.paid_at if payload else None)
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        db.commit()
    return result


@router.post("/payments/{payment_id}/cancel")
def cancel_payment(payment_id: int, db: Session = Depends(get_db)):
    payment = _payment_or_404(db, payment_id)
    if payment.status == "paid":
        raise HTTPException(409, "A paid payment cannot be cancelled; undo the payment instead")
    with change_log.batch_scope(db, "ui"):
        before = change_log.snapshot(payment)
        payment.status = "cancelled"
        payment.updated_at = datetime.utcnow()
        change = change_log.record_update(
            db, payment, before, f"Cancelled payment: {money_label(payment.amount, payment.currency)}")
        db.commit()
    return {**serialize_payment(db, payment), "change": change}


@router.get("/recurring")
def list_recurring(db: Session = Depends(get_db)):
    rows = db.query(models.RecurringRevenue).order_by(
        models.RecurringRevenue.active.desc(), models.RecurringRevenue.next_due).all()
    return [serialize_recurring(db, r) for r in rows]


@router.post("/recurring", status_code=201)
def post_recurring(payload: RecurringCreate, db: Session = Depends(get_db)):
    client = _client_or_404(db, payload.client_id)
    with change_log.batch_scope(db, "ui"):
        try:
            result = add_recurring(db, client, payload.title, payload.amount, payload.currency,
                                   payload.interval, payload.day_of_month, payload.start)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        db.commit()
    return result


@router.patch("/recurring/{recurring_id}")
def patch_recurring(recurring_id: int, payload: RecurringPatch, db: Session = Depends(get_db)):
    rec = db.query(models.RecurringRevenue).filter(models.RecurringRevenue.id == recurring_id).first()
    if rec is None:
        raise HTTPException(404, "Recurring revenue not found")
    data = payload.model_dump(exclude_unset=True)
    if data.get("interval") is not None and data["interval"] not in INTERVALS:
        raise HTTPException(400, f"interval must be one of: {', '.join(INTERVALS)}")
    with change_log.batch_scope(db, "ui"):
        before = change_log.snapshot(rec)
        for key, value in data.items():
            if key == "currency":
                value = _currency(value, rec.currency)
            elif key == "active":
                value = bool(value)
            elif key == "day_of_month" and value is not None:
                value = max(1, min(31, int(value)))
            setattr(rec, key, value)
        if rec.ended_at is not None and rec.ended_at <= date.today():
            rec.active = False
        rec.updated_at = datetime.utcnow()
        change = change_log.record_update(db, rec, before, f"Updated recurring: {rec.title[:60]}")
        db.commit()
    return {**serialize_recurring(db, rec), "change": change}


@router.get("/crm/summary")
def crm_summary(db: Session = Depends(get_db)):
    housekeeping(db)
    return summary(db)
