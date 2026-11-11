"""The assistant answers from the current sources only (OpenAI mocked throughout).

* No tool, no part of the assistant's context and not the briefing reads a legacy table
  (old subject/topic/subtopic mastery, roadmap_tasks, kpi_snapshots, the weekly-target
  scoreboard, trading). The day's ticks in ``habits`` are read only through
  ``assistant_state`` (always joined to the active non-negotiables); ``subjects`` is read
  only by ``syllabus`` for subject names and the exam-date fallback.
* What the model sees from a tool is plain language: no raw booleans, nulls, keys or codes.
* Revenue is reported in Kwacha with USD converted; targets come from annual_targets.
* The Conversations history endpoint and the "history" view.
"""

import json
import re
import sys
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine

sys.path.insert(0, str(Path(__file__).parent.parent))

import briefing
import conversation
import llm
import models
import orchestrator
import orchestrator_tools as ot
from tests.conftest import TestSessionLocal, client

TODAY = date.today()

LEGACY_TABLES = ("topics", "subtopics", "roadmap_tasks", "kpi_snapshots", "weekly_targets",
                 "weekly_target_snapshots", "prop_firm_accounts", "backtest_trades", "live_trades",
                 "social_scores")
LEGACY_RE = re.compile(r'(?<![\w.])"?(' + "|".join(LEGACY_TABLES) + r')"?(?![\w])', re.I)
HABITS_RE = re.compile(r'(?<![\w.])"?habits"?(?![\w])', re.I)
SUBJECTS_RE = re.compile(r'(?<![\w.])"?subjects"?(?![\w])', re.I)


def _db():
    return TestSessionLocal()


@pytest.fixture(autouse=True)
def _no_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ZMW_PER_USD", "27")
    llm.reset_client()
    yield


def _seed_legacy_and_current():
    """Legacy tables full of the numbers the assistant used to quote, plus current data."""
    db = _db()
    # Legacy: old mastery (Maths 94.5%), roadmap 7/7, KPI store, raw habit rows.
    subj = models.Subject(name="Maths 9709", code="9709", exam_date=TODAY + timedelta(days=40))
    db.add(subj)
    db.flush()
    topic = models.Topic(subject_id=subj.id, name="Pure 1")
    db.add(topic)
    db.flush()
    db.add(models.Subtopic(topic_id=topic.id, name="Quadratics", mastery_level=94))
    for i in range(7):
        db.add(models.RoadmapTask(task_id=f"fos-{i}", roadmap="founder_os", done=True))
    db.add(models.KPISnapshot(key="maths_syllabus", value=94.5, date=TODAY))
    # Current sources.
    db.add(models.NonNegotiable(key="scripture_prayer", label="Scripture & Prayer", is_active=True, sort_order=0))
    db.add(models.NonNegotiable(key="ironing", label="Clothes ironed", is_active=True, sort_order=1))
    db.add(models.NonNegotiable(key="old_habit", label="Retired habit", is_active=False, sort_order=2))
    db.add(models.Habit(key="scripture_prayer", label="Scripture & Prayer", date=TODAY, done=True))
    db.add(models.Habit(key="old_habit", label="Retired habit", date=TODAY, done=True))
    db.add(models.SyllabusPoint(subject_code="9709", subject_name="Maths 9709", paper="P1",
                                topic_code="1.1", title="Quadratics", status="bulletproof"))
    db.add(models.SyllabusPoint(subject_code="9709", subject_name="Maths 9709", paper="P1",
                                topic_code="1.3", title="Coordinate geometry", status="not_started"))
    db.add(models.SyllabusPoint(subject_code="9709", subject_name="Maths 9709", paper="S1",
                                topic_code="1.2", title="Probability", status="learned",
                                weakness_notes="- 2026-11-01: conditional probability"))
    db.add(models.AnnualTarget(name="AI services revenue", current_value=520, target_value=2000,
                               unit="USD", is_active=True))
    db.add(models.AnnualTarget(name="Apply to Wharton", display_value="Essays drafted", is_active=True))
    db.add(models.Revenue(amount=500, currency="ZMW", source="Wamu's Bakes", date=TODAY))
    db.add(models.Revenue(amount=20, currency="USD", source="Consulting", date=TODAY))
    db.add(models.Todo(text="Send the proposal", priority=1, done=False, due_date=TODAY))
    db.add(models.Client(name="Bel Baby", status="active"))
    db.commit()
    db.close()


# ════════════════════════════════════════════════════════════════
# 1. No tool, context or briefing reads a legacy table
# ════════════════════════════════════════════════════════════════

class _SqlRecorder:
    def __init__(self):
        self.violations: list[str] = []

    def __call__(self, conn, cursor, statement, parameters, context, executemany):
        files = [f.filename.replace("\\", "/") for f in traceback.extract_stack()]
        if LEGACY_RE.search(statement):
            self.violations.append(f"legacy table: {statement[:160]}")
        if HABITS_RE.search(statement) and not any(f.endswith("/assistant_state.py") for f in files):
            self.violations.append(f"habits read outside assistant_state: {statement[:160]}")
        if SUBJECTS_RE.search(statement) and not any(f.endswith("/syllabus.py") for f in files):
            self.violations.append(f"subjects read outside syllabus: {statement[:160]}")


def _parsed(**over):
    base = {"summary": "Logged.", "todos_add": [], "todos_complete": [], "habits_done": [],
            "annual_updates": [], "reading_updates": [], "bible_log": [], "revenue_updates": [],
            "log_entry": None, "advisory": None}
    base.update(over)
    return base


def _run_every_tool(db) -> dict:
    """Call every registered tool once with plausible arguments. Returns name → model text."""
    out = {}
    fake_research = {"opportunities": [], "project_id": 1, "rejected_count": 0}
    import agents.runner
    with patch("main.get_parse_response", return_value=_parsed(habits_done=["ironing"])), \
         patch.object(agents.runner, "SessionLocal", TestSessionLocal), \
         patch("market_intel.agent.run_market_research", return_value=fake_research):
        calls = [
            ("route_brain_dump", {"text": "ironed my clothes"}),
            ("get_dashboard_state", {}),
            ("detect_off_track", {}),
            ("synthesize_weekly_review", {}),
            ("surface_weakest_topics", {"limit": 3}),
            ("surface_weakest_subtopic", {}),
            ("query_war_room", {"q": "doctrine"}),
            ("add_todos", {"items": ["Call the bank", "Book the dentist"]}),
            ("complete_task", {"query": "Call the bank"}),
            ("query_cold_archive", {"query": "pricing"}),
            ("run_market_research_agent", {"objective": "find painful problems"}),
            ("promote_research_opportunity", {"finding_id": 999}),
            ("remember", {"text": "Prefers evening study blocks", "kind": "preference"}),
            ("notes_create", {"title": "Deep Work", "kind": "book", "body": "Chapter 1"}),
            ("notes_append", {"title": "Deep Work", "text": "Finished chapter 6", "kind": "book"}),
            ("notes_search", {"query": "chapter"}),
            ("notes_read", {"title": "Deep Work"}),
            ("get_deadlines", {"days": 14}),
            ("update_syllabus", {"topic": "Coordinate geometry", "status": "revised"}),
            ("set_exam_date", {"subject": "Maths", "date": (TODAY + timedelta(days=30)).isoformat(), "paper": "P1"}),
            ("show_view", {"view": "history", "params": {"q": "probability"}}),
            ("add_lead", {"name": "Mercy Banda", "company": "Sunrise Pharmacy", "segment": "lusaka pharmacies"}),
            ("find_leads", {"segment": "lusaka pharmacies"}),
            ("crm_lookup", {"query": "Bel Baby"}),
            ("log_communication", {"who": "Bel Baby", "summary": "Called about the delivery date"}),
            ("update_deal", {"deal": "Website build", "client": "Bel Baby", "amount": 4000, "create": True}),
            ("add_contact", {"client": "Bel Baby", "name": "Mrs Banda", "phone": "0977 000 222"}),
            ("record_payment", {"client": "Bel Baby", "amount": 1500, "status": "due", "due_date": "tomorrow"}),
            ("crm_summary", {}),
            ("qualify_leads", {"segment": "lusaka pharmacies"}),
            ("draft_outreach", {"lead": "Mercy Banda", "channel": "whatsapp", "brief": "intro"}),
            ("request_send", {"message_id": 1}),
            ("set_follow_up", {"lead": "Mercy Banda", "date": "in 3 days"}),
            ("log_lead_reply", {"lead": "Mercy Banda", "summary": "Wants a demo on Friday"}),
            ("convert_lead", {"lead": "Mercy Banda"}),
            ("update_project", {"create": True, "title": "Founder OS Phase 5", "priority": 1, "due_date": "in 3 weeks"}),
            ("log_project_update", {"project": "Founder OS Phase 5", "text": "Shipped the leads importer", "kind": "milestone"}),
            ("portfolio_review", {}),
            ("schedule_research", {"objective": "painful admin problems in Lusaka pharmacies", "cadence_days": 14}),
            ("dismiss_research_finding", {"finding_id": 999}),
            ("agent_status", {}),
            ("run_agent", {"key": "portfolio_reviewer"}),
            ("set_agent_schedule", {"key": "lead_qualifier", "interval_minutes": 240}),
            ("add_application", {"name": "Wharton", "institution": "University of Pennsylvania", "deadline": "1 January"}),
            ("import_requirements", {"application": "Wharton", "text": "Two essays of 500 words each are required."}),
            ("update_requirement", {"application": "Wharton", "requirement": "essays", "status": "in_progress"}),
            ("application_status", {"application": "Wharton"}),
            ("application_gaps", {"application": "Wharton"}),
            ("brand_performance", {}),
            ("add_content_idea", {"title": "A short on how I built the bakery site", "format": "short", "scheduled_for": "friday"}),
            ("mark_posted", {"idea": "bakery site"}),
            ("set_reminder", {"text": "call the clinic", "when": "in 2 hours"}),
            ("notification_settings", {"morning_time": "05:45"}),
            ("calendar_today", {}),
            ("calendar_add_event", {"title": "Clinic demo", "when": "friday 14:00"}),
            ("email_lookup", {"who": "Wamu"}),
        ]
        for name, args in calls:
            result, _summary = ot.run_tool(name, args, db)
            out[name] = ot.for_model(name, result)
        dentist = db.query(models.Todo).filter(models.Todo.text == "Book the dentist").first()
        for name, args in [("propose_delete", {"table": "todos", "record_id": dentist.id})]:
            result, _ = ot.run_tool(name, args, db)
            out[name] = ot.for_model(name, result)
            pending_id = result["id"]
        result, _ = ot.run_tool("confirm_action", {"pending_id": pending_id}, db)
        out["confirm_action"] = ot.for_model("confirm_action", result)
        result, _ = ot.run_tool("propose_delete", {"table": "todos", "record_id": db.query(models.Todo).first().id}, db)
        result, _ = ot.run_tool("cancel_action", {"pending_id": result["id"]}, db)
        out["cancel_action"] = ot.for_model("cancel_action", result)
    return out


class TestNoLegacyReads:

    def test_every_tool_the_context_and_the_briefing_avoid_legacy_tables(self):
        _seed_legacy_and_current()
        rec = _SqlRecorder()
        event.listen(Engine, "before_cursor_execute", rec)
        try:
            db = _db()
            outputs = _run_every_tool(db)
            briefing.build_briefing(db, with_summary=False)
            ot.run_tool("show_view", {"view": "progress"}, db)

            class _Req:
                text = "What do you know?"
                session_id = "s"
                context = {"kpis": {"maths_syllabus": {"value": 94.5}}}
            messages, ctx = orchestrator._build_messages(_Req(), db)
            db.close()
        finally:
            event.remove(Engine, "before_cursor_execute", rec)
        assert set(outputs) >= set(ot._TOOL_DISPATCH), "every registered tool was exercised"
        assert rec.violations == []
        assert "94.5" not in json.dumps(messages) + json.dumps(ctx or {})

    def test_the_guard_catches_a_legacy_read(self):
        rec = _SqlRecorder()
        event.listen(Engine, "before_cursor_execute", rec)
        try:
            db = _db()
            db.query(models.RoadmapTask).all()
            db.query(models.Subtopic).all()
            db.query(models.Habit).all()               # raw habit rows, not via assistant_state
            db.query(models.Subject).all()             # subjects outside syllabus
            db.close()
        finally:
            event.remove(Engine, "before_cursor_execute", rec)
        joined = " | ".join(rec.violations)
        assert len(rec.violations) == 4
        assert "roadmap_tasks" in joined and "subtopics" in joined
        assert "habits read outside assistant_state" in joined and "subjects read outside syllabus" in joined

    def test_dispatch_covers_every_schema(self):
        names = {t["function"]["name"] for t in ot.TOOL_SCHEMAS}
        assert names <= set(ot._TOOL_DISPATCH)

    def test_dashboard_reports_current_sources_not_old_numbers(self):
        _seed_legacy_and_current()
        db = _db()
        state, summary = ot.get_dashboard_state(db)
        db.close()
        text = json.dumps(state)
        assert "94" not in text and "7/7" not in text and "10000" not in text and "10,000" not in text
        assert any("Maths 9709: syllabus" in line for line in state["academics"])
        assert "1 of 2 done today" in state["non-negotiables today"]          # retired habit ignored
        assert "Scripture & Prayer" in state["non-negotiables today"]
        assert "Clothes ironed" in state["non-negotiables today"]
        assert any(line.startswith("AI services revenue: 520 of 2,000 USD") for line in state["annual targets"])
        assert "K1,040" in state["revenue"]                                   # K500 + $20 × 27
        assert "converted at K27 per $1" in state["revenue"]
        assert "K1,040" in summary


# ════════════════════════════════════════════════════════════════
# 2. What the model sees is plain language
# ════════════════════════════════════════════════════════════════

class TestReadableToolOutput:

    RAW = ("true", "false", "null", "None", "True", "False", "scripture_prayer", "not_started",
           "on_track", "ten_k", "_kpi", "roadmap", "kpis", "habit_id", "ui_directive")

    def test_no_raw_booleans_nulls_or_field_names(self):
        _seed_legacy_and_current()
        db = _db()
        outputs = _run_every_tool(db)
        db.close()
        for name, text in outputs.items():
            data = json.loads(text)
            for token in self.RAW:
                assert not re.search(rf"(?<![\w]){re.escape(token)}(?![\w])", text), f"{name}: '{token}' in {text[:300]}"
            assert all("_" not in k for k in _keys(data)), f"{name}: snake_case key in {text[:300]}"

    def test_weakest_topics_are_sentences(self):
        _seed_legacy_and_current()
        db = _db()
        result, _ = ot.run_tool("surface_weakest_topics", {"limit": 2}, db)
        db.close()
        assert result["topics"][0].startswith("Maths 9709 P1 1.3 Coordinate geometry — not started")
        assert any("Latest weakness note: conditional probability" in t for t in result["topics"])

    def test_route_brain_dump_reports_what_changed(self):
        _seed_legacy_and_current()
        db = _db()
        with patch("main.get_parse_response", return_value=_parsed(habits_done=["ironing"])):
            result, _ = ot.run_tool("route_brain_dump", {"text": "ironed"}, db)
        db.close()
        assert result["changed"] == ["Non-negotiable done: Clothes ironed"]


def _keys(value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from _keys(v)
    elif isinstance(value, list):
        for v in value:
            yield from _keys(v)


# ════════════════════════════════════════════════════════════════
# 3. Routine, targets and revenue from the current sources
# ════════════════════════════════════════════════════════════════

class TestCurrentSources:

    def test_logging_a_non_negotiable_creates_the_days_tick(self):
        db = _db()
        db.add(models.NonNegotiable(key="sprint_training", label="Sprint training", is_active=True))
        db.commit()
        assert db.query(models.Habit).count() == 0      # no rows seeded for today
        with patch("main.get_parse_response", return_value=_parsed(habits_done=["sprint session"])):
            ot.run_tool("route_brain_dump", {"text": "did my sprint session"}, db)
        row = db.query(models.Habit).filter(models.Habit.key == "sprint_training").one()
        db.close()
        assert row.done is True and row.date == TODAY and row.label == "Sprint training"

    def test_inactive_non_negotiable_is_not_ticked(self):
        db = _db()
        db.add(models.NonNegotiable(key="ironing", label="Clothes ironed", is_active=False))
        db.add(models.NonNegotiable(key="academics", label="Study block", is_active=True))
        db.commit()
        with patch("main.get_parse_response", return_value=_parsed(habits_done=["ironing"])):
            result, _ = ot.run_tool("route_brain_dump", {"text": "ironed"}, db)
        assert db.query(models.Habit).count() == 0
        db.close()
        assert result["changed"] == ["Nothing was written."]

    def test_parser_is_told_the_active_non_negotiables_not_roadmap_ids(self):
        _seed_legacy_and_current()
        db = _db()
        with patch("main.get_parse_response", return_value=_parsed()) as parse:
            ot.run_tool("route_brain_dump", {"text": "x"}, db)
        db.close()
        context = parse.call_args[0][1]
        assert "roadmap_ids" not in context
        assert context["non_negotiables"] == [("scripture_prayer", "Scripture & Prayer"), ("ironing", "Clothes ironed")]

    def test_parser_prompt_has_no_roadmap_or_kpi_keys(self):
        import openai_client
        captured = {}

        def fake(messages, **kw):
            captured["system"] = messages[0]["content"]
            return json.dumps(_parsed())
        with patch("llm.chat_text", side_effect=fake):
            openai_client.get_parse_response("x", {"today": "2026-11-07",
                                                   "non_negotiables": [("ironing", "Clothes ironed")]})
        system = captured["system"]
        assert "roadmap" not in system.lower() and "kpi_updates" not in system
        assert '"habits_done": ["ironing"]' in system and "ironing = Clothes ironed" in system

    def test_weekly_review_uses_completed_tasks_syllabus_and_kwacha(self):
        _seed_legacy_and_current()
        db = _db()
        t = db.query(models.Todo).first()
        t.done, t.completed_at = True, TODAY
        p = db.query(models.SyllabusPoint).filter(models.SyllabusPoint.topic_code == "1.2").one()
        p.last_revised = TODAY
        db.commit()
        result, summary = ot.synthesize_weekly_review(db)
        db.close()
        assert "Completed: Send the proposal" in result["SHIPPED"]
        assert any(line.startswith("Syllabus: Maths 9709 S1 1.2 Probability is now learned but weak")
                   for line in result["SHIPPED"])
        assert not any("fos-" in line for line in result["SHIPPED"])
        assert result["REVENUE"][-1].startswith("Total this week: K1,040")
        assert "K1,040" in summary

    def test_briefing_business_is_the_revenue_target_not_the_10k_scoreboard(self):
        _seed_legacy_and_current()
        b = client.get("/briefing/today").json()
        assert b["progress"]["business"] == 26               # 520 of 2000
        db = _db()
        db.query(models.AnnualTarget).delete()
        db.commit()
        db.close()
        assert client.get("/briefing/today").json()["progress"]["business"] is None

    def test_orchestrator_ignores_client_context(self):
        captured = {}

        def fake(convo, tools):
            captured["convo"] = convo
            yield {"content": "ok"}
            yield {"finish_reason": "stop"}
        with patch("openai_client._stream_completion", fake):
            r = client.post("/orchestrator", json={
                "text": "What do you know?", "session_id": "s",
                "context": {"kpis": {"maths_syllabus": {"value": 94.5}}, "habits": {"scripture_prayer": True}}})
        assert r.status_code == 200
        blob = json.dumps(captured["convo"])
        assert "94.5" not in blob and "Live system context" not in blob and "scripture_prayer" not in blob

    def test_prompt_names_current_sources_and_drops_the_10k_milestone(self):
        import openai_client
        prompt = openai_client._load_orchestrator_prompt()
        assert "$10,000" not in prompt
        flat = " ".join(prompt.split())
        assert "call get_dashboard_state and answer from it" in flat and "call show_view history" in flat


# ════════════════════════════════════════════════════════════════
# 4. Conversations — history endpoint and the "history" view
# ════════════════════════════════════════════════════════════════

def _msg(db, role, text, when, **kw):
    row = conversation.save_message(db, role, text, **kw)
    row.created_at = when
    db.commit()
    return row


class TestConversationHistory:

    def _seed(self):
        db = _db()
        now = datetime.utcnow()
        yesterday = now - timedelta(days=1)
        _msg(db, "user", "Revised S1 probability", yesterday.replace(hour=9, minute=0))
        _msg(db, "assistant", "", yesterday.replace(hour=9, minute=1),
             tool_calls=[{"id": "c1", "type": "function", "function": {"name": "update_syllabus", "arguments": "{}"}}])
        _msg(db, "tool", '{"updated": "x"}', yesterday.replace(hour=9, minute=1), tool_call_id="c1")
        _msg(db, "assistant", "Marked S1 probability as revised.", yesterday.replace(hour=9, minute=2),
             changes=[{"id": 1}])
        _msg(db, "user", "What is due today?", now - timedelta(minutes=5))
        _msg(db, "assistant", "Two things are due.", now - timedelta(minutes=4))
        _msg(db, "user", "Ironed my clothes", now - timedelta(minutes=3), session_id="voice:abc")
        db.close()

    def test_grouped_by_day_newest_first_text_only(self):
        self._seed()
        d = client.get("/conversation/history").json()
        assert [day["label"] for day in d["days"]] == ["Today", "Yesterday"]
        today, yday = d["days"]
        assert [m["text"] for m in today["messages"]] == ["What is due today?", "Two things are due.", "Ironed my clothes"]
        assert [m["role"] for m in yday["messages"]] == ["user", "assistant"]   # no tool rows, no empty rows
        assert yday["messages"][1]["changes"] == 1
        assert today["messages"][2]["voice"] is True and today["messages"][0]["voice"] is False
        assert d["count"] == 5 and d["has_more"] is False

    def test_search_is_case_insensitive(self):
        self._seed()
        d = client.get("/conversation/history", params={"q": "PROBABILITY"}).json()
        texts = [m["text"] for day in d["days"] for m in day["messages"]]
        assert texts == ["Revised S1 probability", "Marked S1 probability as revised."]

    def test_paging_back_with_before_id(self):
        self._seed()
        first = client.get("/conversation/history", params={"limit": 2}).json()
        assert first["has_more"] is True and first["count"] == 2
        rest = client.get("/conversation/history", params={"limit": 10, "before_id": first["next_before_id"]}).json()
        seen = [m["id"] for d in first["days"] + rest["days"] for m in d["messages"]]
        assert len(seen) == len(set(seen)) == 5

    def test_days_follow_the_viewers_timezone(self):
        db = _db()
        now = datetime(2026, 11, 7, 23, 30)                     # 23:30 UTC = 01:30 next day in Lusaka
        _msg(db, "user", "late night thought", now)
        d = conversation.history(db, tz_offset_minutes=-120, now=now)
        db.close()
        assert d["days"][0]["date"] == "2026-11-08" and d["days"][0]["messages"][0]["time"] == "01:30"

    def test_history_requires_auth_when_password_set(self, monkeypatch):
        monkeypatch.setenv("FOUNDER_OS_PASSWORD", "pw")
        assert client.get("/conversation/history").status_code == 401

    def test_show_view_history(self):
        db = _db()
        result, summary = ot.run_tool("show_view", {"view": "conversations", "params": {"q": "probability", "x": "y"}}, db)
        db.close()
        assert result["ui_directive"] == {"view": "history", "params": {"q": "probability"}}
        assert summary == "showing past conversations matching 'probability'"
        schema = next(t for t in ot.TOOL_SCHEMAS if t["function"]["name"] == "show_view")
        assert "history" in schema["function"]["parameters"]["properties"]["view"]["enum"]
