"""
Outreach channels — how an approved message leaves (Phase 5B).

A channel never decides whether to send: ``outreach.execute_send`` calls it only after a
human approval. Each returns ``{"ok": bool, "external_id"?, "open_url"?, "error"?}``.

  whatsapp            — no API: a wa.me link the user opens; the tap is the send
  email               — SMTP through the email integration once it is configured
  linkedin/call/other — manual: the user copies the text and the tap marks it sent
"""

import os
from urllib.parse import quote


def default_country_code() -> str:
    """DEFAULT_COUNTRY_CODE as digits (default 260, Zambia)."""
    digits = "".join(ch for ch in (os.getenv("DEFAULT_COUNTRY_CODE") or "") if ch.isdigit()).lstrip("0")
    return digits or "260"


def whatsapp_digits(number) -> str:
    """The digits wa.me needs: country code first, no plus, no leading zero.

    A number written with ``+`` or ``00`` is international and kept as given. A number
    with one leading zero is local, so the zero is replaced by the default country code.
    With neither: ten digits or fewer is a local number without its zero (the country
    code is added); anything longer already carries a country code."""
    raw = (number or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return ""
    cc = default_country_code()
    if raw.lstrip(" (").startswith("+"):
        return digits
    if digits.startswith("00"):
        return digits[2:].lstrip("0")
    if digits.startswith("0"):
        return cc + digits[1:]
    if digits.startswith(cc) and len(digits) >= len(cc) + 8:
        return digits
    if len(digits) <= 10:
        return cc + digits
    return digits


def recipient(lead, channel: str) -> str | None:
    """Where a message on this channel would go for this lead, as it is shown to Bandile
    before he approves: the WhatsApp number with its country code, or the email address.
    None for a manual channel (nothing is sent) or when there is nothing on file."""
    if lead is None:
        return None
    if channel == "whatsapp":
        digits = whatsapp_digits(lead.whatsapp or lead.phone or "")
        return f"+{digits}" if digits else None
    if channel == "email":
        return (lead.email or "").strip() or None
    return None


class Channel:
    name = "other"

    def send(self, db, message, lead) -> dict:      # pragma: no cover - interface
        raise NotImplementedError


class WhatsAppLinkChannel(Channel):
    name = "whatsapp"

    def send(self, db, message, lead) -> dict:
        number = lead.whatsapp or lead.phone or ""
        digits = whatsapp_digits(number)
        if not digits:
            return {"ok": False, "error": "no WhatsApp number on file for this lead"}
        return {"ok": True, "open_url": f"https://wa.me/{digits}?text={quote(message.body or '')}"}


class EmailChannel(Channel):
    name = "email"
    NOT_CONFIGURED = "email sending is not configured"

    def send(self, db, message, lead) -> dict:
        try:
            import integrations_email
        except ImportError:
            return {"ok": False, "error": self.NOT_CONFIGURED}
        if not integrations_email.configured():
            return {"ok": False, "error": self.NOT_CONFIGURED}
        if not lead.email:
            return {"ok": False, "error": "no email address on file for this lead"}
        result = integrations_email.send(db, to=lead.email, subject=message.subject or "",
                                         body=message.body or "", lead_id=lead.id,
                                         outreach_message_id=message.id)
        if result.get("ok"):
            return {"ok": True, "external_id": result.get("message_id")}
        return {"ok": False, "error": result.get("error") or "email could not be sent"}


class ManualChannel(Channel):
    """LinkedIn, calls and anything else: nothing to automate."""

    def __init__(self, name: str = "other"):
        self.name = name

    def send(self, db, message, lead) -> dict:
        return {"ok": True}


CHANNELS = ("whatsapp", "email", "linkedin", "call", "other")
# Channels through which a message actually leaves once it is approved. What they send
# cannot be unsent, so the record of it is never undone.
EXTERNAL = ("whatsapp", "email")


def get_channel(name: str) -> Channel:
    if name == "whatsapp":
        return WhatsAppLinkChannel()
    if name == "email":
        return EmailChannel()
    return ManualChannel(name if name in CHANNELS else "other")
