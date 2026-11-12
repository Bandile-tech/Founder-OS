"""
Telegram, inbound — messages to the bot go straight into the orchestrator (Phase 6D).

Endpoint: POST /integrations/telegram/webhook   (no session; verified by Telegram's secret header)

Flow for each update:
  1. not paired and the text is the pairing code → pair that chat
  2. any other chat than the paired one → ignored
  3. /brief, /undo, /yes, /no, /mute <n>h, /start, /help
  4. anything else → ``orchestrator.run_turn`` (same history, memory and change batch as
     the dock) and the reply, with what changed and how to undo it

Telegram is answered ``200 {}`` at once; the turn runs after the response, in its own
session, one message at a time. Replies go through ``notify.queue(kind="reply")`` and a
forced dispatch of that row, so they ignore quiet hours and the cap.
"""

import hmac
import json
import os
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

import integrations
import integrations_telegram
import models
from database import SessionLocal

router = APIRouter()

SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"   # VERIFY: the header setWebhook(secret_token=…) produces
SEEN_LIMIT = 200                                    # update ids remembered, to drop Telegram's retries
WINDOW_HOURS = 24                                   # how far back /undo, /yes and /no look
MAX_WRONG_CODES = 5                                 # wrong pairing codes from one chat before it is ignored
LOCKOUT_SECONDS = 3600
MAX_MUTE_HOURS = 72
HELP = "Reply here to log anything.\n/brief resend the briefing · /undo last change · /yes · /no · /mute 2h"

_seen: "OrderedDict[int, None]" = OrderedDict()
_seen_lock = threading.Lock()
_turn_lock = threading.Lock()                       # one message at a time: turns share one thread
_wrong_codes: dict = {}                             # chat id → (count, first attempt at)
_ignored = {"count": 0}


# ── Small helpers ────────────────────────────────────────────

def _already_seen(update_id) -> bool:
    """True for an update id handled before. Remembers the last 200."""
    if not isinstance(update_id, int):
        return False
    with _seen_lock:
        if update_id in _seen:
            return True
        _seen[update_id] = None
        while len(_seen) > SEEN_LIMIT:
            _seen.popitem(last=False)
    return False


def reset_state() -> None:
    """Forget seen updates and pairing attempts (tests)."""
    with _seen_lock:
        _seen.clear()
    _wrong_codes.clear()
    _ignored["count"] = 0


def _session_id(chat_id) -> str:
    return f"telegram:{chat_id}"


def _reply(db, text: str) -> dict:
    import notify
    return notify.send_now(db, kind="reply", body=text, source="telegram")


def _locked_out(chat_id) -> bool:
    count, since = _wrong_codes.get(chat_id, (0, 0.0))
    if count and time.monotonic() - since > LOCKOUT_SECONDS:
        _wrong_codes.pop(chat_id, None)
        return False
    return count >= MAX_WRONG_CODES


def _note_wrong_code(chat_id) -> None:
    count, since = _wrong_codes.get(chat_id, (0, time.monotonic()))
    _wrong_codes[chat_id] = (count + 1, since)
    if len(_wrong_codes) > 1000:                    # never grow without bound
        _wrong_codes.pop(next(iter(_wrong_codes)))


def _code_from(text: str) -> str:
    """The pairing code as sent: on its own, or after /start (a deep link sends that)."""
    words = text.strip().split()
    if words and words[0].lower().split("@")[0] == "/start":
        words = words[1:]
    return words[0].upper() if len(words) == 1 else ""


def _save_exchange(db, chat_id, said: str, answer: str, changes: list | None = None) -> None:
    """Keep a command and its outcome in the thread, so the assistant knows it happened."""
    import conversation
    try:
        conversation.save_message(db, "user", said, session_id=_session_id(chat_id))
        conversation.save_message(db, "assistant", answer, session_id=_session_id(chat_id), changes=changes or None)
    except Exception as exc:  # noqa: BLE001 — the command already ran
        db.rollback()
        integrations.log(f"[telegram] could not save the exchange: {type(exc).__name__}: {exc}", db)


# ── Commands ─────────────────────────────────────────────────

def _undo(db, chat_id) -> str:
    """Reverse the newest batch a Telegram turn made in the last 24 hours."""
    import change_log
    since = datetime.utcnow() - timedelta(hours=WINDOW_HOURS)
    rows = db.query(models.ChatMessage).filter(
        models.ChatMessage.role == "assistant", models.ChatMessage.session_id.like("telegram:%"),
        models.ChatMessage.changes_json.isnot(None), models.ChatMessage.created_at >= since,
    ).order_by(models.ChatMessage.id.desc()).limit(30).all()
    for row in rows:
        try:
            batches = [c.get("batch_id") for c in json.loads(row.changes_json) if isinstance(c, dict)]
        except (ValueError, TypeError):
            continue
        for batch_id in dict.fromkeys(b for b in batches if b):
            pending = db.query(models.ChangeLog).filter(
                models.ChangeLog.batch_id == batch_id, models.ChangeLog.undone_at.is_(None)).all()
            live = [c for c in pending if c.undoable is not False]
            if not live:
                if pending:                         # the newest thing done was a send: say so, undo nothing older
                    return change_log.NOT_UNDOABLE + " Nothing was changed."
                continue
            summaries = [c.summary for c in live if c.summary]
            try:
                outcome = change_log.undo_batch(db, batch_id)
            except HTTPException as exc:
                db.rollback()
                return f"Could not undo that: {exc.detail}"
            changed = {c["id"] for c in outcome.get("changes") or [] if c.get("kept")}
            summaries = [c.summary for c in live if c.summary and c.id not in changed]
            if not summaries and changed:
                return "Nothing was undone. " + change_log.CHANGED_SINCE
            kept = f" ({outcome['note']})" if outcome.get("note") else ""
            return "Undone: " + ("; ".join(summaries) or "the last change") + kept
    return "Nothing to undo from the last 24 hours."


def _waiting(db) -> list:
    """What the LAST Telegram turn proposed and is still waiting on. Proposals from earlier
    turns are no longer Telegram's to answer (``_turn`` lets go of them), so a /yes can never
    land on something Bandile is not looking at."""
    since = datetime.utcnow() - timedelta(hours=WINDOW_HOURS)
    return db.query(models.PendingAction).filter(
        models.PendingAction.status == "pending", models.PendingAction.thread_id.like("telegram:%"),
        models.PendingAction.created_at >= since).order_by(models.PendingAction.id).all()


def _several(waiting: list) -> str:
    listed = "\n".join(f"{n}. {p.summary or 'an action'}" for n, p in enumerate(waiting, 1))
    return (f"{len(waiting)} things are waiting, so /yes will not guess which you mean:\n{listed}\n"
            "/no cancels them all. Then ask for one at a time.")


def _yes(db) -> tuple[str, list]:
    import actions
    import change_log
    waiting = _waiting(db)
    if not waiting:
        return "Nothing is waiting for a yes.", []
    if len(waiting) > 1:
        return _several(waiting), []
    pending = waiting[0]
    summary = pending.summary or "that"
    try:
        # A /yes from the paired chat is Bandile's own approval, in a later message.
        outcome = actions.confirm_action(db, pending.id, human=True)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return f"Could not do that: {integrations.redact(exc, db)[:200]}", []
    if outcome.get("executed"):
        change = outcome.get("change")
        changes = outcome.get("changes") or ([change] if isinstance(change, dict) else [])
        tail = "/undo to reverse" if outcome.get("undoable", True) else change_log.NOT_UNDOABLE
        if outcome.get("open_url"):
            # WhatsApp has no API here: approving only builds the link. The tap is the send.
            return (f"Approved: {summary}\nTap to open WhatsApp and send it:\n{outcome['open_url']}"
                    f"\n{tail}"), changes
        return f"Done: {summary}\n{tail}", changes
    return f"Not done: {outcome.get('reason') or outcome.get('error') or 'it could not be carried out'}", []


def _no(db) -> str:
    import actions
    waiting = _waiting(db)
    if not waiting:
        return "Nothing is waiting for an answer."
    for pending in waiting:
        actions.cancel_action(db, pending.id)
    return "Cancelled: " + "; ".join(p.summary or "that" for p in waiting)


def _mute(db, argument: str) -> str:
    import notify
    from agents import schedule
    s = notify.settings(db)
    arg = argument.strip().lower()
    if arg in ("off", "0", "stop", "end"):
        s.muted_until = None
        db.commit()
        return "Unmuted."
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(h|hr|hrs|hour|hours|m|min|mins|minutes)?", arg or "1h")
    if not m:
        return "Say how long, like /mute 2h or /mute 30m. /mute off ends it."
    amount = float(m.group(1))
    hours = amount / 60 if (m.group(2) or "h").startswith("m") else amount
    if not 0 < hours <= MAX_MUTE_HOURS:
        return f"Between a minute and {MAX_MUTE_HOURS} hours, like /mute 2h."
    s.muted_until = datetime.utcnow() + timedelta(hours=hours)
    db.commit()
    until = schedule.to_local(s.muted_until)
    day = "" if until.date() == schedule.to_local(datetime.utcnow()).date() else f" {until.strftime('%a')}"
    return f"Muted until{day} {until.strftime('%H:%M')}. Replies to you still come through."


def _command(db, chat_id, text: str) -> str | None:
    """Run a slash command and return the reply (None when the command sent its own message)."""
    import notify
    word, _, argument = text.strip().partition(" ")
    name = word.lower().split("@")[0]
    if name == "/brief":
        outcome = notify.brief_now(db, source="telegram")
        return None if outcome["status"] == "sent" else f"Could not send the briefing: {outcome['error'] or outcome['status']}"
    if name == "/undo":
        answer = _undo(db, chat_id)
        _save_exchange(db, chat_id, "/undo", answer)
        return answer
    if name == "/yes":
        answer, changes = _yes(db)
        _save_exchange(db, chat_id, "/yes", answer, changes)
        return answer
    if name == "/no":
        answer = _no(db)
        _save_exchange(db, chat_id, "/no", answer)
        return answer
    if name == "/mute":
        return _mute(db, argument)
    if name in ("/start", "/help"):
        return "You're paired. " + HELP
    return "I don't know that command.\n" + HELP


# ── A turn ───────────────────────────────────────────────────

def format_reply(result: dict) -> str:
    """The assistant's answer, what changed, and how to answer a proposal."""
    text = (result.get("final") or "").strip() or "Done."
    changes = [c for c in result.get("changes") or [] if isinstance(c, dict) and c.get("summary")]
    if changes:
        import change_log
        kept = [c for c in changes if c.get("undoable") is False]
        text += "\nChanged: " + "; ".join(c["summary"] for c in changes) + "\n"
        if len(kept) == len(changes):
            text += change_log.NOT_UNDOABLE
        elif kept:
            text += "/undo to reverse (a sent message cannot be undone)"
        else:
            text += "/undo to reverse"
    waiting = result.get("pending") or []
    if len(waiting) == 1:
        text += "\nReply /yes to confirm or /no."
    elif waiting:
        listed = "\n".join(f"{n}. {p.get('summary') or 'an action'}" for n, p in enumerate(waiting, 1))
        text += (f"\n{len(waiting)} things are waiting for an answer:\n{listed}\n"
                 "/yes takes one thing at a time, so it will not confirm these. /no cancels them all.")
    return text


def _turn(db, chat_id, text: str) -> str:
    import orchestrator
    session_id = _session_id(chat_id)
    # The conversation has moved on: whatever an earlier turn proposed is no longer what a
    # /yes would be about. It stays pending for the app; Telegram lets go of it.
    db.query(models.PendingAction).filter(
        models.PendingAction.status == "pending", models.PendingAction.thread_id.like("telegram:%"),
    ).update({models.PendingAction.thread_id: None}, synchronize_session=False)
    db.commit()
    result = orchestrator.run_turn(text, db, source="telegram", session_id=session_id)
    ids = [p["id"] for p in result.get("pending") or []]
    if ids:
        # Mark what this turn proposed, so /yes and /no know which action is meant.
        db.query(models.PendingAction).filter(models.PendingAction.id.in_(ids)).update(
            {models.PendingAction.thread_id: session_id}, synchronize_session=False)
        db.commit()
    return format_reply(result)


# ── One update ───────────────────────────────────────────────

def handle_update(db, update: dict) -> str:
    """Act on one Telegram update. Returns what was done, for the log and the tests."""
    import notify
    message = update.get("message") if isinstance(update, dict) else None
    if not isinstance(message, dict):
        return "ignored: not a message"
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None or chat.get("type") not in (None, "private"):
        return "ignored: not a private chat"
    text = message.get("text")
    paired_chat = integrations_telegram.chat_id(db)

    if paired_chat is None:
        if not isinstance(text, str) or _locked_out(chat_id):
            return "ignored: not paired"
        code = _code_from(text)
        if not code:
            return "ignored: not paired"
        if not hmac.compare_digest(code.encode("utf-8"), integrations_telegram.pair_code().encode("utf-8")):
            _note_wrong_code(chat_id)
            return "ignored: wrong pairing code"
        sender = message.get("from") or {}
        integrations_telegram.pair(db, chat_id, sender.get("username") or sender.get("first_name"))
        db.commit()
        _wrong_codes.clear()
        _reply(db, f"Paired. You'll get your briefing at {notify.settings(db).morning_time or '06:00'}.\n{HELP}")
        return "paired"

    if str(chat_id) != str(paired_chat):
        _ignored["count"] += 1
        print(f"[telegram] ignored a message from another chat ({_ignored['count']} so far)")
        return "ignored: another chat"

    if not isinstance(text, str) or not text.strip():
        _reply(db, "Text only for now.")
        return "replied: text only"
    text = text.strip()
    if text.startswith("/"):
        answer = _command(db, chat_id, text)
        if answer is not None:
            _reply(db, answer)
        return "command: " + text.split()[0].lower().split("@")[0]
    try:
        answer = _turn(db, chat_id, text)
    except Exception as exc:  # noqa: BLE001 — never leave a message unanswered
        db.rollback()
        integrations.log(f"[telegram] turn failed: {type(exc).__name__}: {exc}", db)
        answer = "Something went wrong on my side. Please try again."
    _reply(db, answer)
    return "turn"


def _process(update: dict) -> None:
    """Background entry point: its own session, one update at a time, never raises."""
    with _turn_lock:
        db = None
        try:
            db = SessionLocal()
            handle_update(db, update)
        except Exception as exc:  # noqa: BLE001
            integrations.log(f"[telegram] update failed: {type(exc).__name__}: {exc}")
        finally:
            if db is not None:
                db.close()


@router.post("/integrations/telegram/webhook")
async def webhook(request: Request, background: BackgroundTasks,
                  x_telegram_bot_api_secret_token: str | None = Header(default=None)):
    """Telegram's updates. 403 unless the secret header matches; otherwise always ``200 {}``."""
    secret = os.getenv("TELEGRAM_WEBHOOK_SECRET")
    given = x_telegram_bot_api_secret_token or ""
    if not secret or not hmac.compare_digest(given.encode("utf-8"), secret.encode("utf-8")):
        raise HTTPException(403, "Forbidden")
    try:
        update = await request.json()
    except Exception:  # noqa: BLE001 — a body we cannot read is not worth a retry
        return {}
    if isinstance(update, dict) and not _already_seen(update.get("update_id")):
        background.add_task(_process, update)
    return {}
