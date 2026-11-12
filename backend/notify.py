"""
Notifications — the system reaching out while the app is closed (Phase 6D, ADR-0002).

Primitives: settings, is_quiet, sent_today, queue, dispatch, render_briefing,
            render_evening, send_test, split_message, TelegramChannel
Endpoints:  GET/PATCH /notifications/settings, GET /notifications,
            POST /notifications/test, POST /notifications/brief-now,
            POST /notifications/{id}/cancel

Nothing sends directly. ``queue()`` writes a row; ``dispatch()`` decides what happens to
each due row: suppressed when Telegram is not paired or the channel is off, held through
quiet hours, suppressed past the daily cap, otherwise sent — retried on the next dispatch
and marked failed after three attempts. One channel today: a Telegram bot.
"""

import json
import os
import re
import threading
from datetime import date, datetime, timedelta

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import integrations
import integrations_telegram
import models
from agents import schedule
from database import get_db

router = APIRouter()

KINDS = ("briefing", "evening", "alert", "deadline", "reminder", "reply", "test")
SEVERITIES = ("info", "warn", "critical")
CAP_EXEMPT = ("briefing", "reply", "test", "reminder")     # never counted against, or stopped by, the daily cap
QUIET_EXEMPT = ("reply", "test", "reminder")               # sent inside quiet hours
ALWAYS_ON = ("reply", "test")                              # sent even with notifications switched off
MAX_ATTEMPTS = 3
MESSAGE_LIMIT = 4000            # VERIFY: Telegram's sendMessage limit is 4,096 characters
BRIEFING_LIMIT = 3500
MAX_ATTENTION = 6
MAX_TODAY = 5
TIMEOUT = 10
API = "https://api.telegram.org"

DEFAULTS = {"enabled": True, "morning_time": "06:00", "evening_time": None, "quiet_start": "22:00",
            "quiet_end": "05:00", "alert_min_severity": "warn", "daily_cap": 12,
            "channels_json": '{"telegram": true}', "deadline_days_json": "[30, 14, 7, 3, 1]"}

# One process serves the app, so a lock is enough to stop two dispatches (the tick and a
# reply, say) from sending the same row twice.
_dispatch_lock = threading.Lock()


# ── Settings ─────────────────────────────────────────────────

def settings(db: Session) -> models.NotificationSettings:
    """Row 1, created with the defaults when it is missing."""
    row = db.query(models.NotificationSettings).filter(models.NotificationSettings.id == 1).first()
    if row is None:
        row = models.NotificationSettings(id=1, updated_at=datetime.utcnow(), **DEFAULTS)
        try:
            with db.begin_nested():             # a savepoint: losing the race undoes only this insert
                db.add(row)
                db.flush()
        except IntegrityError:
            row = db.query(models.NotificationSettings).filter(models.NotificationSettings.id == 1).first()
        else:
            db.commit()
    return row


def _channels(s) -> dict:
    try:
        data = json.loads(s.channels_json) if s.channels_json else {}
    except (ValueError, TypeError):
        data = {}
    return data if isinstance(data, dict) else {}


def deadline_days(s) -> list[int]:
    try:
        marks = [int(d) for d in json.loads(s.deadline_days_json or "[]")]
    except (ValueError, TypeError):
        marks = []
    return sorted({m for m in marks if m >= 0}, reverse=True) or [30, 14, 7, 3, 1]


def serialize_settings(db: Session, s) -> dict:
    return {
        "enabled": bool(s.enabled), "morning_time": s.morning_time, "evening_time": s.evening_time,
        "quiet_start": s.quiet_start, "quiet_end": s.quiet_end, "alert_min_severity": s.alert_min_severity,
        "daily_cap": s.daily_cap, "channels": _channels(s), "deadline_days": deadline_days(s),
        "muted_until": s.muted_until, "updated_at": s.updated_at,
        "paired": integrations_telegram.is_paired(db),
        # Only while there is something to pair: a code on show after pairing is a code to steal.
        "pair_code": None if integrations_telegram.is_paired(db) else integrations_telegram.pair_code(db),
        "bot_link": integrations_telegram.bot_link(),
        "channel_configured": bool(integrations_telegram.bot_token()),
    }


def _clean_time(value, *, optional: bool = False):
    """'5:45' → '05:45'. With ``optional``, an empty value or "off" means None."""
    if value is None or (isinstance(value, str) and value.strip().lower() in ("", "off", "none", "null")):
        if optional:
            return None
        raise ValueError("a time is needed, like 06:00")
    parsed = schedule.parse_time(str(value).strip().replace(".", ":"))
    if parsed is None:
        raise ValueError(f"'{value}' is not a time like 06:00")
    return f"{parsed[0]:02d}:{parsed[1]:02d}"


def update_settings(db: Session, fields: dict, source: str | None = None) -> dict:
    """PATCH semantics, logged. Raises ValueError on a bad value. The caller commits."""
    import change_log
    s = settings(db)
    before = change_log.snapshot(s)
    said = []
    if fields.get("enabled") is not None:
        s.enabled = bool(fields["enabled"])
        said.append("notifications " + ("on" if s.enabled else "off"))
    if "morning_time" in fields and fields["morning_time"] is not None:
        s.morning_time = _clean_time(fields["morning_time"])
        said.append(f"morning briefing at {s.morning_time}")
    if "evening_time" in fields:
        s.evening_time = _clean_time(fields["evening_time"], optional=True)
        said.append(f"evening review at {s.evening_time}" if s.evening_time else "evening review off")
    for key, label in (("quiet_start", "quiet hours from"), ("quiet_end", "quiet hours until")):
        if key in fields and fields[key] is not None:
            setattr(s, key, _clean_time(fields[key]))
            said.append(f"{label} {getattr(s, key)}")
    if fields.get("alert_min_severity") is not None:
        severity = str(fields["alert_min_severity"]).strip().lower()
        if severity not in SEVERITIES:
            raise ValueError("alert severity must be info, warn or critical")
        s.alert_min_severity = severity
        said.append(f"alerts from {severity} up")
    if fields.get("daily_cap") is not None:
        try:
            cap = int(fields["daily_cap"])
        except (ValueError, TypeError):
            raise ValueError("the daily cap must be a number")
        if not 0 <= cap <= 100:
            raise ValueError("the daily cap must be between 0 and 100")
        s.daily_cap = cap
        said.append(f"at most {cap} a day")
    if not said:
        return {"settings": serialize_settings(db, s), "change": None, "summary": "nothing changed"}
    s.updated_at = datetime.utcnow()
    summary = "Notifications: " + ", ".join(said)
    change = change_log.record_update(db, s, before, summary, source)
    return {"settings": serialize_settings(db, s), "change": change, "summary": summary}


# ── Quiet hours and the cap ──────────────────────────────────

def _in_window(now_local: datetime, s) -> bool:
    start, end = schedule.parse_time(s.quiet_start), schedule.parse_time(s.quiet_end)
    if start is None or end is None or start == end:
        return False
    t = (now_local.hour, now_local.minute)
    return (start <= t < end) if start < end else (t >= start or t < end)


def is_quiet(now_local: datetime, s) -> bool:
    """Inside the quiet window (which may cross midnight) or muted."""
    if s.muted_until is not None and schedule.to_utc(now_local) < s.muted_until:
        return True
    return _in_window(now_local, s)


def quiet_ends(now_local: datetime, s) -> datetime:
    """Naive UTC: when the current quiet stretch (window and mute) is over."""
    until = schedule.to_utc(now_local)
    if _in_window(now_local, s):
        end = schedule.parse_time(s.quiet_end)
        candidate = now_local.replace(hour=end[0], minute=end[1], second=0, microsecond=0)
        if candidate <= now_local:
            candidate += timedelta(days=1)
        until = schedule.to_utc(candidate)
    if s.muted_until is not None and s.muted_until > until:
        until = s.muted_until
    return until


def _day_bounds(today: date) -> tuple[datetime, datetime]:
    start_local = datetime(today.year, today.month, today.day, tzinfo=schedule.tz())
    start = schedule.to_utc(start_local)
    return start, start + timedelta(days=1)


def sent_today(db: Session, today: date) -> int:
    """Messages sent on the local day that count against the cap."""
    start, end = _day_bounds(today)
    return db.query(models.Notification).filter(
        models.Notification.status == "sent", models.Notification.sent_at >= start,
        models.Notification.sent_at < end, ~models.Notification.kind.in_(("reply", "test"))).count()


# ── Queue ────────────────────────────────────────────────────

def serialize(n: models.Notification) -> dict:
    return {"id": n.id, "created_at": n.created_at, "channel": n.channel, "kind": n.kind, "title": n.title,
            "body": n.body, "dedupe_key": n.dedupe_key, "send_at": n.send_at, "status": n.status,
            "sent_at": n.sent_at, "error": n.error, "source": n.source, "agent_run_id": n.agent_run_id,
            "suppressed_reason": n.suppressed_reason}


def _by_key(db: Session, dedupe_key: str):
    return db.query(models.Notification).filter(models.Notification.dedupe_key == dedupe_key).first()


def queue(db: Session, *, kind: str, body: str, title: str | None = None, dedupe_key: str | None = None,
          send_at: datetime | None = None, source: str | None = None, agent_run_id: int | None = None) -> dict:
    """Add a notification to the queue. Never sends. A ``dedupe_key`` already used returns
    the existing row with ``deduped: True``. ``send_at`` is naive UTC (None = now). The
    caller owns the transaction."""
    if kind not in KINDS:
        return {"error": f"unknown notification kind '{kind}'"}
    text = str(body or "").strip()
    if not text:
        return {"error": "a notification needs a body"}
    if dedupe_key:
        existing = _by_key(db, dedupe_key)
        if existing is not None:
            return {**serialize(existing), "deduped": True}
    row = models.Notification(
        channel="telegram", kind=kind, title=(str(title).strip()[:200] if title else None), body=text,
        dedupe_key=dedupe_key, send_at=send_at, status="queued", source=source, created_at=datetime.utcnow(),
        agent_run_id=agent_run_id if agent_run_id is not None else db.info.get("agent_run_id"))
    # The insert runs inside a savepoint: if another request queued the same key first, only
    # this insert is rolled back. The caller's own uncommitted work is never touched.
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        existing = _by_key(db, dedupe_key) if dedupe_key else None
        if existing is None:
            raise
        return {**serialize(existing), "deduped": True}
    return {**serialize(row), "deduped": False}


# ── The channel ──────────────────────────────────────────────

def split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """A long message as several, broken on blank lines (then on lines, then hard)."""
    text = str(text or "").strip()
    if len(text) <= limit:
        return [text] if text else []
    pieces = []
    for paragraph in re.split(r"\n\s*\n", text):
        while len(paragraph) > limit:            # one paragraph too long on its own
            cut = paragraph.rfind("\n", 0, limit)
            cut = cut if cut > 0 else limit
            pieces.append(paragraph[:cut].strip())
            paragraph = paragraph[cut:].strip()
        if paragraph:
            pieces.append(paragraph)
    parts, current = [], ""
    for piece in pieces:
        if current and len(current) + 2 + len(piece) > limit:
            parts.append(current)
            current = piece
        else:
            current = f"{current}\n\n{piece}" if current else piece
    if current:
        parts.append(current)
    return parts


class TelegramChannel:
    name = "telegram"

    @staticmethod
    def configured() -> bool:
        return bool(integrations_telegram.bot_token())

    @staticmethod
    def send(chat_id, text: str) -> dict:
        """Post the message (in parts when long). Never raises."""
        token = integrations_telegram.bot_token()
        if not token:
            return {"ok": False, "external_id": None, "error": "TELEGRAM_BOT_TOKEN is not set"}
        external_id = None
        for part in split_message(text):
            try:
                # VERIFY: sendMessage's parameters (chat_id, text, disable_web_page_preview).
                resp = httpx.post(f"{API}/bot{token}/sendMessage",
                                  json={"chat_id": chat_id, "text": part, "disable_web_page_preview": True},
                                  timeout=TIMEOUT)
                data = resp.json()
            except Exception as exc:             # noqa: BLE001 — a failed send is an outcome
                return {"ok": False, "external_id": external_id,
                        "error": integrations.redact(f"{type(exc).__name__}: {exc}")[:300]}
            if not (isinstance(data, dict) and data.get("ok")):
                description = data.get("description") if isinstance(data, dict) else None
                return {"ok": False, "external_id": external_id,
                        "error": integrations.redact(description or f"HTTP {getattr(resp, 'status_code', '?')}")[:300]}
            if external_id is None:
                external_id = str((data.get("result") or {}).get("message_id") or "") or None
        return {"ok": True, "external_id": external_id, "error": None}


def _text(n: models.Notification) -> str:
    if n.title and n.kind in ("alert", "deadline", "reminder"):
        return f"{n.title}\n{n.body}"
    return n.body


def _attempts(n: models.Notification) -> int:
    m = re.match(r"attempt (\d+)/", n.error or "")
    return int(m.group(1)) if m else 0


# ── Dispatch ─────────────────────────────────────────────────

def dispatch(db: Session, now: datetime | None = None, *, force: bool = False, only: int | None = None) -> dict:
    """Decide every due queued row. ``now`` is naive UTC. ``force`` sends through quiet
    hours; ``only`` limits the pass to one notification id. Commits as it goes."""
    now = now or datetime.utcnow()
    now_local = schedule.to_local(now)
    out = {"sent": 0, "held": 0, "suppressed": 0, "failed": 0, "retrying": 0}
    with _dispatch_lock:
        s = settings(db)
        query = db.query(models.Notification).filter(models.Notification.status == "queued")
        if only is not None:
            query = query.filter(models.Notification.id == only)
        rows = [n for n in query.order_by(models.Notification.id).all() if n.send_at is None or n.send_at <= now]
        if not rows:
            return out
        chat = integrations_telegram.chat_id(db)
        channel_on = bool(_channels(s).get("telegram", True))
        quiet = is_quiet(now_local, s)
        count = sent_today(db, now_local.date())
        cap = s.daily_cap if s.daily_cap is not None else DEFAULTS["daily_cap"]

        for n in rows:
            reason = None
            if chat is None:
                reason = "not_paired"
            elif not channel_on or not TelegramChannel.configured() or (not s.enabled and n.kind not in ALWAYS_ON):
                reason = "channel_disabled"
            if reason is None and quiet and not force and n.kind not in QUIET_EXEMPT:
                n.send_at = quiet_ends(now_local, s)
                out["held"] += 1
                db.commit()
                continue
            if reason is None and n.kind not in CAP_EXEMPT and count >= cap:
                reason = "daily_cap"
            if reason is not None:
                n.status, n.suppressed_reason = "suppressed", reason
                out["suppressed"] += 1
                db.commit()
                continue

            result = TelegramChannel.send(chat, _text(n))
            if result["ok"]:
                n.status, n.sent_at, n.external_id, n.error = "sent", now, result["external_id"], None
                out["sent"] += 1
                if n.kind not in ("reply", "test"):
                    count += 1
            else:
                attempt = _attempts(n) + 1
                n.error = f"attempt {attempt}/{MAX_ATTEMPTS}: {integrations.redact(result['error'], db)}"[:500]
                if attempt >= MAX_ATTEMPTS:
                    n.status = "failed"
                    out["failed"] += 1
                else:
                    out["retrying"] += 1
            db.commit()
    return out


# ── The briefing as text ─────────────────────────────────────

_NOT_ATTENTION = ("non_negotiable", "exam", "event", "syllabus", "opportunity", "content")


def _clip(text, limit: int = 110) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _long_date(iso: str | None) -> str:
    try:
        d = date.fromisoformat(str(iso))
    except (ValueError, TypeError):
        return ""
    return f"{d.strftime('%A')} {d.day} {d.strftime('%B')}"


def _attention_lines(briefing: dict) -> tuple[list[str], int]:
    items = [i for i in briefing.get("items") or [] if i.get("category") not in _NOT_ATTENTION
             and (i.get("score") or 0) >= 40]
    lines = []
    for item in items[:MAX_ATTENTION]:
        label = item.get("due_label") or (item.get("category") or "").capitalize()
        lines.append(f"• {label} — {_clip(item.get('title'))}" if label else f"• {_clip(item.get('title'))}")
    return lines, len(items)


def _today_lines(briefing: dict) -> list[str]:
    lines = []
    for item in briefing.get("items") or []:
        if item.get("category") == "event" and len(lines) < MAX_TODAY - 1:
            lines.append(f"• {item.get('due_label') or ''} {_clip(item.get('title'))} (calendar)".replace("•  ", "• "))
    counts = briefing.get("counts") or {}
    total = counts.get("non_negotiables_total") or 0
    if total:
        lines.append(f"• Non-negotiables: {counts.get('non_negotiables_done') or 0}/{total}")
    return lines


def _exam_line(briefing: dict) -> str:
    parts = []
    for exam in (briefing.get("exams") or [])[:3]:
        days = exam.get("days_away")
        if days is None or days < 0:
            continue
        name = f"{exam.get('subject') or exam.get('subject_code')}{' ' + exam['paper'] if exam.get('paper') else ''}"
        parts.append(f"{name} " + ("today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"))
    return "EXAMS  " + " · ".join(parts) if parts else ""


def render_briefing(briefing: dict, *, suppressed_overnight: list[dict]) -> str:
    """The morning message: plain text, at most 3,500 characters, empty sections left out."""
    greeting = briefing.get("greeting") or "Good morning"
    head = f"{greeting}. {_long_date(briefing.get('date'))} · {briefing.get('badge') or ''}".rstrip(" ·")
    blocks = [head + ("\n" + _clip(briefing["greeting_line"], 600) if briefing.get("greeting_line") else "")]

    attention, total = _attention_lines(briefing)
    if attention:
        blocks.append(f"ATTENTION ({total})\n" + "\n".join(attention))
    today = _today_lines(briefing)
    if today:
        blocks.append("TODAY\n" + "\n".join(today))
    exams = _exam_line(briefing)
    if exams:
        blocks.append(exams)
    overnight = [_clip(n.get("body") or n.get("title")) for n in suppressed_overnight or []]
    overnight = [line for line in overnight if line]
    if overnight:
        shown = overnight[:5]
        more = f" (+{len(overnight) - len(shown)} more)" if len(overnight) > len(shown) else ""
        blocks.append("OVERNIGHT (suppressed): " + " ".join(f"• {line}" for line in shown) + more)
    blocks.append("Reply here to log anything. /brief resend · /undo last change")
    text = "\n\n".join(blocks)
    return text if len(text) <= BRIEFING_LIMIT else text[:BRIEFING_LIMIT - 1].rstrip() + "…"


def render_evening(briefing: dict) -> str:
    """The optional evening message: what is still open, and one question."""
    blocks = [f"Evening review. {_long_date(briefing.get('date'))}"]
    carried = [i for i in briefing.get("items") or [] if i.get("category") == "task"
               and i.get("target_type") == "todo" and (i.get("score") or 0) >= 70]
    if carried:
        lines = [f"• {i.get('due_label') or 'Open'} — {_clip(i.get('title'))}" for i in carried[:MAX_ATTENTION]]
        blocks.append(f"STILL OPEN ({len(carried)})\n" + "\n".join(lines))
    counts = briefing.get("counts") or {}
    if counts.get("non_negotiables_total"):
        blocks.append(f"Non-negotiables: {counts.get('non_negotiables_done') or 0}/{counts['non_negotiables_total']}")
    blocks.append("What did you get done? Reply here.")
    text = "\n\n".join(blocks)
    return text if len(text) <= BRIEFING_LIMIT else text[:BRIEFING_LIMIT - 1].rstrip() + "…"


def suppressed_overnight(db: Session, now: datetime | None = None) -> list[dict]:
    """What quiet hours or the cap stopped in the last 24 hours."""
    now = now or datetime.utcnow()
    rows = db.query(models.Notification).filter(
        models.Notification.status == "suppressed",
        models.Notification.suppressed_reason.in_(("quiet_hours", "daily_cap")),
        models.Notification.created_at >= now - timedelta(hours=24)).order_by(models.Notification.id).all()
    return [serialize(n) for n in rows]


def briefing_text(db: Session, now: datetime | None = None) -> str:
    """Today's briefing as the Telegram message (reuses the cached daily summary)."""
    import briefing
    now = now or datetime.utcnow()
    local = schedule.to_local(now).replace(tzinfo=None)
    data = briefing.build_briefing(db, now=local, with_summary=True)
    return render_briefing(data, suppressed_overnight=suppressed_overnight(db, now))


def delivery(db: Session, today: date) -> dict:
    """For the home screen: when today's briefing went out, if it did."""
    start, end = _day_bounds(today)
    last = db.query(models.Notification).filter(
        models.Notification.kind == "briefing", models.Notification.status == "sent",
        models.Notification.sent_at >= start, models.Notification.sent_at < end).order_by(
        models.Notification.sent_at.desc()).first()
    return {"last_sent_at": last.sent_at if last else None, "channel": "telegram",
            "paired": integrations_telegram.is_paired(db)}


# ── One-off sends ────────────────────────────────────────────

def _outcome(db: Session, notification_id: int) -> dict:
    n = db.query(models.Notification).filter(models.Notification.id == notification_id).first()
    error = n.error or ({"not_paired": "Telegram is not paired yet",
                         "channel_disabled": "The Telegram channel is off or TELEGRAM_BOT_TOKEN is not set",
                         "daily_cap": "The daily cap has been reached"}.get(n.suppressed_reason or ""))
    return {"id": n.id, "status": n.status, "error": error, "suppressed_reason": n.suppressed_reason}


def send_now(db: Session, *, kind: str, body: str, title: str | None = None, dedupe_key: str | None = None,
             source: str | None = None) -> dict:
    """Queue one message and dispatch only it, through quiet hours."""
    queued = queue(db, kind=kind, body=body, title=title, dedupe_key=dedupe_key, source=source)
    if queued.get("error"):
        return {"id": None, "status": "failed", "error": queued["error"], "suppressed_reason": None}
    db.commit()
    dispatch(db, force=True, only=queued["id"])
    return _outcome(db, queued["id"])


def send_test(db: Session) -> dict:
    return send_now(db, kind="test", body="Founder OS connected", source="settings")


def brief_now(db: Session, now: datetime | None = None, source: str = "settings") -> dict:
    now = now or datetime.utcnow()
    local = schedule.to_local(now)
    key = f"briefing:{local.date().isoformat()}:manual:{local.strftime('%H%M%S')}"
    return send_now(db, kind="briefing", body=briefing_text(db, now), dedupe_key=key, source=source)


# ── Endpoints ────────────────────────────────────────────────

@router.get("/notifications/settings")
def get_settings(db: Session = Depends(get_db)):
    return serialize_settings(db, settings(db))


@router.patch("/notifications/settings")
def patch_settings(payload: dict, db: Session = Depends(get_db)):
    import change_log
    with change_log.batch_scope(db, "ui"):
        try:
            result = update_settings(db, payload or {})
        except ValueError as exc:
            db.rollback()
            raise HTTPException(422, str(exc))
        db.commit()
    return {**result["settings"], "change": result["change"]}


@router.get("/notifications")
def list_notifications(limit: int = Query(50, ge=1, le=200), status: str | None = None,
                       kind: str | None = None, db: Session = Depends(get_db)):
    query = db.query(models.Notification)
    if status:
        query = query.filter(models.Notification.status == status)
    if kind:
        query = query.filter(models.Notification.kind == kind)
    return [serialize(n) for n in query.order_by(models.Notification.id.desc()).limit(limit).all()]


@router.post("/notifications/test")
def test_endpoint(db: Session = Depends(get_db)):
    result = send_test(db)
    return {"status": result["status"], "error": result["error"]}


@router.post("/notifications/brief-now")
def brief_now_endpoint(db: Session = Depends(get_db)):
    result = brief_now(db)
    return {"status": result["status"], "error": result["error"]}


@router.post("/notifications/{notification_id}/cancel")
def cancel_endpoint(notification_id: int, db: Session = Depends(get_db)):
    n = db.query(models.Notification).filter(models.Notification.id == notification_id).first()
    if n is None:
        raise HTTPException(404, "Notification not found")
    if n.status != "queued":
        raise HTTPException(409, f"Only a queued notification can be cancelled; this one is {n.status}")
    n.status = "cancelled"
    db.commit()
    return serialize(n)
