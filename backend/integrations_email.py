"""
Email over IMAP and SMTP (Phase 6C, ADR-0004) — replies are seen, approved outreach is sent.

Primitives: configured, send, fetch, list_messages
Endpoints:  POST /integrations/email/sync, POST /integrations/email/test, GET /email/messages

Gmail with an App Password, standard library only. The address and password stay in the
environment; the ``email`` integrations row records status and the last sync.

Privacy rules that hold everywhere in this module:
  * only mail whose sender is a known lead or a client's contact is stored; everything
    else is counted and discarded
  * what is stored is the headers and a snippet of at most 500 characters, never a body
  * nothing is sent except through the outreach approval gate (and the test message to
    Bandile's own address)
  * credentials never reach a log line or a stored error (``integrations.redact``)
  * a From line proves nothing: a message only changes a lead when the receiving server's
    own Authentication-Results say DMARC, or an aligned DKIM or SPF, passed. Anything else
    is stored as unverified and offered as a one-tap suggestion
"""

import email as email_lib
import hashlib
import imaplib
import json
import os
import re
import smtplib
from datetime import date, datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr, parsedate_to_datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

import integrations
import models
import user_clock
from database import get_db

router = APIRouter()

PROVIDER = "email"
NOT_CONFIGURED = "email sending is not configured"
SNIPPET_LIMIT = 500
FETCH_DAYS = 3                  # how far back the very first sync looks
MARGIN_DAYS = 1                 # overlap with the last successful sync (IMAP SINCE is whole days)
MAX_MESSAGES = 200              # messages looked at per sync; the rest wait for the next one
SMTP_TIMEOUT = 20
IMAP_TIMEOUT = 20
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_ADDRESS = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")
_HEADERS = ("FROM TO SUBJECT DATE MESSAGE-ID IN-REPLY-TO CONTENT-TYPE CONTENT-TRANSFER-ENCODING "
            "AUTHENTICATION-RESULTS")


# ── Configuration ────────────────────────────────────────────

def address() -> str | None:
    return (os.getenv("EMAIL_ADDRESS") or "").strip() or None


def _password() -> str | None:
    return os.getenv("EMAIL_APP_PASSWORD") or None


def configured() -> bool:
    return bool(address() and _password())


def _imap_host() -> str:
    return os.getenv("EMAIL_IMAP_HOST") or "imap.gmail.com"


def _smtp_host() -> str:
    return os.getenv("EMAIL_SMTP_HOST") or "smtp.gmail.com"


def _smtp_port() -> int:
    try:
        return int(os.getenv("EMAIL_SMTP_PORT") or 465)
    except ValueError:
        return 465


def _authserv_id() -> str | None:
    """The name our own mail server signs its Authentication-Results with. Only that header
    is believed: a sender can put any other header in a message. Gmail's is mx.google.com;
    for another host set EMAIL_AUTHSERV_ID, and until then no sender counts as verified."""
    named = (os.getenv("EMAIL_AUTHSERV_ID") or "").strip().lower()
    if named:
        return named
    host = _imap_host().lower()
    return "mx.google.com" if ("gmail" in host or "googlemail" in host) else None


def _from_name() -> str:
    return os.getenv("EMAIL_FROM_NAME") or "Bandile Masocha"


def _row(db: Session) -> models.Integration:
    return integrations.get_or_create(db, PROVIDER)


def _fail(db: Session, message) -> str:
    """Mark the integration in error with a redacted message; returns that message."""
    row = _row(db)
    integrations.set_error(db, row, message)
    row.account_label = address()
    return row.last_error


def _ok(db: Session, *, synced: bool = False) -> None:
    row = _row(db)
    row.status, row.last_error, row.account_label, row.updated_at = "connected", None, address(), datetime.utcnow()
    if row.connected_at is None:
        row.connected_at = datetime.utcnow()
    if synced:
        row.last_sync_at = user_clock.utcnow()


def serialize(m: models.EmailMessage) -> dict:
    return {"id": m.id, "message_id": m.message_id, "thread_key": m.thread_key, "direction": m.direction,
            "from_addr": m.from_addr, "to_addr": m.to_addr, "subject": m.subject, "snippet": m.snippet,
            "received_at": m.received_at, "lead_id": m.lead_id, "client_id": m.client_id,
            "contact_id": m.contact_id, "outreach_message_id": m.outreach_message_id,
            "verified": m.direction == "out" or m.auth_result in (None, "pass", "confirmed")}


# ── Sending ──────────────────────────────────────────────────

def _one_line(text) -> str:
    return " ".join(str(text or "").split())


def send(db: Session, *, to: str, subject: str, body: str, reply_to: str | None = None,
         lead_id: int | None = None, outreach_message_id: int | None = None, store: bool = True) -> dict:
    """Send one plain-text email. Returns ``{ok, message_id, error}``; never raises. A sent
    message is stored as an ``out`` row (headers and a snippet). The caller commits."""
    if not configured():
        return {"ok": False, "message_id": None, "error": NOT_CONFIGURED}
    # One address, and no second one hiding in the display name: a value like
    # "a@b.example" <someone@else.example> would be sent to the second while the first is shown,
    # so the value must be a bare address, or a plain name with one. A "name" that is itself
    # an address, or a list, is refused.
    raw = _one_line(to).strip()
    name, recipient = parseaddr(raw)
    recipient = recipient.lower()
    if "@" in name or "," in raw or ";" in raw or not _ADDRESS.match(recipient):
        return {"ok": False, "message_id": None, "error": f"'{raw[:80]}' is not an email address"}
    text = str(body or "").strip()
    if not text:
        return {"ok": False, "message_id": None, "error": "an email needs a body"}
    sender = address()
    message = EmailMessage()
    message["From"] = formataddr((_from_name(), sender))
    message["To"] = recipient
    message["Subject"] = _one_line(subject)[:200] or "(no subject)"
    message["Date"] = formatdate(localtime=False)
    message_id = make_msgid(domain=sender.split("@")[-1])
    message["Message-ID"] = message_id
    if reply_to:
        message["In-Reply-To"] = _one_line(reply_to)
        message["References"] = _one_line(reply_to)
    message.set_content(text)
    try:
        with smtplib.SMTP_SSL(_smtp_host(), _smtp_port(), timeout=SMTP_TIMEOUT) as smtp:
            smtp.login(sender, _password())
            smtp.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        error = _fail(db, f"The mail server refused the sign-in: {exc}")
        return {"ok": False, "message_id": None, "error": error}
    except Exception as exc:  # noqa: BLE001 — SMTP and socket errors are outcomes, not crashes
        error = integrations.redact(f"{type(exc).__name__}: {exc}", db)[:300]
        integrations.log(f"[email] send failed: {error}", db)
        return {"ok": False, "message_id": None, "error": error}
    if store:
        client_id = None
        if lead_id is not None:
            lead = db.query(models.Lead).filter(models.Lead.id == lead_id).first()
            client_id = lead.client_id if lead is not None else None
        now = datetime.utcnow()
        db.add(models.EmailMessage(
            message_id=message_id, thread_key=_one_line(reply_to) or None, direction="out", from_addr=sender,
            to_addr=recipient, subject=message["Subject"], snippet=_one_line(text)[:SNIPPET_LIMIT],
            received_at=now, lead_id=lead_id, client_id=client_id, outreach_message_id=outreach_message_id,
            processed_at=now, created_at=now))
        _ok(db)
        db.flush()
    return {"ok": True, "message_id": message_id, "error": None}


# ── Reading a fetched message ────────────────────────────────

def _header(message, name: str) -> str:
    value = message.get(name)
    if value is None:
        return ""
    try:
        return _one_line(str(make_header(decode_header(str(value)))))
    except Exception:  # noqa: BLE001 — an undecodable header is still a header
        return _one_line(value)


def _moment(value: str) -> datetime | None:
    try:
        parsed = parsedate_to_datetime(value)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def _strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
    return re.sub(r"(?s)<[^>]+>", " ", text)


def _snippet(headers, text_bytes: bytes) -> str:
    """The first words of the message as plain text, at most 500 characters. Works on the
    first 2,000 bytes of the raw body, so anything it cannot decode becomes an empty snippet."""
    raw = text_bytes or b""
    content_type = (headers.get("Content-Type") or "text/plain").lower()
    encoding = (headers.get("Content-Transfer-Encoding") or "").lower()
    try:
        if content_type.startswith("multipart/"):
            # The first part that is text: parse it as a message of its own.
            boundary = re.search(r'boundary="?([^";\s]+)"?', headers.get("Content-Type") or "", re.I)
            parts = raw.split(b"--" + boundary.group(1).encode("ascii", "ignore")) if boundary else []
            text = ""
            for part in parts[1:]:
                piece = email_lib.message_from_bytes(part.lstrip(b"\r\n"))
                kind = piece.get_content_type()
                if kind not in ("text/plain", "text/html"):
                    continue
                payload = piece.get_payload(decode=True) or b""
                text = payload.decode(piece.get_content_charset() or "utf-8", "ignore")
                if kind == "text/html":
                    text = _strip_html(text)
                if text.strip():
                    break
        else:
            piece = EmailMessage()
            piece["Content-Type"] = headers.get("Content-Type") or "text/plain"
            if encoding:
                piece["Content-Transfer-Encoding"] = encoding
            piece.set_payload(raw.decode("ascii", "ignore") if encoding in ("base64", "quoted-printable")
                              else raw.decode("utf-8", "ignore"))
            payload = piece.get_payload(decode=True) if encoding in ("base64", "quoted-printable") else None
            text = (payload.decode(piece.get_content_charset() or "utf-8", "ignore") if payload is not None
                    else raw.decode("utf-8", "ignore"))
            if content_type.startswith("text/html"):
                text = _strip_html(text)
    except Exception:  # noqa: BLE001
        return ""
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(">"):
            continue                                # quoted earlier mail
        if re.match(r"^On .{5,120} wrote:$", stripped) or stripped.startswith("-----Original Message"):
            break                                   # the rest is the thread
        lines.append(stripped)
    return _one_line(" ".join(lines))[:SNIPPET_LIMIT]


def _aligned(domain: str, sender_domain: str) -> bool:
    """The same domain, or one a subdomain of the other (DMARC's relaxed alignment)."""
    a, b = (domain or "").strip().strip(".").lower(), (sender_domain or "").strip().strip(".").lower()
    return bool(a and b) and (a == b or a.endswith("." + b) or b.endswith("." + a))


def _auth_result(headers, sender: str) -> str:
    """Did the receiving server vouch for the From address? ``pass``, ``fail`` or ``none``.

    Reads only the topmost Authentication-Results header, and only when it carries our own
    server's name (lower ones, and ones under another name, may have been written by the
    sender). ``pass`` needs DMARC to pass for the From domain or, with no DMARC verdict, a
    DKIM signature or an SPF result that passed for the From domain itself."""
    trusted = _authserv_id()
    values = headers.get_all("Authentication-Results") or []
    if not trusted or not values or "@" not in sender:
        return "none"
    parts = [p.strip() for p in _one_line(str(values[0])).split(";")]
    if not parts or not parts[0] or parts[0].split()[0].lower() != trusted:
        return "none"
    domain = sender.rsplit("@", 1)[-1]
    seen, aligned_pass = False, False
    for part in parts[1:]:
        found = re.match(r"(dmarc|dkim|spf)\s*=\s*([a-z]+)", part, re.I)
        if not found:
            continue
        seen = True
        method, verdict = found.group(1).lower(), found.group(2).lower()

        def prop(name: str) -> str:
            hit = re.search(rf"\b{re.escape(name)}=\s*([^\s;()]+)", part, re.I)
            return hit.group(1).rsplit("@", 1)[-1] if hit else ""

        if method == "dmarc":
            if verdict == "pass" and _aligned(prop("header.from") or domain, domain):
                return "pass"
            if verdict == "fail":
                return "fail"
        elif verdict == "pass":
            signed = (prop("header.i") or prop("header.d")) if method == "dkim" else prop("smtp.mailfrom")
            aligned_pass = aligned_pass or _aligned(signed, domain)
    if aligned_pass:
        return "pass"
    return "fail" if seen else "none"


def _parse(parts) -> dict | None:
    """One IMAP FETCH response → the fields we keep. None when it has no headers."""
    header_bytes, text_bytes, thread = b"", b"", None
    for part in parts or []:
        if isinstance(part, tuple) and len(part) >= 2:
            envelope, literal = part[0] or b"", part[1] or b""
            found = re.search(rb"X-GM-THRID (\d+)", envelope)
            if found:
                thread = found.group(1).decode("ascii")
            if b"HEADER" in envelope.upper():
                header_bytes = literal
            elif b"TEXT" in envelope.upper():
                text_bytes = literal
        elif isinstance(part, (bytes, bytearray)):
            found = re.search(rb"X-GM-THRID (\d+)", bytes(part))
            if found:
                thread = found.group(1).decode("ascii")
    if not header_bytes:
        return None
    headers = email_lib.message_from_bytes(header_bytes)
    sender = parseaddr(_header(headers, "From"))[1].lower()
    subject, when = _header(headers, "Subject"), _header(headers, "Date")
    message_id = _header(headers, "Message-ID")
    if not message_id:
        digest = hashlib.sha1(f"{sender}|{when}|{subject}".encode("utf-8", "ignore")).hexdigest()[:24]
        message_id = f"<no-id-{digest}@founder-os>"
    in_reply_to = _header(headers, "In-Reply-To") or None
    return {"message_id": message_id[:500], "from_addr": sender, "to_addr": _header(headers, "To")[:500] or None,
            "subject": subject[:300] or None, "received_at": _moment(when), "in_reply_to": in_reply_to,
            "thread_key": thread or in_reply_to, "snippet": _snippet(headers, text_bytes),
            "auth": _auth_result(headers, sender)}


# ── Who we know ──────────────────────────────────────────────

def _known_senders(db: Session) -> dict:
    """address → {"lead": Lead | None, "contact": Contact | None}."""
    known: dict = {}
    for lead in db.query(models.Lead).filter(models.Lead.email.isnot(None)).all():
        key = (lead.email or "").strip().lower()
        if key:
            known.setdefault(key, {"lead": None, "contact": None})["lead"] = lead
    if hasattr(models, "Contact"):
        for contact in db.query(models.Contact).filter(models.Contact.email.isnot(None)).all():
            key = (contact.email or "").strip().lower()
            if key:
                known.setdefault(key, {"lead": None, "contact": None})["contact"] = contact
    return known


# ── Fetching ─────────────────────────────────────────────────

def _since_text(day: date) -> str:
    return f"{day.day:02d}-{_MONTHS[day.month - 1]}-{day.year}"


def _suggest_reply(db: Session, lead: models.Lead, row: models.EmailMessage, parsed: dict) -> None:
    """An unverified message that would have marked the lead as replied: ask instead."""
    import actions
    waiting = db.query(models.Suggestion).filter(
        models.Suggestion.kind == "lead_reply", models.Suggestion.status == "pending").all()
    for s in waiting:
        try:
            if json.loads(s.payload_json).get("lead_id") == lead.id:
                return                              # one open question per lead is enough
        except (ValueError, TypeError):
            continue
    subject = parsed["subject"] or "(no subject)"
    actions.add_suggestion(
        db, "lead_reply",
        {"lead_id": lead.id, "email_message_id": row.id, "summary": f"{subject}: {(parsed['snippet'] or '')[:140]}",
         "text": f"Mark {lead.name[:60]} as replied? An email in their name arrived: {subject[:80]}"},
        confidence=None, source="email_sync",
        reason="The sender could not be verified, so nothing was changed.")


def confirm_reply(db: Session, payload: dict) -> dict:
    """Bandile accepted an unverified reply: do what a verified one would have done.
    The caller opens the change batch and commits."""
    import leads
    lead = db.query(models.Lead).filter(models.Lead.id == payload.get("lead_id")).first()
    if lead is None:
        raise HTTPException(409, "That lead no longer exists")
    result = leads.log_reply(db, lead, payload.get("summary"), channel="email")
    row = db.query(models.EmailMessage).filter(models.EmailMessage.id == payload.get("email_message_id")).first()
    if row is not None:
        row.auth_result = "confirmed"
        if row.outreach_message_id:
            sent = db.query(models.OutreachMessage).filter(
                models.OutreachMessage.id == row.outreach_message_id).first()
            if sent is not None and sent.replied_at is None:
                sent.replied_at = row.received_at or datetime.utcnow()
    return result


def _store(db: Session, parsed: dict, who: dict, counts: dict) -> None:
    """Keep one inbound message from a known sender, with everything that follows from it.
    A message whose sender is not verified is kept and changes nothing."""
    import change_log
    lead, contact = who["lead"], who["contact"]
    client_id = contact.client_id if contact is not None else (lead.client_id if lead is not None else None)
    now = datetime.utcnow()
    when = parsed["received_at"] or now
    outreach_message = None
    if parsed["in_reply_to"]:
        outreach_message = db.query(models.OutreachMessage).filter(
            models.OutreachMessage.external_id == parsed["in_reply_to"]).first()
    row = models.EmailMessage(
        message_id=parsed["message_id"], thread_key=parsed["thread_key"], direction="in",
        from_addr=parsed["from_addr"], to_addr=parsed["to_addr"], subject=parsed["subject"],
        snippet=parsed["snippet"], received_at=when, lead_id=lead.id if lead is not None else None,
        client_id=client_id, contact_id=contact.id if contact is not None else None,
        outreach_message_id=outreach_message.id if outreach_message is not None else None,
        auth_result=parsed.get("auth") or "none", processed_at=now, created_at=now)
    db.add(row)
    db.flush()
    counts["stored"] += 1
    counts["matched_leads"] += 1 if lead is not None else 0
    counts["matched_clients"] += 1 if client_id is not None else 0
    if row.auth_result != "pass":
        # Anyone can write a known address in a From line. Keep the message; change nothing.
        counts["unverified"] += 1
        if lead is not None and lead.status == "contacted":
            _suggest_reply(db, lead, row, parsed)
        return
    if outreach_message is not None and outreach_message.replied_at is None:
        outreach_message.replied_at = when

    if hasattr(models, "Communication"):
        import crm
        client = db.query(models.Client).filter(models.Client.id == client_id).first() if client_id else None
        summary = f"{parsed['subject'] or '(no subject)'}: {(parsed['snippet'] or '')[:140]}".rstrip(": ")
        crm.log_communication(db, client=client, lead=lead, contact=contact, channel="email", direction="in",
                              summary=summary, occurred_at=when, source="email_sync",
                              outreach_message_id=outreach_message.id if outreach_message is not None else None)

    if lead is not None and lead.status == "contacted":
        import agents
        import leads
        leads.close_follow_ups(db, lead)
        before = change_log.snapshot(lead)
        lead.status, lead.last_contact_at, lead.updated_at = "replied", when, now
        change_log.record_update(db, lead, before, f"{lead.name[:50]} replied by email")
        agents.emit(db, "lead.replied", {"lead_id": lead.id, "email_message_id": row.id}, source="email_sync")


def _window_start(row: models.Integration) -> date:
    """The day a sync starts reading from: the last successful sync less a day's margin
    (never a fixed number of days back, so a week without a sync loses nothing); three days
    for the first sync; and while a backlog is being worked through, where that began."""
    backlog = integrations.config(row).get("mail_backlog_since")
    if backlog:
        try:
            return date.fromisoformat(str(backlog))
        except ValueError:
            pass
    if row.last_sync_at is None:                    # only a successful fetch sets it
        return user_clock.utcnow().date() - timedelta(days=FETCH_DAYS)
    return row.last_sync_at.date() - timedelta(days=MARGIN_DAYS)


def _uidvalidity(conn) -> int | None:
    """The mailbox's UIDVALIDITY, as SELECT reported it. UIDs only mean anything under it."""
    try:
        _code, data = conn.response("UIDVALIDITY")
        return int(data[0]) if data and data[0] else None
    except Exception:  # noqa: BLE001
        return None


def fetch(db: Session, since: date | None = None) -> dict:
    """Read the inbox since the last successful sync and keep what known people sent.
    Never raises.

    At most ``MAX_MESSAGES`` are looked at per call, oldest first; the UID of the last one is
    remembered, so the next call carries on from there and a long gap is worked through
    without skipping anything. ``since`` forces a re-read from that day.

    Returns ``{ok, fetched, stored, matched_leads, matched_clients, ignored, error, backlog,
    unverified}``; ``backlog`` is how many messages are still waiting for the next call and
    ``unverified`` how many stored messages failed the sender check (see ``_auth_result``)."""
    import change_log
    counts = {"ok": True, "fetched": 0, "stored": 0, "matched_leads": 0, "matched_clients": 0, "ignored": 0,
              "error": None, "backlog": 0, "unverified": 0}
    if not configured():
        return {**counts, "ok": False, "error": "Email is not configured"}
    row = _row(db)
    held = integrations.config(row)
    forced = since is not None
    since = since or _window_start(row)
    host = _imap_host()
    gmail = "gmail" in host.lower() or "googlemail" in host.lower()
    # VERIFY: X-GM-THRID is a Gmail extension; other servers reject it, so it is only
    # asked of Gmail.
    items = f"(BODY.PEEK[HEADER.FIELDS ({_HEADERS})] BODY.PEEK[TEXT]<0.2000>{' X-GM-THRID' if gmail else ''})"
    conn, messages, validity, batch, cursor = None, [], None, [], None
    try:
        conn = imaplib.IMAP4_SSL(host, timeout=IMAP_TIMEOUT)
        conn.login(address(), _password())
        conn.select("INBOX", readonly=True)
        validity = _uidvalidity(conn)
        # VERIFY: UID SEARCH / UID FETCH (RFC 3501) behave on Gmail as on any IMAP server.
        status, data = conn.uid("SEARCH", "SINCE", _since_text(since))
        if status != "OK":
            raise imaplib.IMAP4.error(f"search answered {status}")
        uids = sorted(int(u) for u in ((data[0] or b"").split() if data else []) if u.isdigit())
        if validity is not None and held.get("mail_uidvalidity") == validity and held.get("mail_uid"):
            cursor = int(held["mail_uid"])
        if cursor is not None and not forced:
            uids = [u for u in uids if u > cursor]          # already looked at, kept or not
        # Oldest first when the place can be remembered; else the newest, as a best effort.
        batch = uids[:MAX_MESSAGES] if validity is not None else uids[-MAX_MESSAGES:]
        counts["backlog"] = len(uids) - len(batch)
        for uid in batch:
            status, parts = conn.uid("FETCH", str(uid), items)
            if status == "OK":
                messages.append(parts)
    except Exception as exc:  # noqa: BLE001 — IMAP and socket errors are outcomes
        error = _fail(db, f"{type(exc).__name__}: {exc}")
        db.commit()
        integrations.log(f"[email] fetch failed: {error}", db)
        return {**counts, "ok": False, "error": error}
    finally:
        if conn is not None:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass

    known = _known_senders(db)
    parsed_all = [p for p in (_parse(parts) for parts in messages) if p is not None]
    counts["fetched"] = len(parsed_all)
    ids = [p["message_id"] for p in parsed_all]
    already = {m for (m,) in db.query(models.EmailMessage.message_id).filter(
        models.EmailMessage.message_id.in_(ids)).all()} if ids else set()
    with change_log.batch_scope(db, "email_sync"):
        for parsed in parsed_all:
            if parsed["message_id"] in already:
                continue
            who = known.get(parsed["from_addr"])
            if who is None:
                counts["ignored"] += 1              # an unknown sender: counted, never stored
                continue
            already.add(parsed["message_id"])
            _store(db, parsed, who, counts)
        _ok(db, synced=True)
        if validity is not None:
            seen = max([u for u in (cursor, batch[-1] if batch else None) if u is not None], default=None)
            integrations.set_config(_row(db), mail_uid=seen, mail_uidvalidity=validity)
            if not forced:
                # While mail is still waiting, the next sync must start from the same day.
                integrations.set_config(
                    _row(db), mail_backlog_since=since.isoformat() if counts["backlog"] else None)
        db.commit()
    return counts


def list_messages(db: Session, *, lead_id: int | None = None, client_id: int | None = None,
                  limit: int = 50) -> list[dict]:
    query = db.query(models.EmailMessage)
    if lead_id is not None:
        query = query.filter(models.EmailMessage.lead_id == lead_id)
    if client_id is not None:
        query = query.filter(models.EmailMessage.client_id == client_id)
    rows = query.order_by(models.EmailMessage.received_at.desc(), models.EmailMessage.id.desc()).limit(limit).all()
    return [serialize(m) for m in rows]


# ── Endpoints ────────────────────────────────────────────────

@router.post("/integrations/email/sync")
def sync_endpoint(db: Session = Depends(get_db)):
    if not configured():
        raise HTTPException(503, "Set EMAIL_ADDRESS and EMAIL_APP_PASSWORD first")
    return fetch(db)


@router.post("/integrations/email/test")
def test_endpoint(db: Session = Depends(get_db)):
    """Send a test message to Bandile's own address."""
    if not configured():
        raise HTTPException(503, "Set EMAIL_ADDRESS and EMAIL_APP_PASSWORD first")
    result = send(db, to=address(), subject="Founder OS test", body="Email sending works.", store=False)
    if result["ok"]:
        _ok(db)
    db.commit()
    return {"ok": result["ok"], "error": result["error"]}


@router.get("/email/messages")
def messages_endpoint(lead_id: int | None = None, client_id: int | None = None,
                      limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db)):
    return list_messages(db, lead_id=lead_id, client_id=client_id, limit=limit)
