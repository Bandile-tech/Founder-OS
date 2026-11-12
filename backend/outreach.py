"""
Outreach — drafted messages behind a human approval gate (Phase 5B).

Primitives: draft, request_send, execute_send, cancel, stats
Endpoints:  GET /outreach, GET /outreach/stats, GET /outreach/{id},
            POST /outreach/{id}/send, POST /outreach/{id}/cancel

    draft ──(tap Approve & send | "send it" → pending action → "yes")──► approved ──► sent | failed
      └──(tap Dismiss | "scrap it")──► cancelled

Nothing leaves without a person saying yes. The assistant can only *request* a send (a
pending action); the send happens on a tap, or on a confirmation that arrives in a later
message than the request. Every step is in the change log.
"""

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import change_log
import leads
import llm
import models
import outreach_channels
from database import get_db
import user_clock

router = APIRouter()

FOLLOW_UP_DAYS = 3
SENDABLE = ("draft", "approved")
_CONTEXT_DIR = Path(__file__).resolve().parent.parent / "context" / "core"

# Stable first: the voice rules never change between calls, so the prefix caches.
DRAFT_PROMPT = (
    "You write first-contact and follow-up messages for Bandile, a student founder in "
    "Zambia who builds AI and web automation for small businesses (proof of work: the "
    "Wamu's Bakes & Cakes website). Write as Bandile, in the first person.\n"
    "Rules: plain and specific, no hype, no flattery, no emojis, British English. One clear "
    "ask. At most 90 words for WhatsApp, LinkedIn and call notes; at most 150 words for "
    "email. Use only facts given to you about the lead; never invent a detail, a price or a "
    "mutual contact. For email, respond as JSON: {\"subject\": \"...\", \"body\": \"...\"}. For "
    "every other channel, respond with the message text only."
)


def _read(name: str) -> str:
    try:
        return (_CONTEXT_DIR / name).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _lead_of(db: Session, message: models.OutreachMessage) -> models.Lead | None:
    return db.query(models.Lead).filter(models.Lead.id == message.lead_id).first()


def _result(db: Session, message: models.OutreachMessage, **extra) -> dict:
    return {**leads.serialize_message(db, message, lead_name=True), **extra}


# ── Drafting ─────────────────────────────────────────────────

def draft(db: Session, lead: models.Lead, channel: str, brief: str | None = None, *,
          message_id: int | None = None, created_by: str = "assistant") -> dict:
    """Write a draft for ``lead`` (one model call). With ``message_id`` it is a revision:
    version n+1 that supersedes, and cancels, the previous draft. The caller commits.

    Raises ``llm.AIUnavailable`` when no key is configured, ``ValueError`` on bad input."""
    if channel not in outreach_channels.CHANNELS:
        raise ValueError(f"channel must be one of: {', '.join(outreach_channels.CHANNELS)}")
    previous = None
    if message_id is not None:
        previous = db.query(models.OutreachMessage).filter(
            models.OutreachMessage.id == int(message_id)).first()
        if previous is None or previous.lead_id != lead.id:
            raise ValueError(f"message {message_id} is not a draft for {lead.name}")

    qualification = leads._qualification(lead) or {}
    facts = {k: v for k, v in {
        "name": lead.name, "company": lead.company, "role": lead.role, "segment": lead.segment,
        "location": lead.location, "website": lead.website, "notes": lead.notes,
        "suggested_angle": qualification.get("suggested_angle"),
    }.items() if v}
    user = f"CHANNEL: {channel}\nLEAD: {json.dumps(facts, ensure_ascii=False)}\n"
    if previous is not None:
        user += (f"PREVIOUS DRAFT:\n{previous.body}\n\n"
                 f"REVISE IT AS FOLLOWS: {brief or 'tighten it'}\n")
    else:
        user += f"BRIEF: {brief or 'a short first introduction'}\n"
    messages = [
        {"role": "system", "content": DRAFT_PROMPT},
        {"role": "system", "content": f"IDEAL CUSTOMER:\n{_read('icp.md')}\n\nFOUNDER PROFILE:\n{_read('founder_profile.md')}"},
        {"role": "user", "content": user},
    ]
    kwargs = {"response_format": {"type": "json_object"}} if channel == "email" else {}
    raw = (llm.chat_text(messages, db=db, purpose="outreach_draft", max_tokens=400, **kwargs) or "").strip()
    subject, body = None, raw
    if channel == "email":
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = {}
        subject = (str(parsed.get("subject") or "")).strip() or None
        body = (str(parsed.get("body") or "")).strip()
    if not body:
        raise ValueError("the draft came back empty — try again")

    changes = []
    if previous is not None and previous.status in SENDABLE:
        before = change_log.snapshot(previous)
        previous.status = "cancelled"
        previous.updated_at = datetime.utcnow()
        _close_pending(db, previous)
        changes.append(change_log.record_update(db, previous, before, f"Replaced draft to {lead.name[:50]}"))
    message = models.OutreachMessage(
        lead_id=lead.id, channel=channel, direction="out", subject=subject, body=body,
        status="draft", version=(previous.version or 1) + 1 if previous is not None else 1,
        supersedes_id=previous.id if previous is not None else None,
        brief=(brief or "").strip() or (previous.brief if previous is not None else None),
        created_by=created_by, created_at=datetime.utcnow(), updated_at=datetime.utcnow())
    db.add(message)
    label = {"whatsapp": "WhatsApp", "email": "email", "linkedin": "LinkedIn message"}.get(channel, channel)
    change = change_log.record_create(db, message, f"Drafted {label} to {lead.name[:50]}")
    return _result(db, message, change=change, changes=[c for c in changes + [change] if c])


# ── The approval gate ────────────────────────────────────────

def _close_pending(db: Session, message: models.OutreachMessage, status: str = "cancelled") -> None:
    if message.pending_action_id is None:
        return
    p = db.query(models.PendingAction).filter(models.PendingAction.id == message.pending_action_id).first()
    if p is not None and p.status == "pending":
        p.status = status
        p.resolved_at = datetime.utcnow()


def _latest_user_message(db: Session):
    return db.query(models.ChatMessage).filter(
        models.ChatMessage.role == "user").order_by(models.ChatMessage.id.desc()).first()


def _latest_user_message_id(db: Session) -> int:
    row = _latest_user_message(db)
    return row.id if row else 0


def _is_newest_proposal(db: Session, pending: models.PendingAction) -> bool:
    """True when nothing has been proposed since: a yes answers the last thing asked."""
    newest = db.query(models.PendingAction.id).order_by(models.PendingAction.id.desc()).first()
    return newest is not None and newest[0] == pending.id


def approval_text(db: Session, message: models.OutreachMessage) -> str:
    """What Bandile is being asked to approve, written by the code and not by the model:
    the channel, who it goes to, and the whole text."""
    lead = _lead_of(db, message)
    to = outreach_channels.recipient(lead, message.channel)
    who = (lead.name if lead else "the lead") + (f" <{to}>" if to else "")
    label = {"whatsapp": "WhatsApp", "email": "Email", "linkedin": "LinkedIn message"}.get(
        message.channel, message.channel.capitalize())
    lines = [f"Waiting for your yes. Nothing has been sent.", f"{label} to {who}"]
    if message.subject:
        lines.append(f"Subject: {message.subject}")
    lines.append(message.body or "")
    return "\n".join(lines)


def request_send(db: Session, message: models.OutreachMessage) -> dict:
    """Ask for approval to send. Creates a pending action (or returns the open one while it
    is still the last thing proposed). Sends nothing. The recipient as it stands now is
    recorded: the approval is for this text to this address, and nothing else."""
    import actions
    if message.status not in SENDABLE:
        raise ValueError(f"this message is {message.status}; only a draft can be sent")
    lead = _lead_of(db, message)
    if message.pending_action_id is not None:
        existing = db.query(models.PendingAction).filter(
            models.PendingAction.id == message.pending_action_id).first()
        if existing is not None and existing.status == "pending":
            if _is_newest_proposal(db, existing):
                return actions._pending_dict(existing)
            # Something else has been proposed since. Ask afresh, so that the yes which
            # follows is plainly about this send.
            existing.status, existing.resolved_at = "cancelled", datetime.utcnow()
    p = models.PendingAction(
        created_at=datetime.utcnow(), kind="send_outreach", table_name="outreach_messages",
        record_id=message.id,
        summary=f"Send {message.channel} to {lead.name if lead else 'lead'}: {(message.subject or message.body)[:60]}",
        payload_json=json.dumps({"message_id": message.id,
                                 "after_message_id": _latest_user_message_id(db),
                                 "to": outreach_channels.recipient(lead, message.channel)}),
        status="pending",
        expires_at=datetime.utcnow() + timedelta(hours=actions.PENDING_TTL_HOURS),
        batch_id=change_log.current_batch_id(db),
    )
    db.add(p)
    db.flush()
    message.pending_action_id = p.id
    db.commit()
    return actions._pending_dict(p)


_YES = re.compile(
    r"^\W*(yes|yep|yeah|yup|ok|okay|sure|confirm|confirmed|approve|approved|go ahead|go for it|do it|"
    r"please do|proceed|send it|send)\b", re.I)
_NOT_A_YES = re.compile(
    r"\b(no|not|don'?t|do not|wait|but|stop|cancel|change|edit|instead|first|later|hold)\b|\?", re.I)


def reads_as_yes(text: str | None) -> bool:
    """A short, plain yes: "yes", "ok send it", "go ahead". Anything longer, hedged, asked
    as a question or typed as a command is not one. Deliberately strict: when in doubt the
    answer is no, and Bandile can tap Approve & send."""
    said = (text or "").strip()
    if not said or said.startswith("/") or len(said) > 60:
        return False
    found = _YES.match(said)
    if not found or _NOT_A_YES.search(said):
        return False
    # "send" on its own is a yes; "send the report to Wamu" is a new instruction.
    return found.group(1).lower() != "send" or len(said.split()) <= 2


def confirmed_in_a_later_message(db: Session, pending: models.PendingAction) -> bool:
    """True when the last thing Bandile said is a plain yes, said after the send was asked
    for. A request and its confirmation in one turn is the model approving its own proposal;
    a later message about something else is not an approval either."""
    try:
        after = int(json.loads(pending.payload_json or "{}").get("after_message_id") or 0)
    except (ValueError, TypeError):
        after = 0
    last = _latest_user_message(db)
    return last is not None and last.id > after and reads_as_yes(last.content)


def execute_send(db: Session, message: models.OutreachMessage) -> dict:
    """Send an approved message through its channel. Only ever called after a human
    approval (the tap endpoint, or a confirmed pending action). The caller commits.

    sent: stamps the lead as contacted and creates the follow-up task (+3 days).
    failed: records the error; the lead is not touched."""
    if message.status not in SENDABLE:
        raise ValueError(f"this message is {message.status}; only a draft can be sent")
    lead = _lead_of(db, message)
    if lead is None:
        raise ValueError("the lead for this message no longer exists")
    now = datetime.utcnow()
    before = change_log.snapshot(message)
    # Claim the message before anything leaves: one UPDATE that only succeeds while it is
    # still a draft, committed so that another approval arriving now (a tap on a second
    # device, /yes beside a tap) finds it taken and stops. Without this both would send.
    claimed = db.query(models.OutreachMessage).filter(
        models.OutreachMessage.id == message.id, models.OutreachMessage.status.in_(SENDABLE),
    ).update({models.OutreachMessage.status: "sending", models.OutreachMessage.approved_at: now},
             synchronize_session=False)
    db.commit()
    if claimed != 1:
        raise ValueError("this message is already being sent")
    db.refresh(message)
    try:
        outcome = outreach_channels.get_channel(message.channel).send(db, message, lead)
    except Exception as exc:  # noqa: BLE001 — a channel that raises may or may not have sent
        message.status, message.error, message.updated_at = "failed", f"{type(exc).__name__}"[:200], now
        db.commit()
        raise
    changes, extra = [], {}

    if not outcome.get("ok"):
        message.status, message.error, message.updated_at = "failed", outcome.get("error") or "send failed", now
        _close_pending(db, message, "confirmed")
        changes.append(change_log.record_update(
            db, message, before, f"Could not send {message.channel} to {lead.name[:40]}"))
        return _result(db, message, sent=False, error=message.error, undoable=True,
                       changes=[c for c in changes if c])

    # An email that went out, or a WhatsApp message that was approved and opened, has left:
    # nothing recorded about it may be undone. A manual channel sent nothing itself.
    left = message.channel in outreach_channels.EXTERNAL
    with change_log.irreversible(db, left):
        message.status, message.sent_at, message.error, message.updated_at = "sent", now, None, now
        if outcome.get("external_id"):
            message.external_id = outcome["external_id"]
        _close_pending(db, message, "confirmed")
        changes.append(change_log.record_update(db, message, before, f"Sent {message.channel} to {lead.name[:50]}"))
        db.commit()         # it has left: that fact is kept even if the bookkeeping below fails

        lead_before = change_log.snapshot(lead)
        lead.last_contact_at = now
        if lead.status in ("new", "qualified", "disqualified"):
            lead.status = "contacted"
        follow = leads.open_follow_up(db, lead.id)
        if follow is None:
            follow, follow_change = leads.set_follow_up(db, lead, user_clock.today() + timedelta(days=FOLLOW_UP_DAYS))
            changes.append(follow_change)
        lead.updated_at = now
        changes.append(change_log.record_update(db, lead, lead_before, f"Contacted {lead.name[:60]}"))

        if hasattr(models, "Communication"):
            import crm
            logged = crm.log_communication(
                db, lead=lead, channel=message.channel if message.channel in crm.CHANNELS else "other",
                direction="out", summary=(message.subject or message.body)[:200], occurred_at=now,
                source="outreach", outreach_message_id=message.id)
            changes.append(logged["change"])

    if outcome.get("open_url"):
        extra["open_url"] = outcome["open_url"]
    if left:
        extra["undo_note"] = change_log.NOT_UNDOABLE
    return _result(db, message, sent=True, follow_up_todo_id=follow.id, undoable=not left,
                   changes=[c for c in changes if c], **extra)


def execute_pending(db: Session, pending: models.PendingAction) -> dict:
    """Executor for a confirmed ``send_outreach`` pending action (see actions._EXECUTORS)."""
    message = db.query(models.OutreachMessage).filter(
        models.OutreachMessage.id == pending.record_id).first()
    if message is None:
        return {"executed": False, "reason": "the message no longer exists", "status": "cancelled"}
    if message.status not in SENDABLE:
        return {"executed": False, "reason": f"the message is already {message.status}", "status": "cancelled"}
    # The yes was for this text to the address shown when the send was asked for.
    try:
        approved_to = json.loads(pending.payload_json or "{}").get("to")
    except (ValueError, TypeError):
        approved_to = None
    current_to = outreach_channels.recipient(_lead_of(db, message), message.channel)
    if approved_to and approved_to != current_to:
        return {"executed": False, "status": "cancelled",
                "reason": "the recipient changed after this send was requested; ask for the send again"}
    with change_log.batch_scope(db, "assistant"):
        try:
            result = execute_send(db, message)
        except ValueError as exc:
            return {"executed": False, "reason": str(exc), "status": "cancelled"}
    if not result["sent"]:
        return {"executed": False, "reason": result["error"], "status": "confirmed", "message": result}
    out = {"executed": True, "status": "confirmed", "message": result, "changes": result["changes"],
           "follow_up_todo_id": result["follow_up_todo_id"], "undoable": result["undoable"]}
    if result.get("open_url"):
        out["open_url"] = result["open_url"]
    if result.get("undo_note"):
        out["undo_note"] = result["undo_note"]
    return out


def cancel(db: Session, message: models.OutreachMessage) -> dict:
    """Scrap a draft. The caller commits."""
    if message.status not in SENDABLE:
        raise ValueError(f"this message is {message.status}; only a draft can be dismissed")
    lead = _lead_of(db, message)
    before = change_log.snapshot(message)
    message.status, message.updated_at = "cancelled", datetime.utcnow()
    _close_pending(db, message)
    change = change_log.record_update(
        db, message, before, f"Dismissed draft to {(lead.name if lead else 'lead')[:50]}")
    return _result(db, message, change=change)


# ── Stats ────────────────────────────────────────────────────

def _rate(part: int, whole: int) -> float:
    return round(part / whole, 3) if whole else 0.0


def stats(db: Session, days: int = 90) -> dict:
    """Who was contacted in the last ``days``, who replied and who converted, by segment
    and by channel. A lead counts as contacted when a message to it was sent in the window;
    as replied when it then answered (status replied or converted, or an inbound message);
    as converted when it became a client. Rates are replied ÷ contacted and
    converted ÷ contacted, and 0 when nobody was contacted."""
    since = datetime.utcnow() - timedelta(days=max(1, int(days)))
    all_leads = {l.id: l for l in db.query(models.Lead).all()}
    sent = db.query(models.OutreachMessage).filter(
        models.OutreachMessage.direction == "out", models.OutreachMessage.status == "sent",
        models.OutreachMessage.sent_at >= since).all()
    answered = {m.lead_id for m in db.query(models.OutreachMessage).filter(
        models.OutreachMessage.direction == "in").all()}

    def replied(lead):
        return lead.status in ("replied", "converted") or lead.id in answered

    def row(contacted_ids, **head):
        contacted = [all_leads[i] for i in contacted_ids if i in all_leads]
        n_replied = sum(1 for l in contacted if replied(l))
        n_converted = sum(1 for l in contacted if l.status == "converted")
        return {**head, "contacted": len(contacted), "replied": n_replied, "converted": n_converted,
                "reply_rate": _rate(n_replied, len(contacted)),
                "conversion_rate": _rate(n_converted, len(contacted))}

    contacted_ids = {m.lead_id for m in sent}
    segments = {}
    for lead in all_leads.values():
        segments.setdefault(lead.segment, []).append(lead.id)
    by_segment = [row([i for i in ids if i in contacted_ids], segment=segment, leads=len(ids))
                  for segment, ids in segments.items()]
    by_segment.sort(key=lambda r: (-r["contacted"], -r["leads"], r["segment"] or ""))
    channels = {}
    for m in sent:
        channels.setdefault(m.channel, {"ids": set(), "sent": 0})
        channels[m.channel]["ids"].add(m.lead_id)
        channels[m.channel]["sent"] += 1
    by_channel = [row(info["ids"], channel=channel, sent=info["sent"]) for channel, info in channels.items()]
    by_channel.sort(key=lambda r: (-r["contacted"], r["channel"]))
    return {"days": int(days), "by_segment": by_segment, "by_channel": by_channel,
            "totals": row(contacted_ids, leads=len(all_leads), sent=len(sent))}


# ── Endpoints ────────────────────────────────────────────────

def _get(db: Session, message_id: int) -> models.OutreachMessage:
    message = db.query(models.OutreachMessage).filter(models.OutreachMessage.id == message_id).first()
    if message is None:
        raise HTTPException(404, "Message not found")
    return message


@router.get("/outreach")
def list_outreach(status: str | None = None, lead_id: int | None = None, db: Session = Depends(get_db)):
    q = db.query(models.OutreachMessage)
    if status:
        q = q.filter(models.OutreachMessage.status.in_([s.strip() for s in status.split(",") if s.strip()]))
    if lead_id is not None:
        q = q.filter(models.OutreachMessage.lead_id == lead_id)
    return [_result(db, m) for m in q.order_by(models.OutreachMessage.id.desc()).limit(200).all()]


@router.get("/outreach/stats")
def outreach_stats(days: int = 90, db: Session = Depends(get_db)):
    return stats(db, days)


# NOTE: any fixed /outreach/<word> route must be declared above this one.
@router.get("/outreach/{message_id}")
def get_outreach(message_id: int, db: Session = Depends(get_db)):
    return _result(db, _get(db, message_id))


@router.post("/outreach/{message_id}/send")
def send_outreach(message_id: int, db: Session = Depends(get_db)):
    """The Approve & send tap: a human tap is the approval, so this sends in one step."""
    message = _get(db, message_id)
    if message.status not in SENDABLE:
        raise HTTPException(409, f"This message is {message.status}; only a draft can be sent")
    with change_log.batch_scope(db, "ui"):
        try:
            result = execute_send(db, message)
        except ValueError as exc:
            db.rollback()
            raise HTTPException(409, str(exc))
        db.commit()
    hidden = ("changes", "open_url", "follow_up_todo_id", "undoable", "undo_note")
    out = {"message": {k: v for k, v in result.items() if k not in hidden},
           "changes": result["changes"], "undoable": result["undoable"]}
    if result.get("undo_note"):
        out["undo_note"] = result["undo_note"]
    if result.get("open_url"):
        out["open_url"] = result["open_url"]
    if result.get("follow_up_todo_id"):
        out["follow_up_todo_id"] = result["follow_up_todo_id"]
    return out


@router.post("/outreach/{message_id}/cancel")
def cancel_outreach(message_id: int, db: Session = Depends(get_db)):
    message = _get(db, message_id)
    with change_log.batch_scope(db, "ui"):
        try:
            result = cancel(db, message)
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        db.commit()
    return result
