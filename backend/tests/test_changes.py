"""
pytest suite for three targeted changes:

  1. /parse  → revenue_updates produces Revenue rows
  2. DailyLog.weekly_target_id  is nullable (standalone entries)
  3. /chat   → today's DailyLog entries are injected into the AI context
"""

import json
import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import models
from main import app
# Engine, session, client, override, and fresh_db fixture come from conftest.py
from tests.conftest import TestSessionLocal, client


# ════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════

def _db() -> "Session":
    return TestSessionLocal()


def _fake_parse_response(overrides: dict) -> dict:
    """Minimal valid parse response with optional overrides."""
    base = {
        "summary": "test",
        "kpi_updates": [],
        "todos_add": [],
        "todos_complete": [],
        "roadmap_complete": [],
        "habits_done": [],
        "annual_updates": [],
        "revenue_updates": [],
        "log_entry": "test log",
        "advisory": None,
    }
    base.update(overrides)
    return base


# ════════════════════════════════════════════════════════════════
# 1. /parse  →  revenue_updates creates Revenue rows
# ════════════════════════════════════════════════════════════════

class TestParseRevenueUpdates:

    def test_revenue_row_created_from_parse(self):
        """Revenue rows must be persisted when revenue_updates is non-empty."""
        payload = _fake_parse_response({
            "revenue_updates": [
                {"amount": 150.0, "source": "Wamu Bakes website", "client": None}
            ],
            "log_entry": "signed first client",
        })

        with patch("main.get_parse_response", return_value=payload):
            resp = client.post("/parse", json={"text": "got paid $150"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["revenue_logged"] == [{"amount": 150.0, "source": "Wamu Bakes website"}]

        db = _db()
        rows = db.query(models.Revenue).all()
        assert len(rows) == 1
        assert rows[0].amount == 150.0
        assert rows[0].source == "Wamu Bakes website"
        assert rows[0].client_id is None
        db.close()

    def test_revenue_linked_to_client_by_name(self):
        """When client name matches an existing Client, client_id is set."""
        db = _db()
        c = models.Client(name="Insurance CEO", company="Acme", status="active")
        db.add(c)
        db.commit()
        db.refresh(c)
        client_id = c.id
        db.close()

        payload = _fake_parse_response({
            "revenue_updates": [
                {"amount": 500.0, "source": "PA automation", "client": "Insurance CEO"}
            ],
        })

        with patch("main.get_parse_response", return_value=payload):
            resp = client.post("/parse", json={"text": "invoiced insurance CEO"})

        assert resp.status_code == 200
        db = _db()
        row = db.query(models.Revenue).first()
        assert row is not None
        assert row.client_id == client_id
        db.close()

    def test_revenue_zero_amount_skipped(self):
        """Revenue entries with amount <= 0 must not create rows."""
        payload = _fake_parse_response({
            "revenue_updates": [{"amount": 0, "source": "nothing", "client": None}],
        })

        with patch("main.get_parse_response", return_value=payload):
            resp = client.post("/parse", json={"text": "nothing happened"})

        assert resp.status_code == 200
        db = _db()
        assert db.query(models.Revenue).count() == 0
        db.close()

    def test_multiple_revenue_entries(self):
        """Multiple revenue_updates in one parse all become rows."""
        payload = _fake_parse_response({
            "revenue_updates": [
                {"amount": 100.0, "source": "client A", "client": None},
                {"amount": 200.0, "source": "client B", "client": None},
            ],
        })

        with patch("main.get_parse_response", return_value=payload):
            resp = client.post("/parse", json={"text": "two payments"})

        assert resp.status_code == 200
        db = _db()
        assert db.query(models.Revenue).count() == 2
        db.close()

    def test_parse_without_revenue_updates_is_fine(self):
        """Omitting revenue_updates entirely must not error."""
        payload = _fake_parse_response({})
        del payload["revenue_updates"]   # simulate old-style response

        with patch("main.get_parse_response", return_value=payload):
            resp = client.post("/parse", json={"text": "nothing financial"})

        assert resp.status_code == 200
        assert resp.json()["revenue_logged"] == []


# ════════════════════════════════════════════════════════════════
# 2. DailyLog.weekly_target_id  is nullable
# ════════════════════════════════════════════════════════════════

class TestNullableDailyLog:

    def test_dailylog_created_without_weekly_target(self):
        """Inserting a DailyLog with weekly_target_id=None must not raise."""
        db = _db()
        log = models.DailyLog(date=date.today(), entry="standalone entry",
                              weekly_target_id=None, impact_score=0)
        db.add(log)
        db.commit()
        db.refresh(log)
        assert log.id is not None
        assert log.weekly_target_id is None
        db.close()

    def test_post_logs_without_target_id(self):
        """POST /logs with no weekly_target_id must return 200, not 404."""
        resp = client.post("/logs", json={
            "date": str(date.today()),
            "entry": "standalone brain dump",
        })
        assert resp.status_code == 200
        body = resp.json()
        assert "log_id" in body

    def test_post_logs_with_missing_target_returns_404(self):
        """POST /logs with a non-existent weekly_target_id must still return 404."""
        resp = client.post("/logs", json={
            "date": str(date.today()),
            "entry": "linked entry",
            "weekly_target_id": 9999,
        })
        assert resp.status_code == 404

    def test_parse_creates_standalone_dailylog(self):
        """Brain dump parse must persist the log_entry as a standalone DailyLog row."""
        payload = _fake_parse_response({"log_entry": "trained hard today"})

        with patch("main.get_parse_response", return_value=payload):
            resp = client.post("/parse", json={"text": "trained hard today"})

        assert resp.status_code == 200
        db = _db()
        logs = db.query(models.DailyLog).filter(
            models.DailyLog.weekly_target_id == None  # noqa: E711
        ).all()
        assert len(logs) >= 1
        assert any("trained hard" in (l.entry or "") for l in logs)
        db.close()

    def test_get_today_logs_endpoint(self):
        """GET /logs/today must return all of today's entries."""
        db = _db()
        db.add(models.DailyLog(date=date.today(), entry="entry one",
                               weekly_target_id=None, impact_score=0))
        db.add(models.DailyLog(date=date.today(), entry="entry two",
                               weekly_target_id=None, impact_score=0))
        db.commit()
        db.close()

        resp = client.get("/logs/today")
        assert resp.status_code == 200
        entries = [item["entry"] for item in resp.json()]
        assert "entry one" in entries
        assert "entry two" in entries


# ════════════════════════════════════════════════════════════════
# 3. /chat  →  today's DailyLog entries are injected into context
# ════════════════════════════════════════════════════════════════

class TestChatLogInjection:

    def _setup_logs(self, entries: list[str]):
        """Insert DailyLog rows for today with the given entries."""
        db = _db()
        for entry in entries:
            db.add(models.DailyLog(date=date.today(), entry=entry,
                                   weekly_target_id=None, impact_score=0))
        db.commit()
        db.close()

    def test_log_entries_appear_in_ai_prompt(self):
        """Today's DailyLog entries must appear in the message sent to the AI."""
        self._setup_logs(["ran 200m in 22.8s", "finished maths chapter"])

        captured_messages = []

        def fake_chat(messages, db, context_type):
            captured_messages.extend(messages)
            return "acknowledged"

        with patch("main.get_chat_response_with_memory", side_effect=fake_chat):
            resp = client.post("/chat", json={
                "message": "what did I do today?",
                "session_id": "test-session-001",
            })

        assert resp.status_code == 200
        assert resp.json()["reply"] == "acknowledged"

        # The last user message (with injected context) must reference both log lines
        user_msg = next(m for m in reversed(captured_messages) if m["role"] == "user")
        assert "ran 200m in 22.8s" in user_msg["content"]
        assert "finished maths chapter" in user_msg["content"]

    def test_log_section_label_present(self):
        """The injected block must have the [Today's activity log] header."""
        self._setup_logs(["habit: scripture done"])

        captured = []

        def fake_chat(messages, db, context_type):
            captured.extend(messages)
            return "ok"

        with patch("main.get_chat_response_with_memory", side_effect=fake_chat):
            client.post("/chat", json={
                "message": "summary?",
                "session_id": "test-session-002",
            })

        user_msg = next(m for m in reversed(captured) if m["role"] == "user")
        assert "[Today's activity log]" in user_msg["content"]

    def test_no_logs_today_no_injection(self):
        """When there are no DailyLog rows today, no log block is injected."""
        captured = []

        def fake_chat(messages, db, context_type):
            captured.extend(messages)
            return "ok"

        with patch("main.get_chat_response_with_memory", side_effect=fake_chat):
            client.post("/chat", json={
                "message": "hello",
                "session_id": "test-session-003",
            })

        user_msg = next(m for m in reversed(captured) if m["role"] == "user")
        assert "[Today's activity log]" not in user_msg["content"]

    def test_client_context_and_log_both_injected(self):
        """Both client-supplied context and DailyLog entries should appear."""
        self._setup_logs(["400m trial: 54.2s"])

        captured = []

        def fake_chat(messages, db, context_type):
            captured.extend(messages)
            return "ok"

        with patch("main.get_chat_response_with_memory", side_effect=fake_chat):
            client.post("/chat", json={
                "message": "review",
                "session_id": "test-session-004",
                "context": {"kpis": {"sprint_400m": 54.2}},
            })

        user_msg = next(m for m in reversed(captured) if m["role"] == "user")
        assert "400m trial: 54.2s" in user_msg["content"]
        assert "sprint_400m" in user_msg["content"]

    def test_logs_from_other_dates_not_injected(self):
        """DailyLog rows from past dates must not appear in today's injection."""
        from datetime import timedelta
        yesterday = date.today() - timedelta(days=1)
        db = _db()
        db.add(models.DailyLog(date=yesterday, entry="yesterday's entry",
                               weekly_target_id=None, impact_score=0))
        db.commit()
        db.close()

        captured = []

        def fake_chat(messages, db, context_type):
            captured.extend(messages)
            return "ok"

        with patch("main.get_chat_response_with_memory", side_effect=fake_chat):
            client.post("/chat", json={
                "message": "what happened?",
                "session_id": "test-session-005",
            })

        user_msg = next(m for m in reversed(captured) if m["role"] == "user")
        assert "yesterday's entry" not in user_msg["content"]


# ════════════════════════════════════════════════════════════════
# 4. /parse  →  fuzzy habit matching (Fix 1)
# ════════════════════════════════════════════════════════════════

class TestFuzzyHabitMatching:
    """AI may return short/partial habit keys; all three tiers must resolve."""

    def _seed_habits(self):
        """Insert today's habit rows for all defaults."""
        from datetime import date as _date
        db = _db()
        from models import Habit as _Habit
        DEFAULTS = [
            ("scripture_prayer", "Scripture & Prayer (pre-5:20am)"),
            ("ironing",          "Clothes ironed night before"),
            ("python_session",   "Python / Aether (20:30–21:30)"),
            ("sprint_training",  "Sprint training"),
            ("academics",        "Academic study block"),
        ]
        today = _date.today()
        for key, label in DEFAULTS:
            db.add(_Habit(key=key, label=label, done=False, date=today))
        db.commit()
        db.close()

    def _habit_done(self, key: str) -> bool:
        from datetime import date as _date
        from models import Habit as _Habit
        db = _db()
        h = db.query(_Habit).filter(_Habit.key == key, _Habit.date == _date.today()).first()
        result = h.done if h else False
        db.close()
        return result

    def test_exact_key_match(self):
        """Exact key 'scripture_prayer' ticks that habit."""
        self._seed_habits()
        payload = _fake_parse_response({"habits_done": ["scripture_prayer"]})
        with patch("main.get_parse_response", return_value=payload):
            resp = client.post("/parse", json={"text": "did scripture"})
        assert resp.status_code == 200
        assert self._habit_done("scripture_prayer") is True

    def test_label_substring_match_scripture(self):
        """AI returns 'scripture'; label 'Scripture & Prayer…' must match via substring."""
        self._seed_habits()
        payload = _fake_parse_response({"habits_done": ["scripture"]})
        with patch("main.get_parse_response", return_value=payload):
            resp = client.post("/parse", json={"text": "did scripture today"})
        assert resp.status_code == 200
        assert self._habit_done("scripture_prayer") is True

    def test_label_substring_match_prayer(self):
        """AI returns 'prayer'; should still resolve to scripture_prayer."""
        self._seed_habits()
        payload = _fake_parse_response({"habits_done": ["prayer"]})
        with patch("main.get_parse_response", return_value=payload):
            resp = client.post("/parse", json={"text": "did my prayer"})
        assert resp.status_code == 200
        assert self._habit_done("scripture_prayer") is True

    def test_keyword_overlap_match_training(self):
        """AI returns 'sprint session'; 'sprint' overlaps with 'Sprint training'."""
        self._seed_habits()
        payload = _fake_parse_response({"habits_done": ["sprint session"]})
        with patch("main.get_parse_response", return_value=payload):
            resp = client.post("/parse", json={"text": "finished sprint session"})
        assert resp.status_code == 200
        assert self._habit_done("sprint_training") is True

    def test_keyword_overlap_match_python(self):
        """AI returns 'python'; overlaps with label 'Python / Aether'."""
        self._seed_habits()
        payload = _fake_parse_response({"habits_done": ["python"]})
        with patch("main.get_parse_response", return_value=payload):
            resp = client.post("/parse", json={"text": "python session done"})
        assert resp.status_code == 200
        assert self._habit_done("python_session") is True

    def test_no_match_does_not_crash(self):
        """Unknown habit key with no overlap must not crash and returns 200."""
        self._seed_habits()
        payload = _fake_parse_response({"habits_done": ["xyzzy_nonexistent"]})
        with patch("main.get_parse_response", return_value=payload):
            resp = client.post("/parse", json={"text": "nothing relevant"})
        assert resp.status_code == 200
        # Nothing should have been ticked
        assert self._habit_done("scripture_prayer") is False


# ════════════════════════════════════════════════════════════════
# 5. /input  →  apply_parse_updates called; full updates surfaced
# ════════════════════════════════════════════════════════════════

class TestUnifiedInputAppliesUpdates:
    """
    Verifies that /input's parse path uses apply_parse_updates so that
    habits, revenue, and multi-event dumps are all correctly applied
    and returned in the response's 'updates' object.
    """

    SESSION = "test-input-session"

    def _seed_habits(self):
        from datetime import date as _date
        from models import Habit as _Habit
        db = _db()
        DEFAULTS = [
            ("scripture_prayer", "Scripture & Prayer (pre-5:20am)"),
            ("ironing",          "Clothes ironed night before"),
            ("python_session",   "Python / Aether (20:30–21:30)"),
            ("sprint_training",  "Sprint training"),
            ("academics",        "Academic study block"),
        ]
        for key, label in DEFAULTS:
            db.add(_Habit(key=key, label=label, done=False, date=_date.today()))
        db.commit()
        db.close()

    def _habit_done(self, key: str) -> bool:
        from datetime import date as _date
        from models import Habit as _Habit
        db = _db()
        h = db.query(_Habit).filter(_Habit.key == key, _Habit.date == _date.today()).first()
        result = h.done if h else False
        db.close()
        return result

    def test_input_ticks_habit_and_returns_it(self):
        """
        /input with 'did scripture today':
        - scripture_prayer habit row becomes done=True
        - response['updates']['habits_updated'] contains scripture_prayer
        """
        self._seed_habits()
        payload = _fake_parse_response({"habits_done": ["scripture_prayer"]})

        with patch("main.get_parse_response", return_value=payload):
            resp = client.post("/input", json={
                "text": "did scripture today",
                "session_id": self.SESSION,
            })

        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "log"
        assert "updates" in data

        habit_keys = [h["key"] for h in data["updates"]["habits_updated"]]
        assert "scripture_prayer" in habit_keys
        assert self._habit_done("scripture_prayer") is True

    def test_input_creates_revenue_and_returns_it(self):
        """
        /input with 'earned K500 from Wamu':
        - Revenue row created in DB
        - response['updates']['revenue_logged'] contains the entry
        """
        payload = _fake_parse_response({
            "revenue_updates": [{"amount": 500.0, "source": "Wamu's Bakes", "client": None}]
        })

        with patch("main.get_parse_response", return_value=payload):
            resp = client.post("/input", json={
                "text": "earned K500 from Wamu's",
                "session_id": self.SESSION,
            })

        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "log"
        rev = data["updates"]["revenue_logged"]
        assert len(rev) == 1
        assert rev[0]["amount"] == 500.0
        assert "Wamu" in rev[0]["source"]

        db = _db()
        from models import Revenue as _Rev
        rows = db.query(_Rev).all()
        assert any(r.amount == 500.0 for r in rows)
        db.close()

    def test_input_multi_event_applies_both(self):
        """
        Multi-event dump: habit tick + revenue in one call — both applied.
        """
        self._seed_habits()
        payload = _fake_parse_response({
            "habits_done": ["scripture_prayer"],
            "revenue_updates": [{"amount": 250.0, "source": "consulting", "client": None}],
        })

        with patch("main.get_parse_response", return_value=payload):
            resp = client.post("/input", json={
                "text": "did scripture, earned K250 from consulting",
                "session_id": self.SESSION,
            })

        assert resp.status_code == 200
        data = resp.json()
        assert self._habit_done("scripture_prayer") is True
        assert len(data["updates"]["revenue_logged"]) == 1
        assert len(data["updates"]["habits_updated"]) == 1

    def test_input_updates_object_structure(self):
        """Response always contains all expected keys in updates, even when empty."""
        payload = _fake_parse_response({})  # nothing to apply

        with patch("main.get_parse_response", return_value=payload):
            resp = client.post("/input", json={
                "text": "all quiet today",
                "session_id": self.SESSION,
            })

        assert resp.status_code == 200
        u = resp.json()["updates"]
        for key in ("habits_updated", "kpi_updates_applied", "todos_added",
                    "roadmap_completed", "annual_updates_applied",
                    "revenue_logged", "log_entry_created"):
            assert key in u, f"Missing key: {key}"

    def test_parse_response_still_backward_compatible(self):
        """
        /parse must still return legacy top-level fields
        kpi_updates_applied and revenue_logged unchanged.
        """
        payload = _fake_parse_response({
            "revenue_updates": [{"amount": 100.0, "source": "test", "client": None}],
        })

        with patch("main.get_parse_response", return_value=payload):
            resp = client.post("/parse", json={"text": "test"})

        assert resp.status_code == 200
        data = resp.json()
        # Legacy fields still present at top level
        assert "kpi_updates_applied" in data
        assert "revenue_logged" in data
        # New unified updates object also present
        assert "updates" in data
        assert data["revenue_logged"] == data["updates"]["revenue_logged"]
