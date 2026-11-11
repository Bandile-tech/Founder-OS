"""
Leads — the prospect database (Phase 5B).

Primitives: serialize, find_duplicate, create_lead, find_leads, pick_lead, import_csv,
            segments, set_follow_up, close_follow_ups, pre_score, qualify, log_reply, convert
Endpoints:  GET /leads, GET /leads/segments, GET /leads/{id}, POST /leads,
            PATCH /leads/{id}, POST /leads/import, POST /leads/qualify,
            POST /leads/{id}/replied, POST /leads/{id}/convert

A lead is a person or business Bandile intends to approach. Leads arrive by CSV import
(an upsert with counts) or from conversation, never through a form. Every write is in the
change log.
"""

import csv
import io
import json
from datetime import date, datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

import business_common
import change_log
import models
from database import get_db

router = APIRouter()

STATUSES = ("new", "qualified", "disqualified", "contacted", "replied", "converted", "lost")

# Columns a caller may set, in the order they are reported.
FIELDS = ("name", "company", "role", "email", "phone", "whatsapp", "linkedin_url", "website",
          "location", "segment", "notes")

# CSV header (lower-cased, trimmed) → lead column.
HEADER_SYNONYMS = {
    "name": "name", "full name": "name", "contact": "name", "contact name": "name", "owner": "name",
    "company": "company", "business": "company", "organisation": "company",
    "organization": "company", "org": "company",
    "role": "role", "title": "role", "position": "role",
    "email": "email", "e-mail": "email", "mail": "email",
    "phone": "phone", "tel": "phone", "telephone": "phone", "mobile": "phone", "cell": "phone",
    "number": "phone",
    "whatsapp": "whatsapp", "wa": "whatsapp",
    "linkedin": "linkedin_url", "linkedin url": "linkedin_url", "profile": "linkedin_url",
    "website": "website", "site": "website", "url": "website",
    "location": "location", "city": "location", "town": "location", "area": "location",
    "segment": "segment", "industry": "segment", "category": "segment", "type": "segment",
    "notes": "notes", "comment": "notes", "comments": "notes",
}


# ── Normalisation ────────────────────────────────────────────

def normalise_email(value) -> str | None:
    v = (str(value or "")).strip().lower()
    return v or None


def normalise_phone(value) -> str | None:
    """Digits and one leading '+' only. A local number stays as typed ('0977…'); no country
    code is added."""
    raw = (str(value or "")).strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return None
    return ("+" if raw.startswith("+") else "") + digits


def normalise_segment(value) -> str | None:
    v = " ".join((str(value or "")).split()).lower()
    return v or None


def _clean(fields: dict) -> dict:
    """Trim, normalise and drop empty values from a dict of lead fields."""
    out = {}
    for key in FIELDS:
        value = fields.get(key)
        if value is None:
            continue
        if key == "email":
            value = normalise_email(value)
        elif key in ("phone", "whatsapp"):
            value = normalise_phone(value)
        elif key == "segment":
            value = normalise_segment(value)
        else:
            value = str(value).strip() or None
        if value:
            out[key] = value
    return out


# ── Serialisation ────────────────────────────────────────────

def _qualification(lead: models.Lead):
    try:
        return json.loads(lead.qualification_json) if lead.qualification_json else None
    except (ValueError, TypeError):
        return None


def open_follow_up(db: Session, lead_id: int):
    """The lead's open follow-up todo (earliest due first), or None."""
    todos = db.query(models.Todo).filter(
        models.Todo.lead_id == lead_id, models.Todo.done == False,   # noqa: E712
        models.Todo.category == "outreach",
    ).all()
    if not todos:
        return None
    todos.sort(key=lambda t: (t.due_date is None, t.due_date, t.id))
    return todos[0]


def serialize(db: Session, lead: models.Lead) -> dict:
    follow = open_follow_up(db, lead.id)
    last = db.query(models.OutreachMessage).filter(
        models.OutreachMessage.lead_id == lead.id,
    ).order_by(models.OutreachMessage.id.desc()).first()
    return {
        "id": lead.id, "name": lead.name, "company": lead.company, "role": lead.role,
        "email": lead.email, "phone": lead.phone, "whatsapp": lead.whatsapp,
        "linkedin_url": lead.linkedin_url, "website": lead.website, "location": lead.location,
        "segment": lead.segment, "source": lead.source, "status": lead.status,
        "qualification_score": lead.qualification_score,
        "qualification": _qualification(lead),
        "opportunity_id": lead.opportunity_id, "client_id": lead.client_id,
        "notes": lead.notes, "last_contact_at": lead.last_contact_at,
        "next_follow_up_at": lead.next_follow_up_at,
        "created_at": lead.created_at, "updated_at": lead.updated_at,
        "open_follow_up": {"todo_id": follow.id, "due_date": follow.due_date} if follow else None,
        "last_message": ({"id": last.id, "channel": last.channel, "status": last.status,
                          "created_at": last.created_at} if last else None),
    }


def serialize_message(db: Session, m: models.OutreachMessage, *, lead_name: bool = False) -> dict:
    out = {
        "id": m.id, "lead_id": m.lead_id, "channel": m.channel, "direction": m.direction,
        "subject": m.subject, "body": m.body, "status": m.status, "version": m.version,
        "supersedes_id": m.supersedes_id, "brief": m.brief,
        "pending_action_id": m.pending_action_id, "approved_at": m.approved_at,
        "sent_at": m.sent_at, "external_id": m.external_id, "error": m.error,
        "created_by": m.created_by, "created_at": m.created_at, "updated_at": m.updated_at,
    }
    if lead_name:
        lead = db.query(models.Lead).filter(models.Lead.id == m.lead_id).first()
        out["lead_name"] = lead.name if lead else None
    return out


# ── Primitives ───────────────────────────────────────────────

def find_duplicate(db: Session, fields: dict):
    """The lead these fields already describe, by the dedupe key: the email when there is
    one, else the WhatsApp or phone number, else the lower-cased name and company."""
    email = fields.get("email")
    if email:
        return db.query(models.Lead).filter(models.Lead.email == email).order_by(models.Lead.id).first()
    numbers = [n for n in (fields.get("whatsapp"), fields.get("phone")) if n]
    if numbers:
        return db.query(models.Lead).filter(
            models.Lead.phone.in_(numbers) | models.Lead.whatsapp.in_(numbers)
        ).order_by(models.Lead.id).first()
    name = (fields.get("name") or "").lower()
    company = (fields.get("company") or "").lower()
    for lead in db.query(models.Lead).filter(func.lower(models.Lead.name) == name).order_by(models.Lead.id).all():
        if (lead.company or "").lower() == company:
            return lead
    return None


def _merge_notes(existing: str | None, incoming: str) -> str:
    """Append the incoming lines that are not already there."""
    have = [line for line in (existing or "").split("\n")]
    seen = {line.strip() for line in have if line.strip()}
    for line in incoming.split("\n"):
        if line.strip() and line.strip() not in seen:
            have.append(line)
            seen.add(line.strip())
    return "\n".join(line for line in have if line.strip())


def create_lead(db: Session, fields: dict, *, source: str = "manual") -> tuple:
    """Create a lead, or update the one these fields already describe.

    Returns ``(lead, deduped, change)``. On a duplicate, non-empty incoming values replace
    what is stored (blanks never overwrite data, notes are appended). The caller commits."""
    data = _clean(fields)
    if not data.get("name"):
        if not data.get("company"):
            raise ValueError("a lead needs a name or a company")
        data["name"] = data["company"]
    if data.get("phone") and not data.get("whatsapp"):
        data["whatsapp"] = data["phone"]

    existing = find_duplicate(db, data)
    if existing is not None:
        before = change_log.snapshot(existing)
        for key, value in data.items():
            if key == "notes":
                existing.notes = _merge_notes(existing.notes, value)
            elif key == "whatsapp" and "whatsapp" not in _clean(fields) and existing.whatsapp:
                continue          # a defaulted WhatsApp number never replaces a stored one
            else:
                setattr(existing, key, value)
        if change_log.snapshot(existing) != before:
            existing.updated_at = datetime.utcnow()
        change = change_log.record_update(db, existing, before, f"Updated lead: {existing.name[:70]}")
        return existing, True, change

    lead = models.Lead(source=source, status="new", created_at=datetime.utcnow(),
                       updated_at=datetime.utcnow(), **data)
    db.add(lead)
    change = change_log.record_create(db, lead, f"Added lead: {lead.name[:70]}")
    return lead, False, change


def find_leads(db: Session, *, query: str | None = None, segment: str | None = None,
               status: str | None = None, limit: int = 100) -> list:
    q = db.query(models.Lead)
    if status:
        q = q.filter(models.Lead.status == status)
    if segment:
        q = q.filter(models.Lead.segment == normalise_segment(segment))
    if query and query.strip():
        frag = query.strip().lower()
        q = q.filter(func.lower(models.Lead.name).contains(frag)
                     | func.lower(func.coalesce(models.Lead.company, "")).contains(frag))
    return q.order_by(models.Lead.updated_at.desc(), models.Lead.id.desc()).limit(max(1, min(int(limit), 500))).all()


def pick_lead(db: Session, ref):
    """Resolve a lead from an id or a name fragment. Returns ``(lead, candidates)`` with the
    same accept rule as task completion (top >= 0.75 and >= 0.2 clear of the runner-up)."""
    if isinstance(ref, int) or (isinstance(ref, str) and ref.strip().isdigit()):
        lead = db.query(models.Lead).filter(models.Lead.id == int(ref)).first()
        return lead, ([(1.0, lead)] if lead else [])
    rows = db.query(models.Lead).all()
    return business_common.fuzzy_pick(
        rows, lambda l: [l.name, l.company, f"{l.name} {l.company or ''}"], str(ref or ""))


def segments(db: Session) -> list:
    out = {}
    for lead in db.query(models.Lead).all():
        key = lead.segment or ""
        entry = out.setdefault(key, {"segment": lead.segment, "total": 0, "by_status": {}})
        entry["total"] += 1
        entry["by_status"][lead.status] = entry["by_status"].get(lead.status, 0) + 1
    return sorted(out.values(), key=lambda e: (-e["total"], e["segment"] or ""))


# ── CSV import ───────────────────────────────────────────────

def import_csv(db: Session, text: str, *, segment: str | None = None,
               mapping: dict | None = None, filename: str | None = None) -> dict:
    """Upsert leads from CSV text. One ``lead_imports`` row, one change-log batch, commits.

    ``mapping`` ({csv header: lead column}) overrides the header synonyms. Columns that map
    to nothing are kept in ``notes`` as ``Header: value`` lines. Rows with neither a name
    nor a company are skipped; a row matching an existing lead updates it."""
    reader = csv.reader(io.StringIO(text or ""))
    rows = [r for r in reader]
    header = [h.strip() for h in rows[0]] if rows else []
    override = {str(k).strip().lower(): str(v).strip() for k, v in (mapping or {}).items()}
    columns = {}
    for h in header:
        target = override.get(h.lower()) or HEADER_SYNONYMS.get(h.lower())
        if target in FIELDS:
            columns[h] = target
    default_segment = normalise_segment(segment)

    with change_log.batch_scope(db, "ui"):
        record = models.LeadImport(filename=filename, segment=default_segment,
                                   mapping_json=json.dumps(columns), created_at=datetime.utcnow())
        db.add(record)
        db.flush()
        created = updated = skipped = 0
        data_rows = rows[1:]
        for raw in data_rows:
            fields, extra = {}, []
            for i, h in enumerate(header):
                value = (raw[i] if i < len(raw) else "").strip()
                if not value:
                    continue
                target = columns.get(h)
                if target is None:
                    extra.append(f"{h}: {value}")
                elif target == "notes" and fields.get("notes"):
                    fields["notes"] += "\n" + value
                elif target not in fields:
                    fields[target] = value
                else:
                    extra.append(f"{h}: {value}")     # a second column for the same field is kept
            if not fields.get("name") and not fields.get("company"):
                skipped += 1
                continue
            if extra:
                fields["notes"] = "\n".join(([fields["notes"]] if fields.get("notes") else []) + extra)
            if default_segment and not fields.get("segment"):
                fields["segment"] = default_segment
            _lead, deduped, _change = create_lead(db, fields, source=f"csv:{record.id}")
            if deduped:
                updated += 1
            else:
                created += 1
        record.created, record.updated, record.skipped = created, updated, skipped
        record.total_rows = len(data_rows)
        batch_id = change_log.current_batch_id(db)
        db.commit()
        return {"import_id": record.id, "created": created, "updated": updated, "skipped": skipped,
                "total_rows": len(data_rows), "segment": default_segment,
                "columns_mapped": columns, "batch_id": batch_id}


# ── Endpoints ────────────────────────────────────────────────

class LeadCreate(BaseModel):
    name: str
    company: str | None = None
    role: str | None = None
    email: str | None = None
    phone: str | None = None
    whatsapp: str | None = None
    linkedin_url: str | None = None
    website: str | None = None
    location: str | None = None
    segment: str | None = None
    notes: str | None = None


class LeadPatch(BaseModel):
    name: str | None = None
    company: str | None = None
    role: str | None = None
    email: str | None = None
    phone: str | None = None
    whatsapp: str | None = None
    linkedin_url: str | None = None
    website: str | None = None
    location: str | None = None
    segment: str | None = None
    notes: str | None = None
    status: str | None = None


def _get(db: Session, lead_id: int) -> models.Lead:
    lead = db.query(models.Lead).filter(models.Lead.id == lead_id).first()
    if lead is None:
        raise HTTPException(404, "Lead not found")
    return lead


@router.get("/leads")
def list_leads(status: str | None = None, segment: str | None = None, q: str | None = None,
               limit: int = 100, db: Session = Depends(get_db)):
    return [serialize(db, l) for l in find_leads(db, query=q, segment=segment, status=status, limit=limit)]


@router.get("/leads/segments")
def list_segments(db: Session = Depends(get_db)):
    return segments(db)


@router.post("/leads/import")
async def import_leads(file: UploadFile = File(...), segment: str | None = Form(None),
                       mapping: str | None = Form(None), db: Session = Depends(get_db)):
    """Multipart: file (CSV), optional segment, optional mapping (JSON {csv header: column})."""
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    parsed_mapping = None
    if mapping:
        try:
            parsed_mapping = json.loads(mapping)
        except ValueError:
            raise HTTPException(400, "mapping must be a JSON object")
        if not isinstance(parsed_mapping, dict):
            raise HTTPException(400, "mapping must be a JSON object")
    return import_csv(db, text, segment=segment, mapping=parsed_mapping, filename=file.filename)


@router.get("/leads/{lead_id}")
def get_lead(lead_id: int, db: Session = Depends(get_db)):
    lead = _get(db, lead_id)
    messages = db.query(models.OutreachMessage).filter(
        models.OutreachMessage.lead_id == lead.id).order_by(models.OutreachMessage.id).all()
    follow_ups = db.query(models.Todo).filter(
        models.Todo.lead_id == lead.id, models.Todo.done == False).order_by(models.Todo.id).all()   # noqa: E712
    return {**serialize(db, lead),
            "messages": [serialize_message(db, m) for m in messages],
            "follow_ups": [{"todo_id": t.id, "text": t.text, "due_date": t.due_date} for t in follow_ups]}


@router.post("/leads", status_code=201)
def post_lead(payload: LeadCreate, response: Response, db: Session = Depends(get_db)):
    with change_log.batch_scope(db, "ui"):
        try:
            lead, deduped, change = create_lead(db, payload.model_dump(exclude_none=True), source="manual")
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        db.commit()
    if deduped:
        response.status_code = 200
    return {**serialize(db, lead), "change": change, "deduped": deduped}


@router.patch("/leads/{lead_id}")
def patch_lead(lead_id: int, payload: LeadPatch, db: Session = Depends(get_db)):
    lead = _get(db, lead_id)
    data = payload.model_dump(exclude_unset=True)
    status = data.pop("status", None)
    if status is not None and status not in STATUSES:
        raise HTTPException(400, f"Invalid status. Must be one of: {', '.join(STATUSES)}")
    with change_log.batch_scope(db, "ui"):
        before = change_log.snapshot(lead)
        cleaned = _clean(data)
        for key in data:
            if key == "name" and not cleaned.get("name"):
                continue                      # a lead always keeps a name
            setattr(lead, key, cleaned.get(key))
        if status is not None:
            lead.status = status
        lead.updated_at = datetime.utcnow()
        change = change_log.record_update(db, lead, before, f"Updated lead: {lead.name[:70]}")
        db.commit()
    return {**serialize(db, lead), "change": change}


# ════════════════════════════════════════════════════════════════
# Follow-ups and qualification
# ════════════════════════════════════════════════════════════════

QUALIFY_MAX = 20
QUALIFIED_AT = 50
ZAMBIAN_PLACES = ("lusaka", "kitwe", "ndola", "kabwe", "livingstone", "chingola", "mufulira",
                  "chipata", "solwezi", "kasama", "zambia")

# Stable first so the prefix caches; the ideal-customer file is appended after it.
QUALIFY_PROMPT = (
    "You score how well one sales lead fits the ideal customer described below. Judge only "
    "from the lead's fields; do not invent facts. Respond as JSON: "
    "{\"fit\": <integer 0-60>, \"reasons\": [up to 3 short reasons], "
    "\"disqualifiers\": [any disqualifier from the profile that applies, else an empty list], "
    "\"suggested_angle\": \"one sentence on what to open with\"}."
)


def set_follow_up(db: Session, lead: models.Lead, due: date, reason: str | None = None) -> tuple:
    """Create the lead's follow-up task, or move the open one. Returns ``(todo, change)``.
    A follow-up is an ordinary todo (category and source ``outreach``), so the briefing,
    the home checkbox and undo already handle it. The caller commits."""
    text = f"Follow up with {lead.name}" + (f" — {reason.strip()}" if reason and reason.strip() else "")
    todo = open_follow_up(db, lead.id)
    if todo is not None:
        before = change_log.snapshot(todo)
        todo.due_date, todo.due = due, due.isoformat()
        if reason and reason.strip():
            todo.text = text
        change = change_log.record_update(db, todo, before, f"Follow-up with {lead.name[:50]} moved to {due.isoformat()}")
    else:
        todo = models.Todo(text=text, priority=3, done=False, category="outreach", source="outreach",
                           due=due.isoformat(), due_date=due, lead_id=lead.id)
        db.add(todo)
        change = change_log.record_create(db, todo, f"Follow-up with {lead.name[:50]} on {due.isoformat()}")
    lead.next_follow_up_at = due
    return todo, change


def close_follow_ups(db: Session, lead: models.Lead) -> list:
    """Mark the lead's open follow-up tasks done. Returns the change entries."""
    changes = []
    for todo in db.query(models.Todo).filter(
            models.Todo.lead_id == lead.id, models.Todo.done == False,   # noqa: E712
            models.Todo.category == "outreach").all():
        before = change_log.snapshot(todo)
        todo.done, todo.completed_at = True, date.today()
        changes.append(change_log.record_update(db, todo, before, f"Completed task: {todo.text[:80]}"))
    lead.next_follow_up_at = None
    return [c for c in changes if c]


def pre_score(lead: models.Lead) -> int:
    """The deterministic part of the score (0-40): reachable, has a company, has a segment,
    is in Zambia."""
    score = 0
    if lead.email or lead.whatsapp or lead.phone:
        score += 15
    if (lead.company or "").strip():
        score += 10
    if (lead.segment or "").strip():
        score += 10
    if any(place in (lead.location or "").lower() for place in ZAMBIAN_PLACES):
        score += 5
    return score


def _icp() -> str:
    from pathlib import Path
    path = Path(__file__).resolve().parent.parent / "context" / "core" / "icp.md"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _llm_fit(db: Session, lead: models.Lead) -> dict:
    """One model call for one lead. {} when the answer is unusable."""
    import llm
    fields = {k: v for k, v in {
        "name": lead.name, "company": lead.company, "role": lead.role, "segment": lead.segment,
        "location": lead.location, "website": lead.website, "linkedin_url": lead.linkedin_url,
        "has_email": bool(lead.email), "has_phone": bool(lead.whatsapp or lead.phone),
        "notes": lead.notes,
    }.items() if v}
    messages = [
        {"role": "system", "content": QUALIFY_PROMPT + "\n\n" + _icp()},
        {"role": "user", "content": json.dumps(fields, ensure_ascii=False)},
    ]
    raw = llm.chat_text(messages, db=db, purpose="lead_qualify", max_tokens=300,
                        response_format={"type": "json_object"})
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def qualify(db: Session, lead_ids: list[int]) -> list[dict]:
    """Score leads against the ideal customer: a deterministic pre-score (0-40) plus one
    model call per lead for fit (0-60). At most 20 leads per call (``ValueError`` above
    that). With no key only the pre-score is reported and nothing changes. The caller
    commits."""
    import llm
    ids = [int(i) for i in (lead_ids or [])]
    if len(ids) > QUALIFY_MAX:
        raise ValueError(f"max {QUALIFY_MAX} per call")
    configured = llm.is_configured()
    results = []
    for lead_id in ids:
        lead = db.query(models.Lead).filter(models.Lead.id == lead_id).first()
        if lead is None:
            results.append({"lead_id": lead_id, "error": "lead not found"})
            continue
        pre = pre_score(lead)
        entry = {"lead_id": lead.id, "name": lead.name, "pre_score": pre, "score": pre,
                 "status": lead.status, "fit": None, "reasons": [], "disqualifiers": [],
                 "suggested_angle": None}
        if not configured:
            entry["skipped_llm"] = True
            results.append(entry)
            continue
        try:
            answer = _llm_fit(db, lead)
        except Exception as exc:                # noqa: BLE001 — one bad call must not stop the rest
            print(f"[leads] qualification failed for lead {lead.id}: {type(exc).__name__}")
            answer = {}
        try:
            fit = max(0, min(60, int(round(float(answer.get("fit"))))))
        except (TypeError, ValueError):
            entry["error"] = "the model gave no usable score"
            results.append(entry)
            continue
        reasons = [str(r)[:200] for r in (answer.get("reasons") or []) if r][:3]
        disqualifiers = [str(d)[:200] for d in (answer.get("disqualifiers") or []) if d]
        angle = (str(answer.get("suggested_angle") or "")).strip() or None
        score = pre + fit
        if disqualifiers and fit < 15:
            outcome = "disqualified"
        elif score >= QUALIFIED_AT:
            outcome = "qualified"
        else:
            outcome = "below_threshold"

        before = change_log.snapshot(lead)
        lead.qualification_score = score
        lead.qualification_json = json.dumps({
            "fit": fit, "reasons": reasons, "disqualifiers": disqualifiers,
            "suggested_angle": angle, "model": llm.assistant_model(),
            "scored_at": datetime.utcnow().isoformat(timespec="seconds")})
        # Only a lead that has not been contacted yet changes status on a score.
        if outcome != "below_threshold" and lead.status in ("new", "qualified", "disqualified"):
            lead.status = outcome
        lead.updated_at = datetime.utcnow()
        verb = {"qualified": "Qualified", "disqualified": "Disqualified"}.get(outcome, "Scored")
        change = change_log.record_update(db, lead, before, f"{verb} {lead.name[:50]} {score}/100")
        entry.update({"score": score, "fit": fit, "reasons": reasons, "disqualifiers": disqualifiers,
                      "suggested_angle": angle, "status": lead.status, "outcome": outcome,
                      "change": change})
        results.append(entry)
    return results


class QualifyBody(BaseModel):
    ids: list[int] | None = None
    segment: str | None = None


@router.post("/leads/qualify")
def qualify_endpoint(payload: QualifyBody, db: Session = Depends(get_db)):
    """``{ids: [..≤20]}`` or ``{segment}`` (the first 20 leads of the segment still ``new``)."""
    if payload.ids:
        ids = payload.ids
    elif payload.segment:
        ids = [l.id for l in db.query(models.Lead).filter(
            models.Lead.segment == normalise_segment(payload.segment), models.Lead.status == "new",
        ).order_by(models.Lead.id).limit(QUALIFY_MAX).all()]
    else:
        raise HTTPException(400, "Give ids or a segment")
    with change_log.batch_scope(db, "ui"):
        try:
            results = qualify(db, ids)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        db.commit()
    return {"results": results, "changes": [r["change"] for r in results if r.get("change")]}


# ════════════════════════════════════════════════════════════════
# Replies and conversion
# ════════════════════════════════════════════════════════════════

def log_reply(db: Session, lead: models.Lead, summary: str | None = None,
              channel: str | None = None) -> dict:
    """The lead answered: an inbound message row, status ``replied``, follow-ups closed and
    a dated line in the notes. The caller commits."""
    import outreach_channels
    last_out = db.query(models.OutreachMessage).filter(
        models.OutreachMessage.lead_id == lead.id, models.OutreachMessage.direction == "out",
    ).order_by(models.OutreachMessage.id.desc()).first()
    channel = channel if channel in outreach_channels.CHANNELS else (last_out.channel if last_out else "whatsapp")
    text = (summary or "").strip() or "Replied"
    now = datetime.utcnow()
    changes = []
    inbound = models.OutreachMessage(
        lead_id=lead.id, channel=channel, direction="in", body=text, status="received",
        created_by="ui", created_at=now, updated_at=now)
    db.add(inbound)
    changes.append(change_log.record_create(db, inbound, f"Reply from {lead.name[:50]}: {text[:40]}"))
    changes += close_follow_ups(db, lead)
    before = change_log.snapshot(lead)
    if lead.status not in ("converted", "lost"):
        lead.status = "replied"
    lead.last_contact_at = now
    lead.notes = _merge_notes(lead.notes, f"- {date.today().isoformat()}: replied — {text}")
    lead.updated_at = now
    change = change_log.record_update(db, lead, before, f"{lead.name[:50]} replied")
    changes.append(change)
    if hasattr(models, "Communication"):
        import crm
        client = (db.query(models.Client).filter(models.Client.id == lead.client_id).first()
                  if lead.client_id else None)
        logged = crm.log_communication(
            db, client=client, lead=lead, channel=channel if channel in crm.CHANNELS else "other",
            direction="in", summary=text[:200], occurred_at=now, source="outreach",
            outreach_message_id=inbound.id)
        changes.append(logged["change"])
    return {**serialize(db, lead), "change": change, "changes": [c for c in changes if c],
            "message_id": inbound.id}


def convert(db: Session, lead: models.Lead) -> dict:
    """The lead became a client: a ``clients`` row linked both ways, status ``converted``,
    open follow-ups closed, and the lead's contact details as the primary contact.
    Idempotent: a converted lead returns its client. The caller commits."""
    if lead.client_id is not None:
        client = db.query(models.Client).filter(models.Client.id == lead.client_id).first()
        if client is not None:
            return {"lead": serialize(db, lead), "client": _client_dict(client), "changes": [],
                    "already_converted": True}
    last_sent = db.query(models.OutreachMessage).filter(
        models.OutreachMessage.lead_id == lead.id, models.OutreachMessage.status == "sent",
    ).order_by(models.OutreachMessage.id.desc()).first()
    now = datetime.utcnow()
    changes = []
    client = models.Client(
        name=lead.name, company=lead.company or "", status="active",
        service=(last_sent.brief or "")[:200] if last_sent is not None else "",
        notes="", lead_id=lead.id, currency="ZMW", created_at=now, updated_at=now)
    db.add(client)
    changes.append(change_log.record_create(db, client, f"New client: {lead.name[:60]}"))
    changes += close_follow_ups(db, lead)
    before = change_log.snapshot(lead)
    lead.client_id, lead.status, lead.updated_at = client.id, "converted", now
    changes.append(change_log.record_update(db, lead, before, f"Converted {lead.name[:60]} to a client"))
    if hasattr(models, "Contact") and (lead.email or lead.phone or lead.whatsapp):
        import crm
        contact = crm.add_contact(db, client, name=lead.name, role=lead.role, email=lead.email,
                                  phone=lead.phone, whatsapp=lead.whatsapp, is_primary=True)
        changes += contact["changes"]
    return {"lead": serialize(db, lead), "client": _client_dict(client),
            "changes": [c for c in changes if c]}


def _client_dict(client: models.Client) -> dict:
    return {"id": client.id, "name": client.name, "company": client.company,
            "status": client.status, "service": client.service, "lead_id": client.lead_id}


class ReplyBody(BaseModel):
    summary: str | None = None


@router.post("/leads/{lead_id}/replied")
def replied_endpoint(lead_id: int, payload: ReplyBody | None = None, db: Session = Depends(get_db)):
    lead = _get(db, lead_id)
    with change_log.batch_scope(db, "ui"):
        result = log_reply(db, lead, payload.summary if payload else None)
        db.commit()
    return result


@router.post("/leads/{lead_id}/convert")
def convert_endpoint(lead_id: int, db: Session = Depends(get_db)):
    lead = _get(db, lead_id)
    with change_log.batch_scope(db, "ui"):
        result = convert(db, lead)
        db.commit()
    return result
