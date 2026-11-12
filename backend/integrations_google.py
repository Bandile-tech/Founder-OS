"""
Google Calendar (Phase 6C, ADR-0004) — OAuth, a cache of the calendar, and event creation.

Primitives: start_url, check_state, exchange, connected, sync, create_event, delete_remote,
            today, week, disconnect
Endpoints:  GET /integrations/google/start, GET /integrations/google/callback (no session),
            POST /integrations/google/sync, POST /integrations/google/disconnect,
            GET /calendar/today, GET /calendar/week, POST /calendar/events

Tokens live in the ``google_calendar`` integrations row's ``secret_json`` and are never
returned; every stored error and log line goes through ``integrations.redact``. The cache
(``calendar_events``) is not change-logged; an event Founder OS creates is, and undoing it
deletes the remote event too (best effort). Times are stored naive UTC; ``start_date`` is
the local date.
"""

import hashlib
import hmac
import json
import os
import secrets as _secrets
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote, urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

import auth
import integrations
import models
from agents import schedule
from database import get_db

router = APIRouter()

PROVIDER = "google_calendar"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API = "https://www.googleapis.com/calendar/v3"
SCOPE = "https://www.googleapis.com/auth/calendar.events"
STATE_TTL_SECONDS = 600
TIMEOUT = 15
PAGE_SIZE = 250
MAX_PAGES = 10
RECONNECT = "Reconnect Google Calendar"
NOT_CONNECTED = "Calendar not connected"
AFTER_CONNECT = "/app/#settings"            # the app and the API share one host


class GoogleError(Exception):
    """A call to Google failed. The message is safe to show (already redacted).

    ``transient`` is True for everything except a sign-in Google has ended (``invalid_grant``
    on refresh, a 401 from the API): a timeout, a 5xx or any other refusal leaves the
    integration's status alone and is simply tried again on the next sync."""

    def __init__(self, message: str, *, transient: bool = True):
        super().__init__(message)
        self.transient = transient


# ── Configuration ────────────────────────────────────────────

def client_id() -> str | None:
    return os.getenv("GOOGLE_CLIENT_ID") or None


def client_secret() -> str | None:
    return os.getenv("GOOGLE_CLIENT_SECRET") or None


def calendar_id() -> str:
    return os.getenv("GOOGLE_CALENDAR_ID") or "primary"


def redirect_uri() -> str:
    return (os.getenv("FOUNDER_OS_API_URL") or "").rstrip("/") + "/integrations/google/callback"


def _row(db: Session) -> models.Integration:
    return integrations.get_or_create(db, PROVIDER)


def connected(db: Session) -> bool:
    row = _row(db)
    return row.status == "connected" and bool(integrations.secrets(row).get("access_token"))


# ── OAuth ────────────────────────────────────────────────────

def _state_key(db: Session) -> bytes:
    """A random key kept on the row, used only to sign OAuth states. Not the server secret:
    without FOUNDER_OS_SECRET that is derived from the password, and a state passes through
    Google, the browser's history and the access log, where its signature would be something
    to test password guesses against."""
    row = _row(db)
    key = integrations.config(row).get("state_key")
    if not key:
        key = _secrets.token_hex(32)
        integrations.set_config(row, state_key=key)
        db.commit()
    return str(key).encode("utf-8")


def _sign(db: Session, nonce: str, stamp: int) -> str:
    return hmac.new(_state_key(db), f"{nonce}:{stamp}".encode("utf-8"), hashlib.sha256).hexdigest()


def new_state(db: Session) -> str:
    """A signed, single-use state good for ten minutes. The nonce is remembered on the row."""
    nonce, stamp = _secrets.token_urlsafe(16), int(time.time())
    signature = _sign(db, nonce, stamp)
    row = _row(db)
    integrations.set_config(row, pending_state=nonce)
    db.commit()
    return f"{nonce}.{stamp}.{signature}"


def check_state(db: Session, state: str | None) -> bool:
    """True once for a state we issued in the last ten minutes."""
    try:
        nonce, stamp_text, signature = str(state or "").split(".")
        stamp = int(stamp_text)
    except ValueError:
        return False
    if not hmac.compare_digest(signature.encode("utf-8"), _sign(db, nonce, stamp).encode("utf-8")):
        return False
    if not 0 <= time.time() - stamp <= STATE_TTL_SECONDS:
        return False
    row = _row(db)
    pending = integrations.config(row).get("pending_state")
    if not pending or not hmac.compare_digest(str(pending).encode("utf-8"), nonce.encode("utf-8")):
        return False
    integrations.set_config(row, pending_state=None)        # single use
    db.commit()
    return True


def start_url(db: Session) -> str:
    return AUTH_URL + "?" + urlencode({
        "client_id": client_id(), "redirect_uri": redirect_uri(), "response_type": "code", "scope": SCOPE,
        "access_type": "offline", "prompt": "consent", "state": new_state(db)})


def _expiry(expires_in) -> str:
    try:
        seconds = int(expires_in)
    except (ValueError, TypeError):
        seconds = 3600
    return (datetime.utcnow() + timedelta(seconds=seconds)).isoformat()


def exchange(db: Session, code: str) -> None:
    """Swap the authorisation code for tokens and mark the row connected. Raises GoogleError."""
    try:
        # VERIFY: the token endpoint's form fields and the response's access_token,
        # refresh_token and expires_in.
        resp = httpx.post(TOKEN_URL, data={
            "code": code, "client_id": client_id(), "client_secret": client_secret(),
            "redirect_uri": redirect_uri(), "grant_type": "authorization_code"}, timeout=TIMEOUT)
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise GoogleError(integrations.redact(f"{type(exc).__name__}: {exc}", db)[:200])
    if resp.status_code != 200 or not isinstance(data, dict) or not data.get("access_token"):
        reason = data.get("error_description") or data.get("error") if isinstance(data, dict) else None
        raise GoogleError(integrations.redact(reason or f"HTTP {resp.status_code}", db)[:200])
    row = _row(db)
    held = integrations.secrets(row)
    row.secret_json = json.dumps({
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token") or held.get("refresh_token"),
        "expiry": _expiry(data.get("expires_in"))})
    row.status, row.account_label, row.scopes, row.last_error = "connected", calendar_id(), SCOPE, None
    row.connected_at = row.updated_at = datetime.utcnow()
    db.commit()


def _token(db: Session) -> str | None:
    """A usable access token, refreshed when it is within a minute of expiring.

    None, with the row marked in error, only when the sign-in is over: Google answers
    ``invalid_grant``, or there is no refresh token to try. A refresh that fails any other
    way (a timeout, a 5xx, a refusal that is not about the grant) raises a transient
    GoogleError and leaves the row and its tokens exactly as they were."""
    row = _row(db)
    held = integrations.secrets(row)
    access = held.get("access_token")
    if not access or row.status == "disconnected":
        return None
    try:
        expiry = datetime.fromisoformat(str(held.get("expiry")))
    except (ValueError, TypeError):
        expiry = datetime.utcnow()
    if expiry - timedelta(seconds=60) >= datetime.utcnow():
        return access
    if not held.get("refresh_token"):
        integrations.set_error(db, row, RECONNECT)
        db.commit()
        return None
    try:
        resp = httpx.post(TOKEN_URL, data={
            "client_id": client_id(), "client_secret": client_secret(),
            "refresh_token": held["refresh_token"], "grant_type": "refresh_token"}, timeout=TIMEOUT)
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 — a network hiccup or an unreadable body: retry later
        raise GoogleError(f"Google could not be reached ({type(exc).__name__})")
    if resp.status_code != 200 or not isinstance(data, dict) or not data.get("access_token"):
        reason = data.get("error") if isinstance(data, dict) else None
        if resp.status_code < 500 and reason == "invalid_grant":
            integrations.set_error(db, row, RECONNECT)      # the grant is gone: only a reconnect helps
            db.commit()
            return None
        raise GoogleError(integrations.redact(
            f"Google refused the token refresh ({reason or resp.status_code})", db)[:200])
    row.secret_json = json.dumps({**held, "access_token": data["access_token"],
                                  "expiry": _expiry(data.get("expires_in"))})
    row.status, row.last_error, row.updated_at = "connected", None, datetime.utcnow()
    db.commit()
    return data["access_token"]


def _call(db: Session, method: str, url: str, **kwargs):
    """An authorised call. Returns the decoded body (None for an empty one). Raises GoogleError."""
    token = _token(db)
    if token is None:
        raise GoogleError(_row(db).last_error or NOT_CONNECTED, transient=False)
    try:
        resp = getattr(httpx, method)(url, headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT, **kwargs)
    except Exception as exc:  # noqa: BLE001
        raise GoogleError(integrations.redact(f"Google could not be reached ({type(exc).__name__})", db))
    if resp.status_code == 401:                     # the token was revoked
        row = _row(db)
        integrations.set_error(db, row, RECONNECT)
        db.commit()
        raise GoogleError(RECONNECT, transient=False)
    if resp.status_code >= 400:
        try:
            detail = (resp.json().get("error") or {}).get("message")
        except Exception:  # noqa: BLE001
            detail = None
        raise GoogleError(integrations.redact(detail or f"Google answered {resp.status_code}", db)[:300])
    if resp.status_code == 204 or not getattr(resp, "content", b"x"):
        return None
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return None


def _events_url(external_id: str | None = None) -> str:
    base = f"{API}/calendars/{quote(calendar_id(), safe='')}/events"
    return f"{base}/{quote(external_id, safe='')}" if external_id else base


# ── Reading Google's event shape ─────────────────────────────

def _rfc3339(naive_utc: datetime) -> str:
    return naive_utc.replace(microsecond=0).isoformat() + "Z"


def _parse_moment(value) -> datetime | None:
    """RFC3339 → naive UTC."""
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=schedule.tz())
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


def _local_midnight_utc(day: date) -> datetime:
    return schedule.to_utc(datetime(day.year, day.month, day.day, tzinfo=schedule.tz()))


def _fields(item: dict) -> dict | None:
    """Google's event → our columns. None when it has no usable start."""
    # VERIFY: start.dateTime / start.date (all-day), end, summary, status, htmlLink, updated.
    start, end = item.get("start") or {}, item.get("end") or {}
    if start.get("dateTime"):
        start_at, end_at, all_day = _parse_moment(start["dateTime"]), _parse_moment(end.get("dateTime")), False
        if start_at is None:
            return None
        start_date = schedule.to_local(start_at).date()
    elif start.get("date"):
        try:
            start_date = date.fromisoformat(start["date"])
            last = date.fromisoformat(end["date"]) if end.get("date") else start_date + timedelta(days=1)
        except (ValueError, TypeError):
            return None
        start_at, end_at, all_day = _local_midnight_utc(start_date), _local_midnight_utc(last), True
    else:
        return None
    return {
        "title": (str(item.get("summary") or "").strip() or "(no title)")[:300],
        "description": (str(item["description"])[:1000] if item.get("description") else None),
        "location": (str(item["location"])[:300] if item.get("location") else None),
        "start_at": start_at, "end_at": end_at or start_at, "all_day": all_day, "start_date": start_date,
        "status": item.get("status") or "confirmed", "html_link": item.get("htmlLink"),
        "remote_updated_at": _parse_moment(item.get("updated")),
    }


# ── Sync ─────────────────────────────────────────────────────

def sync(db: Session, *, window_days: int = 30, now: datetime | None = None) -> dict:
    """Refresh the cache for yesterday through ``window_days`` ahead. Never raises.

    A failure returns ``ok: False`` with ``error`` and ``transient``. A transient failure
    (a timeout, a 5xx) changes nothing, not even the status: the next sync tries again.

    Upserts by Google's event id, marks rows in the window that Google no longer returns as
    deleted, and emits ``calendar.changed`` when anything changed. Nothing is change-logged."""
    now = now or datetime.utcnow()
    counts = {"ok": True, "created": 0, "updated": 0, "deleted": 0, "unchanged": 0, "error": None}
    row = _row(db)
    if row.status == "disconnected" or not integrations.secrets(row).get("access_token"):
        return {**counts, "ok": False, "error": NOT_CONNECTED, "transient": False}
    time_min, time_max = now - timedelta(days=1), now + timedelta(days=window_days)
    items, page_token = [], None
    try:
        for _ in range(MAX_PAGES):
            params = {"timeMin": _rfc3339(time_min), "timeMax": _rfc3339(time_max), "singleEvents": "true",
                      "orderBy": "startTime", "maxResults": PAGE_SIZE}
            if page_token:
                params["pageToken"] = page_token
            page = _call(db, "get", _events_url(), params=params) or {}
            items.extend(page.get("items") or [])
            page_token = page.get("nextPageToken")
            if not page_token:
                break
    except GoogleError as exc:
        # The status is only ever moved to "error" where the sign-in is known to be over
        # (_token on invalid_grant, _call on a 401). Anything else is retried next time.
        if exc.transient:
            integrations.log(f"[google] sync failed, will retry: {exc}", db)
        return {**counts, "ok": False, "error": str(exc), "transient": exc.transient}

    cal = calendar_id()
    existing = {e.external_id: e for e in db.query(models.CalendarEvent).filter(
        models.CalendarEvent.provider == "google").all()}
    seen = set()
    for item in items:
        external_id = item.get("id")
        fields = _fields(item) if external_id else None
        if fields is None or fields["status"] == "cancelled":
            continue
        seen.add(external_id)
        event = existing.get(external_id)
        if event is None:
            db.add(models.CalendarEvent(provider="google", calendar_id=cal, external_id=external_id,
                                        synced_at=now, **fields))
            counts["created"] += 1
            continue
        changed = event.deleted_at is not None or any(getattr(event, k) != v for k, v in fields.items())
        for key, value in fields.items():
            setattr(event, key, value)
        event.deleted_at, event.synced_at, event.calendar_id = None, now, cal
        counts["updated" if changed else "unchanged"] += 1
    for external_id, event in existing.items():
        if (external_id not in seen and event.deleted_at is None and event.start_at is not None
                and time_min <= event.start_at <= time_max):
            event.deleted_at = now
            counts["deleted"] += 1

    row = _row(db)
    row.status, row.last_sync_at, row.last_error, row.updated_at = "connected", now, None, datetime.utcnow()
    if counts["created"] or counts["updated"] or counts["deleted"]:
        import agents
        agents.emit(db, "calendar.changed", {k: counts[k] for k in ("created", "updated", "deleted")},
                    source="calendar_sync")
    db.commit()
    return counts


# ── Creating and removing events ─────────────────────────────

def create_event(db: Session, title: str, start_local: datetime, duration_minutes: int = 60,
                 location: str | None = None, notes: str | None = None) -> dict:
    """Create the event on Google and in the cache, logged. ``start_local`` is an aware
    datetime in the user's zone. Raises ValueError for bad input and GoogleError when Google
    refuses. The caller commits."""
    import change_log
    title = (title or "").strip()
    if not title:
        raise ValueError("an event needs a title")
    if start_local.tzinfo is None:
        start_local = start_local.replace(tzinfo=schedule.tz())
    try:
        minutes = int(duration_minutes or 60)
    except (ValueError, TypeError):
        raise ValueError("the duration must be a number of minutes")
    if not 5 <= minutes <= 24 * 60:
        raise ValueError("the duration must be between 5 minutes and a day")
    if not connected(db):
        raise GoogleError(NOT_CONNECTED)
    end_local = start_local + timedelta(minutes=minutes)
    zone = os.getenv("FOUNDER_OS_TZ") or schedule.DEFAULT_TZ
    body = {"summary": title,
            "start": {"dateTime": start_local.replace(microsecond=0).isoformat(), "timeZone": zone},
            "end": {"dateTime": end_local.replace(microsecond=0).isoformat(), "timeZone": zone}}
    if location:
        body["location"] = str(location)[:300]
    if notes:
        body["description"] = str(notes)[:1000]
    created = _call(db, "post", _events_url(), json=body) or {}
    if not created.get("id"):
        raise GoogleError("Google did not return the new event")
    now = datetime.utcnow()
    event = models.CalendarEvent(
        provider="google", calendar_id=calendar_id(), external_id=created["id"], title=title[:300],
        description=body.get("description"), location=body.get("location"),
        start_at=schedule.to_utc(start_local), end_at=schedule.to_utc(end_local), all_day=False,
        start_date=start_local.date(), status=created.get("status") or "confirmed",
        html_link=created.get("htmlLink"), created_by_us=True,
        remote_updated_at=_parse_moment(created.get("updated")), synced_at=now)
    db.add(event)
    db.flush()
    change = change_log.record_create(db, event, f"Calendar: {title} {start_local.strftime('%H:%M')}")
    return {"event": serialize(event), "change": change}


def delete_remote(db: Session, external_id: str) -> bool:
    """Delete an event on Google. Best effort: a failure is logged, never raised."""
    try:
        _call(db, "delete", _events_url(external_id))
        return True
    except Exception as exc:  # noqa: BLE001
        integrations.log(f"[google] could not delete event {external_id}: {type(exc).__name__}: {exc}", db)
        return False


# ── Reading the cache ────────────────────────────────────────

def _clock(naive_utc: datetime | None) -> str | None:
    return schedule.to_local(naive_utc).strftime("%H:%M") if naive_utc else None


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    """The local day as naive UTC: [midnight, the next midnight)."""
    return _local_midnight_utc(day), _local_midnight_utc(day + timedelta(days=1))


def serialize(event: models.CalendarEvent, day: date | None = None) -> dict:
    """An event as the app shows it. With ``day``, as it looks on that day: an event that
    began on an earlier day reads "until HH:MM" on its last day and "all day" in between."""
    all_day = bool(event.all_day)
    start_local = "all day" if all_day else _clock(event.start_at)
    end_local = None if all_day else _clock(event.end_at)
    if day is not None and not all_day and event.start_at is not None:
        first, last = _day_bounds(day)
        end_at = event.end_at or event.start_at
        if event.start_at < first:                      # it began before this day
            if end_at >= last:
                all_day, start_local, end_local = True, "all day", None
            else:
                start_local = f"until {_clock(end_at)}"
    return {"id": event.id, "title": event.title, "start_local": start_local, "end_local": end_local,
            "all_day": all_day, "date": event.start_date, "start_at": event.start_at,
            "end_at": event.end_at, "location": event.location, "html_link": event.html_link,
            "created_by_us": bool(event.created_by_us)}


def _live(db: Session):
    return db.query(models.CalendarEvent).filter(
        models.CalendarEvent.deleted_at.is_(None), models.CalendarEvent.status != "cancelled")


def today(db: Session, day: date | None = None) -> list[dict]:
    """The day's events from the cache: all-day first, then by start time. An event is on
    every local day it overlaps, not only the day it starts (an all-day event's end is the
    midnight after its last day, so it stops there)."""
    day = day or schedule.to_local(datetime.utcnow()).date()
    first, last = _day_bounds(day)
    rows = []
    for event in _live(db).filter(models.CalendarEvent.start_at < last).all():
        end_at = event.end_at or event.start_at
        starts_today = event.start_at >= first
        if starts_today or end_at > first:
            rows.append(event)
    shown = [serialize(e, day) for e in rows]
    order = {e.id: (e.start_at or datetime.min, e.id) for e in rows}
    shown.sort(key=lambda e: (not e["all_day"], order[e["id"]]))
    return shown


def week(db: Session, first: date | None = None) -> list[dict]:
    first = first or schedule.to_local(datetime.utcnow()).date()
    return [{"date": first + timedelta(days=n), "events": today(db, first + timedelta(days=n))} for n in range(7)]


def disconnect(db: Session) -> dict:
    """Revoke the tokens (best effort), forget them, and retire the cache."""
    row = _row(db)
    held = integrations.secrets(row)
    token = held.get("refresh_token") or held.get("access_token")
    if token:
        try:
            httpx.post(REVOKE_URL, params={"token": token}, timeout=TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            integrations.log(f"[google] revoke failed: {type(exc).__name__}: {exc}", db)
    now = datetime.utcnow()
    row.secret_json, row.status, row.last_error, row.account_label = None, "disconnected", None, None
    integrations.set_config(row, pending_state=None)
    for event in _live(db).filter(models.CalendarEvent.provider == "google").all():
        event.deleted_at = now
    db.commit()
    return integrations.serialize(db, row)


# ── Endpoints ────────────────────────────────────────────────

@router.get("/integrations/google/start")
def start_endpoint(db: Session = Depends(get_db)):
    if not integrations.configured(PROVIDER):
        raise HTTPException(503, "Set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and FOUNDER_OS_API_URL first")
    return {"url": start_url(db)}


@router.get("/integrations/google/callback")
def callback_endpoint(code: str | None = None, state: str | None = None, error: str | None = None,
                      db: Session = Depends(get_db)):
    """Where Google sends the browser back. No session: the signed state is the check."""
    def back(query: str) -> RedirectResponse:
        return RedirectResponse(f"{AFTER_CONNECT}?{query}", status_code=302)

    if error or not code:
        return back("error=google_denied")
    if not check_state(db, state):
        return back("error=google_state")
    try:
        exchange(db, code)
    except GoogleError as exc:
        db.rollback()
        integrations.log(f"[google] token exchange failed: {exc}", db)
        return back("error=google_exchange")
    try:
        sync(db)
    except Exception as exc:  # noqa: BLE001 — connected is connected; the agent will sync
        db.rollback()
        integrations.log(f"[google] first sync failed: {type(exc).__name__}: {exc}", db)
    return back("connected=google")


@router.post("/integrations/google/sync")
def sync_endpoint(db: Session = Depends(get_db)):
    return sync(db)


@router.post("/integrations/google/disconnect")
def disconnect_endpoint(db: Session = Depends(get_db)):
    return disconnect(db)


def _day_param(text: str | None) -> date | None:
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise HTTPException(422, "Dates are YYYY-MM-DD")


@router.get("/calendar/today")
def calendar_today_endpoint(date: str | None = None, db: Session = Depends(get_db)):
    return {"connected": connected(db), "events": today(db, _day_param(date))}


@router.get("/calendar/week")
def calendar_week_endpoint(start: str | None = Query(None, alias="from"), db: Session = Depends(get_db)):
    return {"connected": connected(db), "days": week(db, _day_param(start))}


@router.post("/calendar/events")
def create_event_endpoint(payload: dict, db: Session = Depends(get_db)):
    import business_common
    import change_log
    title, start = (payload or {}).get("title"), (payload or {}).get("start")
    now_local = schedule.to_local(datetime.utcnow())
    when = None
    try:
        when = datetime.fromisoformat(str(start))
        if when.tzinfo is None:
            when = when.replace(tzinfo=schedule.tz())
    except (ValueError, TypeError):
        parsed = business_common.parse_when(start, now_local, default_hour=None)
        when = schedule.to_local(parsed) if parsed else None
    if when is None:
        raise HTTPException(422, "start needs a date and a time")
    with change_log.batch_scope(db, "ui"):
        try:
            result = create_event(db, title, when, payload.get("duration_minutes") or 60,
                                  payload.get("location"), payload.get("notes"))
        except ValueError as exc:
            db.rollback()
            raise HTTPException(422, str(exc))
        except GoogleError as exc:
            db.rollback()
            raise HTTPException(503 if str(exc) == NOT_CONNECTED else 502, str(exc))
        db.commit()
    return {**result["event"], "change": result["change"]}
