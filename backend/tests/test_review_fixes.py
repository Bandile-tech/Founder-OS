"""
Review-round fixes on the Phase 5/6 branch. Each section reproduces one reported problem
before its fix; the tests stay as the regression guard.

No network and no OpenAI.
"""

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import llm
import models
from tests.conftest import TestSessionLocal, client, engine


@pytest.fixture(autouse=True)
def _no_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    llm.reset_client()
    yield
    llm.reset_client()


def _db():
    return TestSessionLocal()


# ── 2. WhatsApp links for local numbers ───────────────────────

@pytest.mark.parametrize("stored", [
    "0977123456",            # local, with the trunk zero
    "977123456",             # local, no zero and no country code
    "+260977123456",         # international
    "260977123456",          # country code, no plus
    "+260 977 123 456",      # as people type it
    "(0977) 123-456",
    "00260977123456",        # international call prefix
])
def test_whatsapp_link_carries_the_country_code(stored, monkeypatch):
    import outreach_channels as oc

    monkeypatch.delenv("DEFAULT_COUNTRY_CODE", raising=False)
    assert oc.whatsapp_digits(stored) == "260977123456"
    lead = models.Lead(name="Mercy Banda", whatsapp=stored)
    message = models.OutreachMessage(body="Hi Mercy")
    out = oc.WhatsAppLinkChannel().send(None, message, lead)
    assert out == {"ok": True, "open_url": "https://wa.me/260977123456?text=Hi%20Mercy"}


def test_whatsapp_country_code_is_configurable_and_foreign_numbers_are_left_alone(monkeypatch):
    import outreach_channels as oc

    monkeypatch.setenv("DEFAULT_COUNTRY_CODE", "+27")
    assert oc.whatsapp_digits("0821234567") == "27821234567"
    assert oc.whatsapp_digits("821234567") == "27821234567"
    assert oc.whatsapp_digits("+260977123456") == "260977123456"       # a plus is always trusted
    monkeypatch.delenv("DEFAULT_COUNTRY_CODE")
    assert oc.whatsapp_digits("+27821234567") == "27821234567"
    assert oc.whatsapp_digits("27821234567") == "27821234567"          # too long to be local
    assert oc.whatsapp_digits("+1 (202) 555-0123") == "12025550123"
    assert oc.whatsapp_digits("") == "" and oc.whatsapp_digits(None) == ""
    lead = models.Lead(name="No Number")
    assert oc.WhatsAppLinkChannel().send(None, models.OutreachMessage(body="x"), lead)["ok"] is False
