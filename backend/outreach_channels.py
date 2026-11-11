"""
Outreach channels — how an approved message leaves (Phase 5B).

A channel never decides whether to send: ``outreach.execute_send`` calls it only after a
human approval. Each returns ``{"ok": bool, "external_id"?, "open_url"?, "error"?}``.

  whatsapp            — no API: a wa.me link the user opens; the tap is the send
  email               — SMTP through the email integration once it is configured
  linkedin/call/other — manual: the user copies the text and the tap marks it sent
"""

from urllib.parse import quote


class Channel:
    name = "other"

    def send(self, db, message, lead) -> dict:      # pragma: no cover - interface
        raise NotImplementedError


class WhatsAppLinkChannel(Channel):
    name = "whatsapp"

    def send(self, db, message, lead) -> dict:
        number = lead.whatsapp or lead.phone or ""
        digits = "".join(ch for ch in number if ch.isdigit())
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


def get_channel(name: str) -> Channel:
    if name == "whatsapp":
        return WhatsAppLinkChannel()
    if name == "email":
        return EmailChannel()
    return ManualChannel(name if name in CHANNELS else "other")
