"""
Phase 6B — agents acceptance tests.

  1. a fake agent runs through a tick: run row, summary, batch, agents row
  2. a write outside tables_write is undone and the run is a violation
  3. schedule semantics: at_time, interval, min gap, weekdays
  4. events: delivered to the subscribed agent, consumed, dropped after 24 hours
  5. model calls are attributed to the run and capped
  6. the tick budget defers what does not fit
  7. /jobs/tick: 503 unset, 401 wrong token, 200 with the token and no session
  8. an agent toggle is logged and undoable
  9. crm_housekeeper generates due payments, flips overdue, emits payment.overdue once a day
 10. market_intel runs exactly one due objective per tick
     plus the agents API (list, patch + undo, run now, activity log, undo a run), the tools,
     the workflow file and the in-process tick
 11. the startup chain still passes; agents rows appear only on first use

Fake agents are registered inside the tests (the registry is patched). No network.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import agents
import change_log
import llm
import models
from agents import runner, schedule
from agents.base import AgentResult, AgentSpec, Permissions, Schedule
from tests.conftest import TestSessionLocal, client


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("JOBS_TOKEN", raising=False)
    monkeypatch.delenv("JOBS_TICK_BUDGET", raising=False)
    monkeypatch.setenv("FOUNDER_OS_TZ", "Africa/Lusaka")
    llm.reset_client()
    llm.clear_run_context()
    # Agent runs open their own sessions: point them at the test database, and start each
    # test with an empty registry.
    with patch.object(runner, "SessionLocal", TestSessionLocal), patch.dict(agents.REGISTRY, {}, clear=True):
        yield
    llm.reset_client()
    llm.clear_run_context()


def _db():
    return TestSessionLocal()


def _spec(key, run, *, schedule_=None, events=None, tables=None, tools=None, max_llm_calls=20,
          may_emit=None, enabled=True):
    spec = AgentSpec(
        key=key, name=key.replace("_", " ").title(), description="test agent",
        schedule=schedule_ or Schedule(interval_minutes=60), events=events or [],
        permissions=Permissions(tools=tools or [], tables_write=tables or [], may_emit=may_emit or [],
                                max_llm_calls=max_llm_calls),
        run=run, enabled_by_default=enabled, phase="test")
    agents.register(spec)
    return spec


def _add_todo(ctx, text="made by an agent"):
    todo = models.Todo(text=text, priority=5, done=False, category="personal", source="agent")
    ctx.db.add(todo)
    change_log.record_create(ctx.db, todo, f"Added task: {text}")
    ctx.db.commit()
    return todo


def _local(year, month, day, hour, minute=0):
    """A naive UTC datetime for a Lusaka wall-clock time (UTC+2)."""
    return datetime(year, month, day, hour, minute) - timedelta(hours=2)


# ── 1. a run through the tick ─────────────────────────────────

def test_fake_agent_runs_through_a_tick():
    seen = {}

    def run(ctx):
        seen["today"], seen["trigger"], seen["config"] = ctx.today, ctx.run.trigger, ctx.config
        _add_todo(ctx)
        ctx.log("did one thing")
        return AgentResult(summary="added one task", result={"added": 1})

    _spec("writer", run, tables=["todos"])
    db = _db()
    assert db.query(models.Agent).count() == 0                     # nothing until the first tick

    out = runner.tick("manual")
    assert [r["key"] for r in out["ran"]] == ["writer"] and out["errors"] == 0 and out["tick_id"]
    row = db.query(models.AgentRun).one()
    assert (row.agent_key, row.status, row.summary, row.trigger) == ("writer", "completed", "added one task", "manual")
    assert row.batch_id and row.changes_count == 1 and row.finished_at is not None
    assert json.loads(row.result_json) == {"added": 1} and row.log_text == "did one thing\n"
    assert row.llm_calls == 0 and row.cost_usd == 0 and row.over_time is False

    agent = db.query(models.Agent).one()
    assert agent.key == "writer" and agent.enabled is True and agent.last_run_at is not None
    assert (agent.last_status, agent.last_summary, agent.runs_count) == ("completed", "added one task", 1)
    change = db.query(models.ChangeLog).one()
    assert (change.source, change.batch_id, change.table_name) == ("agent:writer", row.batch_id, "todos")
    tick_row = db.query(models.JobTick).one()
    assert (tick_row.trigger, tick_row.agents_run, tick_row.errors) == ("manual", 1, 0)
    assert tick_row.finished_at is not None and seen["trigger"] == "manual"
    db.close()


def test_a_failing_agent_is_recorded_and_the_tick_carries_on():
    def boom(ctx):
        _add_todo(ctx, "committed before the crash")
        raise ValueError("no such thing")

    _spec("crasher", boom, tables=["todos"])
    _spec("steady", lambda ctx: AgentResult(summary="fine"))
    out = runner.tick("manual")
    assert [(r["key"], r["status"]) for r in out["ran"]] == [("crasher", "failed"), ("steady", "completed")]
    assert out["errors"] == 1
    db = _db()
    failed = db.query(models.AgentRun).filter(models.AgentRun.agent_key == "crasher").one()
    assert failed.error == "ValueError: no such thing" and failed.changes_count == 1
    assert db.query(models.Todo).count() == 1                      # partial work stays, logged and undoable
    assert client.post(f"/changes/batch/{failed.batch_id}/undo").status_code == 200
    db.expire_all()
    assert db.query(models.Todo).count() == 0
    db.close()


def test_skipped_result_and_disabled_agents():
    _spec("idle", lambda ctx: AgentResult(summary="nothing to do", status="skipped"))
    _spec("off", lambda ctx: AgentResult(summary="should not run"), enabled=False)
    out = runner.tick("manual")
    assert [(r["key"], r["status"]) for r in out["ran"]] == [("idle", "skipped")]
    assert {"key": "off", "reason": "disabled"} in out["skipped"]
    manual = runner.run_agent(agents.REGISTRY["off"], "manual")     # Run now ignores the toggle
    assert manual["status"] == "completed" and manual["trigger"] == "manual"


# ── 2. the permission audit ───────────────────────────────────

def test_a_write_outside_the_allow_list_is_undone():
    def run(ctx):
        _add_todo(ctx)
        return AgentResult(summary="wrote a todo")

    _spec("overreach", run, tables=["notes"])
    out = runner.tick("manual")
    assert out["ran"][0]["status"] == "violation" and out["errors"] == 1
    db = _db()
    row = db.query(models.AgentRun).one()
    assert row.status == "violation" and row.error == "wrote to todos"
    assert db.query(models.Todo).count() == 0                      # the batch was undone
    assert db.query(models.ChangeLog).one().undone_at is not None
    assert db.query(models.Agent).one().last_status == "violation"
    db.close()


def test_run_tool_is_allow_listed():
    captured = {}

    def run(ctx):
        captured["denied"] = ctx.run_tool("add_todos", {"items": ["sneaky"]})
        captured["allowed"] = ctx.run_tool("get_deadlines", {"days": 7})
        captured["emit"] = ctx.emit("lead.replied", {"lead_id": 1})
        captured["notify"] = ctx.notify(kind="alert", title="t", body="b", dedupe_key="k")
        return AgentResult(summary="read only")

    _spec("reader", run, tools=["get_deadlines"])
    runner.tick("manual")
    assert "not allowed" in captured["denied"][0]["error"]
    assert "error" not in captured["allowed"][0]
    assert captured["emit"] is False and "may not notify" in captured["notify"]["error"]
    db = _db()
    assert db.query(models.Todo).count() == 0 and db.query(models.AgentEvent).count() == 0
    assert "not allowed" in db.query(models.AgentRun).one().log_text
    db.close()


# ── 3. schedules ──────────────────────────────────────────────

def test_at_time_runs_once_a_day_after_the_time():
    _spec("morning", lambda ctx: AgentResult(summary="ran"), schedule_=Schedule(at_time="06:00"))
    day = (2026, 11, 11)                                           # a Wednesday
    assert runner.tick("manual", now=_local(*day, 5, 59))["ran"] == []
    assert len(runner.tick("manual", now=_local(*day, 6, 0))["ran"]) == 1
    assert runner.tick("manual", now=_local(*day, 9, 0))["ran"] == []        # already ran today
    assert runner.tick("manual", now=_local(2026, 11, 12, 5, 0))["ran"] == []
    assert len(runner.tick("manual", now=_local(2026, 11, 12, 6, 30))["ran"]) == 1


def test_interval_and_min_gap():
    _spec("hourly", lambda ctx: AgentResult(summary="ran"), schedule_=Schedule(interval_minutes=60))
    _spec("eager", lambda ctx: AgentResult(summary="ran"), schedule_=Schedule(interval_minutes=1, min_gap_minutes=10))
    t0 = _local(2026, 11, 11, 8, 0)
    assert len(runner.tick("manual", now=t0)["ran"]) == 2                    # never run: due
    second = runner.tick("manual", now=t0 + timedelta(minutes=2))            # a doubled tick
    assert second["ran"] == [] and {"key": "eager", "reason": "ran moments ago"} in second["skipped"]
    at_59 = runner.tick("manual", now=t0 + timedelta(minutes=59))
    assert [r["key"] for r in at_59["ran"]] == ["eager"]                     # hourly is not due yet
    at_61 = runner.tick("manual", now=t0 + timedelta(minutes=61))
    assert [r["key"] for r in at_61["ran"]] == ["hourly"]                    # eager ran two minutes ago


def test_weekdays_and_overrides():
    _spec("sunday", lambda ctx: AgentResult(summary="ran"), schedule_=Schedule(at_time="07:00", weekdays=[6]))
    assert runner.tick("manual", now=_local(2026, 11, 14, 8, 0))["ran"] == []        # Saturday
    assert len(runner.tick("manual", now=_local(2026, 11, 15, 8, 0))["ran"]) == 1    # Sunday

    spec = _spec("daily", lambda ctx: AgentResult(summary="ran"), schedule_=Schedule(at_time="06:00"))
    db = _db()
    runner.update_agent(db, spec, at_time="21:30", weekdays=[2])
    db.commit()
    row = db.query(models.Agent).filter(models.Agent.key == "daily").one()
    eff = schedule.effective(spec, row)
    assert (eff.at_time, eff.weekdays) == ("21:30", [2]) and schedule.describe(eff) == "Wed at 21:30"
    assert schedule.due(spec, row, schedule.to_local(_local(2026, 11, 11, 21, 0))) is False
    assert schedule.due(spec, row, schedule.to_local(_local(2026, 11, 11, 21, 30))) is True
    runner.update_agent(db, spec, at_time="", weekdays=[], interval_minutes=45)
    db.commit()
    eff = schedule.effective(spec, row)
    assert (eff.at_time, eff.interval_minutes, eff.weekdays) == (None, 45, None)
    assert schedule.describe(eff) == "every 45 min"
    with pytest.raises(ValueError):
        runner.update_agent(db, spec, at_time="25:99")
    db.close()
    assert schedule.describe(Schedule()) == "on events only"
    assert schedule.to_local(datetime(2026, 11, 11, 4, 0)).hour == 6                 # Lusaka is UTC+2


# ── 4. events ─────────────────────────────────────────────────

def test_events_trigger_the_subscribed_agent_and_are_consumed():
    got = []

    def run(ctx):
        got.append([(e.kind, json.loads(e.payload_json)) for e in ctx.events])
        return AgentResult(summary=f"saw {len(ctx.events)} event(s)")

    _spec("listener", run, schedule_=Schedule(), events=["lead.replied"])
    _spec("bystander", lambda ctx: AgentResult(summary="ran"), schedule_=Schedule(), events=["payment.overdue"])
    assert runner.tick("manual")["ran"] == []                                 # event-only, nothing emitted

    db = _db()
    agents.emit(db, "lead.replied", {"lead_id": 7}, source="test")
    old = agents.emit(db, "lead.replied", {"lead_id": 8})
    old.created_at = datetime.utcnow() - timedelta(hours=25)
    unheard = agents.emit(db, "brand.sync_failed", {"account_id": 1})
    db.commit()
    unheard_id = unheard.id

    out = runner.tick("manual")
    assert [r["key"] for r in out["ran"]] == ["listener"]
    assert got == [[("lead.replied", {"lead_id": 7})]]                        # the 25-hour-old one is not delivered
    assert out["events_consumed"] == 1 and out["events_dropped"] == 1
    db.expire_all()
    events = {e.id: e for e in db.query(models.AgentEvent).all()}
    fresh = next(e for e in events.values() if json.loads(e.payload_json) == {"lead_id": 7})
    dropped = next(e for e in events.values() if json.loads(e.payload_json) == {"lead_id": 8})
    assert fresh.consumed_at is not None and not fresh.dropped and fresh.source == "test"
    assert dropped.consumed_at is not None and dropped.dropped is True
    assert events[unheard_id].consumed_at is None                             # nobody subscribed: waits
    assert db.query(models.AgentRun).one().trigger == "event"

    assert runner.tick("manual")["ran"] == []                                 # consumed: not delivered twice
    db.close()


def test_an_agent_may_only_emit_what_it_declares():
    def run(ctx):
        assert ctx.emit("payment.overdue", {"payment_id": 3}) is True
        assert ctx.emit("lead.replied", {}) is False
        return AgentResult(summary="emitted")

    _spec("emitter", run, may_emit=["payment.overdue"])
    runner.tick("manual")
    db = _db()
    event = db.query(models.AgentEvent).one()
    assert (event.kind, event.source, json.loads(event.payload_json)) == ("payment.overdue", "agent:emitter", {"payment_id": 3})
    db.close()


# ── 5. cost attribution and the call cap ──────────────────────

def _fake_client():
    def create(*, model, messages, **kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=200,
                                  prompt_tokens_details=SimpleNamespace(cached_tokens=0)))
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def test_model_calls_are_attributed_to_the_run(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")

    def run(ctx):
        text = llm.chat_text([{"role": "user", "content": "hi"}], db=ctx.db, purpose="test_agent")
        return AgentResult(summary=f"model said {text}")

    _spec("thinker", run, max_llm_calls=5)
    with patch("llm.get_client", return_value=_fake_client()):
        runner.tick("manual")
    db = _db()
    run_row = db.query(models.AgentRun).one()
    usage = db.query(models.LLMUsage).one()
    assert usage.agent_run_id == run_row.id and usage.purpose == "test_agent"
    assert run_row.llm_calls == 1 and run_row.cost_usd == usage.estimated_cost_usd > 0
    assert db.query(models.Agent).one().cost_usd_total == run_row.cost_usd
    assert llm.current_run_id() is None                                       # cleared after the run
    db.close()


def test_the_call_cap_fails_the_run(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")

    def run(ctx):
        llm.chat_text([{"role": "user", "content": "one"}], db=ctx.db, purpose="test_agent")
        llm.chat_text([{"role": "user", "content": "two"}], db=ctx.db, purpose="test_agent")
        return AgentResult(summary="never reached")

    _spec("greedy", run, max_llm_calls=1)
    with patch("llm.get_client", return_value=_fake_client()):
        out = runner.tick("manual")
    assert out["ran"][0]["status"] == "failed"
    db = _db()
    run_row = db.query(models.AgentRun).one()
    assert run_row.error.startswith("BudgetExceeded") and run_row.llm_calls == 1
    assert db.query(models.LLMUsage).count() == 1
    db.close()

    def none_allowed(ctx):
        llm.chat_text([{"role": "user", "content": "x"}], db=ctx.db)
        return AgentResult(summary="never reached")

    _spec("deterministic", none_allowed, max_llm_calls=0)
    with patch("llm.get_client", return_value=_fake_client()):
        result = runner.run_agent(agents.REGISTRY["deterministic"], "manual")
    assert result["status"] == "failed" and "BudgetExceeded" in result["error"]


def test_usage_outside_a_run_is_not_attributed(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    db = _db()
    with patch("llm.get_client", return_value=_fake_client()):
        llm.chat_text([{"role": "user", "content": "hi"}], db=db, purpose="assistant")
        llm.log_usage(db, model="gpt-5.4-mini", purpose="explicit", agent_run_id=42)
    rows = {u.purpose: u.agent_run_id for u in db.query(models.LLMUsage).all()}
    assert rows == {"assistant": None, "explicit": 42}
    db.close()


# ── 6. the tick budget ────────────────────────────────────────

def test_tick_budget_defers_what_does_not_fit(monkeypatch):
    monkeypatch.setenv("JOBS_TICK_BUDGET", "0")
    _spec("first", lambda ctx: AgentResult(summary="ran"))
    _spec("second", lambda ctx: AgentResult(summary="ran"))
    _spec("briefing_courier", lambda ctx: AgentResult(summary="ran"))
    _spec("third", lambda ctx: AgentResult(summary="ran"))
    out = runner.tick("manual")
    assert [r["key"] for r in out["ran"]] == ["first"]
    assert out["deferred"] == ["second", "third", "briefing_courier"]          # couriers go last
    db = _db()
    assert db.query(models.JobTick).one().agents_skipped == 3
    db.close()
    monkeypatch.delenv("JOBS_TICK_BUDGET")
    later = runner.tick("manual", now=datetime.utcnow() + timedelta(minutes=30))
    assert [r["key"] for r in later["ran"]] == ["second", "third", "briefing_courier"]   # they ran next tick


# ── 7. /jobs/tick ─────────────────────────────────────────────

def test_jobs_tick_needs_its_token_and_no_session(monkeypatch):
    import auth
    _spec("writer", lambda ctx: AgentResult(summary="ran"))
    with patch.object(agents, "load_all", lambda: None):
        assert client.post("/jobs/tick").status_code == 503                    # JOBS_TOKEN unset
        monkeypatch.setenv("JOBS_TOKEN", "a" * 40)
        assert client.post("/jobs/tick").status_code == 401                    # no header
        assert client.post("/jobs/tick", headers={"X-Jobs-Token": "wrong"}).status_code == 401

        monkeypatch.setenv("FOUNDER_OS_PASSWORD", "a-password")               # the login gate is on
        assert client.get("/todos").status_code == 401                         # and it is really enforced
        ok = client.post("/jobs/tick", headers={"X-Jobs-Token": "a" * 40})     # no bearer token at all
        assert ok.status_code == 200
        body = ok.json()
        assert body["trigger"] == "github" and [r["key"] for r in body["ran"]] == ["writer"]
        assert client.get("/jobs/status").status_code == 401                   # the status page stays behind login
        monkeypatch.delenv("FOUNDER_OS_PASSWORD")

        status = client.get("/jobs/status").json()
        assert status["token_configured"] is True and status["in_process_scheduler"] is True
        assert status["last_tick"]["trigger"] == "github" and status["ticks_24h"] == 1
        assert status["agents_due_now"] == []                                  # it just ran
    assert "/jobs/tick" in auth.EXEMPT_EXACT
    db = _db()
    assert db.query(models.AgentRun).one().trigger == "schedule"
    db.close()


# ── 8. toggles are logged ─────────────────────────────────────

def test_agent_toggle_is_logged_and_undoable():
    spec = _spec("writer", lambda ctx: AgentResult(summary="ran"))
    db = _db()
    with change_log.batch_scope(db, "ui"):
        out = runner.update_agent(db, spec, enabled=False)
        db.commit()
    change = out["change"]
    assert change["table"] == "agents" and change["action"] == "update"
    assert change["summary"] == "Agent Writer: off, every 60 min"
    assert db.query(models.Agent).one().enabled is False
    assert runner.tick("manual")["ran"] == []                                  # disabled: skipped

    assert client.post(f"/changes/{change['id']}/undo").status_code == 200
    db.expire_all()
    assert db.query(models.Agent).one().enabled is True
    assert len(runner.tick("manual")["ran"]) == 1
    with change_log.batch_scope(db, "ui"):
        cfg = runner.update_agent(db, spec, config={"limit": 5})
        db.commit()
    assert json.loads(cfg["row"].config_json) == {"limit": 5}
    db.close()


# ── 11. migration registered; rows are lazy ───────────────────

def test_m020_is_registered_and_agent_rows_are_lazy():
    source = (Path(__file__).parent.parent / "main.py").read_text(encoding="utf-8")
    assert "run_m020(db)" in source and source.index("run_m020(db)") < source.index("_validate_schema(engine)   #")
    from migrations import m020_agents
    assert set(m020_agents._TABLES) == {"agents", "agent_runs", "agent_events", "job_ticks"}
    _spec("writer", lambda ctx: AgentResult(summary="ran"))
    db = _db()
    assert db.query(models.Agent).count() == 0
    assert runner.due_now(db) == ["writer"]                                    # listing creates the row
    assert db.query(models.Agent).count() == 1
    db.close()


def test_runner_and_tick_never_raise():
    def bad_result(ctx):
        return "just a string"

    _spec("loose", bad_result)
    out = runner.tick("manual")
    assert out["ran"][0]["status"] == "completed" and out["ran"][0]["summary"] == "just a string"
    with patch.object(runner, "SessionLocal", side_effect=RuntimeError("database is gone")):
        assert runner.run_agent(agents.REGISTRY["loose"], "manual")["status"] == "failed"
        broken = runner.tick("manual")
        assert broken["errors"] == 1 and "database is gone" in broken["error"] and broken["ran"] == []


# ════════════════════════════════════════════════════════════════
# 6B.2 — the shipped agents, the agents API, the tools
# ════════════════════════════════════════════════════════════════

from datetime import date

import orchestrator_tools as ot

TODAY = date.today()


def _use(*modules):
    """Put shipped agents back in the (emptied) registry for one test."""
    import importlib
    for name in modules:
        agents.register(importlib.import_module(f"agents.{name}").SPEC)


def test_shipped_agents_have_the_documented_defaults():
    _use("crm_housekeeper", "lead_qualifier", "follow_up_sentinel", "portfolio_reviewer", "brand_sync", "market_intel")
    r = agents.REGISTRY
    assert list(r) == ["crm_housekeeper", "lead_qualifier", "follow_up_sentinel", "portfolio_reviewer",
                       "brand_sync", "market_intel"]
    assert r["crm_housekeeper"].schedule.interval_minutes == 60 and r["crm_housekeeper"].permissions.max_llm_calls == 0
    assert r["crm_housekeeper"].permissions.tables_write == ["payments", "recurring_revenue", "clients", "projects"]
    assert r["lead_qualifier"].schedule.interval_minutes == 180 and r["lead_qualifier"].permissions.max_llm_calls == 20
    assert r["lead_qualifier"].permissions.tables_write == ["leads"]
    assert r["follow_up_sentinel"].schedule.at_time == "05:30" and r["follow_up_sentinel"].events == ["lead.replied"]
    assert r["follow_up_sentinel"].permissions.tables_write == ["todos", "leads"]
    assert r["portfolio_reviewer"].schedule.at_time == "05:40"
    assert r["portfolio_reviewer"].permissions.tables_write == ["projects", "suggestions"]
    assert r["brand_sync"].schedule.at_time == "04:00" and r["brand_sync"].permissions.max_llm_calls == 0
    assert r["market_intel"].schedule.interval_minutes == 60 and r["market_intel"].permissions.max_llm_calls == 12
    assert all(not s.permissions.may_notify for s in r.values())
    assert all(not s.permissions.tools for s in r.values())      # no delete tool, no confirm_action, nothing


# ── 9. crm_housekeeper ────────────────────────────────────────

def test_crm_housekeeper_generates_flips_and_emits_once_per_payment():
    _use("crm_housekeeper")
    db = _db()
    c = models.Client(name="Hope Clinic", status="active")
    db.add(c)
    db.commit()
    start = TODAY - timedelta(days=45)
    db.add(models.RecurringRevenue(client_id=c.id, title="Hosting", amount=600, currency="ZMW",
                                   interval="monthly", day_of_month=start.day, next_due=start, active=True))
    db.add(models.Payment(client_id=c.id, amount=2000, currency="ZMW", status="due", title="Deposit",
                          due_date=TODAY - timedelta(days=3)))
    db.add(models.Payment(client_id=c.id, amount=500, currency="ZMW", status="due", title="Later",
                          due_date=TODAY + timedelta(days=9)))
    db.commit()

    out = runner.run_agent(agents.REGISTRY["crm_housekeeper"], "manual")
    assert out["status"] == "completed", out
    assert out["result"]["generated"] == 2 and out["result"]["overdue"] == 3      # the two instalments are late too
    assert out["result"]["events_emitted"] == 3 and out["llm_calls"] == 0
    db.expire_all()
    statuses = sorted(p.status for p in db.query(models.Payment).all())
    assert statuses == ["due", "overdue", "overdue", "overdue"]
    events = db.query(models.AgentEvent).filter(models.AgentEvent.kind == "payment.overdue").all()
    overdue_ids = {p.id for p in db.query(models.Payment).filter(models.Payment.status == "overdue").all()}
    assert {json.loads(e.payload_json)["payment_id"] for e in events} == overdue_ids
    assert db.query(models.Client).one().health == "red"                            # an instalment 45 days late
    tables = {ch.table_name for ch in db.query(models.ChangeLog).all()}
    assert tables <= {"payments", "recurring_revenue"}                              # inside its allow-list

    again = runner.run_agent(agents.REGISTRY["crm_housekeeper"], "manual")
    assert again["status"] == "skipped" and again["result"]["events_emitted"] == 0  # once per payment per day
    assert db.query(models.AgentEvent).count() == 3

    for e in db.query(models.AgentEvent).all():                                     # a day later: reminded again
        e.created_at = datetime.utcnow() - timedelta(hours=25)
    db.commit()
    third = runner.run_agent(agents.REGISTRY["crm_housekeeper"], "manual")
    assert third["result"]["events_emitted"] == 3
    db.close()


# ── 10. market_intel: one objective per tick ──────────────────

def test_market_intel_runs_exactly_one_due_objective_per_tick(monkeypatch):
    _use("market_intel")
    db = _db()
    older = models.ResearchObjective(objective="painful admin problems in Lusaka pharmacies", cadence_days=7,
                                     enabled=True, last_run_at=datetime.utcnow() - timedelta(days=30))
    newer = models.ResearchObjective(objective="what bakeries in Kitwe struggle with every week", cadence_days=7,
                                     enabled=True, last_run_at=datetime.utcnow() - timedelta(days=10))
    off = models.ResearchObjective(objective="something that is switched off for now", cadence_days=7, enabled=False)
    db.add_all([older, newer, off])
    db.commit()
    older_id, newer_id = older.id, newer.id

    idle = runner.run_agent(agents.REGISTRY["market_intel"], "manual")
    assert idle["status"] == "skipped" and idle["summary"] == "no AI key configured"

    monkeypatch.setenv("OPENAI_API_KEY", "test")
    calls = []

    def fake_run(session, objective, max_opportunities=3):
        calls.append(objective)
        project = models.ResearchProject(title="t", objective=objective, status="completed")
        session.add(project)
        session.commit()
        return {"project_id": project.id, "title": "t", "opportunities": [{"id": 1}], "repeated": [],
                "rejected_count": 0, "lessons_updated": 0, "cost_usd": 0.02, "llm_calls": 6}

    with patch("market_intel.agent.run_market_research", side_effect=fake_run):
        out = runner.tick("manual", now=datetime.utcnow() + timedelta(minutes=61))   # an hour after the idle run
        assert [r["key"] for r in out["ran"]] == ["market_intel"]
        assert calls == ["painful admin problems in Lusaka pharmacies"]              # the longest-waiting one
        db.expire_all()
        run_row = db.query(models.AgentRun).order_by(models.AgentRun.id.desc()).first()
        result = json.loads(run_row.result_json)
        assert result["objective_id"] == older_id and result["deferred"] == [newer_id]
        assert "1 objective(s) deferred" in run_row.summary
        assert db.query(models.ResearchObjective).filter(models.ResearchObjective.id == older_id).one().last_project_id
        assert db.query(models.AgentEvent).filter(models.AgentEvent.kind == "research.completed").count() == 1

        runner.tick("manual", now=datetime.utcnow() + timedelta(minutes=125))        # the next tick takes the other
        assert calls[1] == "what bakeries in Kitwe struggle with every week" and len(calls) == 2
        final = runner.run_agent(agents.REGISTRY["market_intel"], "manual")
        assert final["status"] == "skipped" and final["summary"] == "no research objective is due"
    db.close()


# ── the other shipped agents ──────────────────────────────────

def test_follow_up_sentinel_adds_one_reminder_for_a_quiet_lead():
    _use("follow_up_sentinel")
    db = _db()
    quiet = models.Lead(name="Mercy Banda", status="contacted", last_contact_at=datetime.utcnow() - timedelta(days=9))
    recent = models.Lead(name="Ruth Zulu", status="contacted", last_contact_at=datetime.utcnow() - timedelta(days=3))
    replied = models.Lead(name="Peter Lungu", status="replied", last_contact_at=datetime.utcnow() - timedelta(days=20))
    db.add_all([quiet, recent, replied])
    db.commit()

    out = runner.run_agent(agents.REGISTRY["follow_up_sentinel"], "manual")
    assert out["status"] == "completed" and out["result"] == {"quiet": 1, "created": 1}
    todo = db.query(models.Todo).one()
    assert todo.lead_id == quiet.id and todo.due_date is not None and todo.category == "outreach"
    assert "no reply in 9 days" in todo.text
    assert db.query(models.AgentEvent).filter(models.AgentEvent.kind == "lead.stale").count() == 1
    again = runner.run_agent(agents.REGISTRY["follow_up_sentinel"], "manual")
    assert again["status"] == "skipped" and db.query(models.Todo).count() == 1       # never a second one

    agents.emit(db, "lead.replied", {"lead_id": quiet.id})
    db.commit()
    woken = runner.tick("manual", now=datetime.utcnow() + timedelta(minutes=30))
    assert [r["key"] for r in woken["ran"]] == ["follow_up_sentinel"]
    log = db.query(models.AgentRun).order_by(models.AgentRun.id.desc()).first().log_text
    assert "replied; nothing to chase" in log
    db.close()


def test_lead_qualifier_scores_reachable_new_leads_once(monkeypatch):
    _use("lead_qualifier")
    db = _db()
    db.add_all([
        models.Lead(name="Mercy Banda", status="new", email="mercy@example.com", company="Sunrise Pharmacy",
                    segment="lusaka pharmacies", location="Lusaka"),
        models.Lead(name="No Channel", status="new"),
        models.Lead(name="Already Contacted", status="contacted", email="x@example.com"),
    ])
    db.commit()
    assert runner.run_agent(agents.REGISTRY["lead_qualifier"], "manual")["summary"] == "no AI key configured"

    monkeypatch.setenv("OPENAI_API_KEY", "test")
    answer = json.dumps({"fit": 30, "reasons": ["fits"], "disqualifiers": [], "suggested_angle": "stock alerts"})
    with patch("llm.chat_text", return_value=answer) as chat:
        out = runner.run_agent(agents.REGISTRY["lead_qualifier"], "manual")
        assert out["status"] == "completed" and out["result"]["scored"] == 1 and out["result"]["qualified"] == 1
        assert chat.call_count == 1
        again = runner.run_agent(agents.REGISTRY["lead_qualifier"], "manual")
        assert again["status"] == "skipped" and chat.call_count == 1                 # scored once, not every run
    db.expire_all()
    assert db.query(models.Lead).filter(models.Lead.name == "Mercy Banda").one().status == "qualified"
    db.close()


def test_portfolio_reviewer_suggests_and_never_pauses():
    _use("portfolio_reviewer", "brand_sync")
    db = _db()
    assert runner.run_agent(agents.REGISTRY["portfolio_reviewer"], "manual")["status"] == "skipped"
    stale = models.Project(title="Newsletter", status="active", priority=4, kind="internal",
                           last_activity_at=datetime.utcnow() - timedelta(days=20))
    db.add(stale)
    db.add(models.Project(title="Founder OS Phase 5", status="active", priority=1, kind="internal",
                          last_activity_at=datetime.utcnow()))
    db.commit()
    out = runner.run_agent(agents.REGISTRY["portfolio_reviewer"], "manual")
    assert out["status"] == "completed" and out["result"]["pause_suggestions_new"] == 1
    assert "focus: Founder OS Phase 5" in out["summary"]
    db.expire_all()
    assert db.query(models.Project).filter(models.Project.title == "Newsletter").one().status == "active"
    assert db.query(models.Suggestion).filter(models.Suggestion.kind == "project_pause").count() == 1
    second = runner.run_agent(agents.REGISTRY["portfolio_reviewer"], "manual")
    assert second["result"]["pause_suggestions_new"] == 0

    brand = runner.run_agent(agents.REGISTRY["brand_sync"], "manual")               # its module comes later
    assert brand["status"] == "skipped"
    db.close()


# ── the agents API ────────────────────────────────────────────

def test_agents_api_lists_toggles_runs_and_undoes():
    def run(ctx):
        _add_todo(ctx, "from the agent")
        return AgentResult(summary="added one task")

    _spec("writer", run, tables=["todos"], schedule_=Schedule(at_time="06:00"))
    _spec("briefing_courier", lambda ctx: AgentResult(summary="sent"))

    listed = client.get("/agents").json()
    assert [a["key"] for a in listed] == ["writer", "briefing_courier"]
    a = listed[0]
    assert a["enabled"] is True and a["last_run"] is None and a["runs_count"] == 0
    assert a["schedule"] == {"interval_minutes": None, "at_time": "06:00", "weekdays": None,
                             "effective": "daily at 06:00", "overridden": False}
    assert a["permissions"]["tables_write"] == ["todos"] and a["phase"] == "test"
    assert a["next_due_estimate"] is not None

    off = client.patch("/agents/writer", json={"enabled": False, "at_time": "07:15", "weekdays": [6]})
    assert off.status_code == 200
    body = off.json()
    assert body["enabled"] is False and body["schedule"]["effective"] == "Sun at 07:15"
    assert body["schedule"]["overridden"] is True and body["next_due_estimate"] is None
    assert body["change"]["table"] == "agents"
    assert client.post(f"/changes/{body['change']['id']}/undo").status_code == 200
    restored = client.get("/agents/writer").json()
    assert restored["enabled"] is True and restored["schedule"]["effective"] == "daily at 06:00"
    assert client.patch("/agents/writer", json={"at_time": "nope"}).status_code == 400
    assert client.patch("/agents/nobody", json={"enabled": False}).status_code == 404

    ran = client.post("/agents/writer/run")
    assert ran.status_code == 200
    run_row = ran.json()
    assert (run_row["status"], run_row["trigger"], run_row["summary"]) == ("completed", "manual", "added one task")
    assert run_row["changes_count"] == 1 and run_row["duration_ms"] is not None

    detail = client.get("/agents/writer").json()
    assert detail["last_run"]["status"] == "completed" and detail["runs_count"] == 1 and detail["config"] == {}
    assert detail["runs"][0]["changes"][0]["summary"] == "Added task: from the agent"
    assert [r["id"] for r in client.get("/agents/writer/runs").json()] == [run_row["id"]]
    activity = client.get("/agents/runs").json()
    assert activity[0]["agent_name"] == "Writer" and activity[0]["changes"][0]["table"] == "todos"
    assert client.get("/agents/runs", params={"status": "failed"}).json() == []

    db = _db()
    assert db.query(models.Todo).count() == 1
    undone = client.post(f"/agents/runs/{run_row['id']}/undo")
    assert undone.status_code == 200
    db.expire_all()
    assert db.query(models.Todo).count() == 0
    db.close()
    client.post("/agents/briefing_courier/run")
    quiet = client.get("/agents/briefing_courier/runs").json()[0]
    assert client.post(f"/agents/runs/{quiet['id']}/undo").status_code == 409         # it changed nothing
    assert client.post("/agents/runs/99999/undo").status_code == 404
    assert client.post("/agents/nobody/run").status_code == 404


def test_agent_tools_report_run_and_reschedule():
    _spec("writer", lambda ctx: AgentResult(summary="did its thing"))
    db = _db()
    with patch.object(agents, "load_all", lambda: None):
        status, summary = ot.run_tool("agent_status", {}, db)
        assert summary == "1 agent(s), 1 enabled" and status["agents"][0]["schedule"] == "every 60 min"
        ran, summary = ot.run_tool("run_agent", {"key": "Writer"}, db)
        assert ran["status"] == "completed" and summary == "Writer: did its thing"
        one, summary = ot.run_tool("agent_status", {"key": "writer"}, db)
        assert one["last_status"] == "completed" and one["recent_runs"][0]["summary"] == "did its thing"

        moved, summary = ot.run_tool("set_agent_schedule", {"key": "writer", "at_time": "07:00", "weekdays": [6]}, db)
        assert summary == "Writer: on, Sun at 07:00" and moved["change"]["table"] == "agents"
        off, summary = ot.run_tool("set_agent_schedule", {"key": "writer", "enabled": False}, db)
        assert off["enabled"] is False and "off" in summary
        nothing, _ = ot.run_tool("set_agent_schedule", {"key": "writer"}, db)
        assert nothing["error"] == "nothing to change"
        bad, _ = ot.run_tool("set_agent_schedule", {"key": "writer", "at_time": "25:00"}, db)
        assert "error" in bad
        unknown, summary = ot.run_tool("run_agent", {"key": "ghost"}, db)
        assert "no agent called 'ghost'" in summary and unknown["agents"] == ["writer"]
    db.close()


def test_workflow_ships_with_the_schedule_switched_off():
    text = (Path(__file__).parent.parent.parent / ".github" / "workflows" / "jobs-tick.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch: {}" in text
    assert '  # schedule:\n  #   - cron: "*/15 * * * *"' in text                      # present, commented out
    active = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    assert not any("cron" in line or line.strip() == "schedule:" for line in active)
    assert "for i in 1 2 3" in text and "sleep 20" in text and "-m 120" in text
    assert "secrets.FOUNDER_OS_URL" in text and "X-Jobs-Token: ${{ secrets.JOBS_TOKEN }}" in text


def test_in_process_tick_is_guarded_by_the_env_flag(monkeypatch):
    import main
    assert main._jobs_in_process() is True
    monkeypatch.setenv("JOBS_IN_PROCESS", "0")
    assert main._jobs_in_process() is False
    _spec("writer", lambda ctx: AgentResult(summary="ran"))
    with patch.object(agents, "load_all", lambda: None):
        main._jobs_tick_job()                                                        # never raises
    db = _db()
    assert db.query(models.JobTick).one().trigger == "in_process"
    assert db.query(models.AgentRun).one().trigger == "in_process"
    db.close()


def test_agent_row_survives_two_requests_creating_it_at_once():
    """The Agents page asks for the registry and the scheduler status together; both may
    try to create the same agents row. The second must read the first one's row."""
    spec = _spec("writer", lambda ctx: AgentResult(summary="ran"))
    winner, loser = _db(), _db()
    real_find = runner._find_row
    seen = {"first": True}

    def racing_find(db, key):
        if db is loser and seen["first"]:            # the loser looked before the winner committed
            seen["first"] = False
            return None
        return real_find(db, key)

    first = runner.agent_row(winner, spec)
    with patch.object(runner, "_find_row", side_effect=racing_find):
        second = runner.agent_row(loser, spec)       # would raise IntegrityError without the retry
    assert second.id == first.id and loser.query(models.Agent).count() == 1
    assert loser.query(models.Todo).count() == 0     # the session is still usable afterwards
    winner.close()
    loser.close()

    listed = client.get("/agents").json()            # and the endpoints that share it still answer
    status = client.get("/jobs/status").json()
    assert [a["key"] for a in listed] == ["writer"] and status["agents_due_now"] == ["writer"]


# ── Review round: one run at a time (item 12) ─────────────────

def test_an_agent_that_is_running_is_not_started_again_by_a_tick_or_a_manual_run():
    """A second tick (the in-process timer and POST /jobs/tick can overlap) or a Run now tap
    arriving while an agent is running must not start it a second time. The overlap is made
    deterministic by letting the agent's own run trigger them."""
    calls, inner = [], {}
    now = _local(2026, 11, 11, 8, 0)

    def run(ctx):
        calls.append(ctx.run.id)
        if len(calls) == 1:
            inner["tick"] = runner.tick("in_process", now=now)
            inner["manual"] = runner.run_agent(agents.REGISTRY["slow"], "manual")
            inner["api"] = client.post("/agents/slow/run")
        return AgentResult(summary="worked")

    spec = _spec("slow", run)
    outer = runner.tick("schedule", now=now)

    assert len(calls) == 1                                           # it ran once, not three or four times
    assert [r["key"] for r in outer["ran"]] == ["slow"] and outer["errors"] == 0
    assert inner["tick"]["ran"] == []
    assert {"key": "slow", "reason": "already running"} in inner["tick"]["skipped"]
    assert inner["manual"]["status"] == "busy" and "already running" in inner["manual"]["summary"]
    assert inner["api"].status_code == 409
    db = _db()
    assert db.query(models.AgentRun).count() == 1
    row = db.query(models.Agent).filter_by(key="slow").one()
    assert row.claimed_at is None and row.runs_count == 1            # released when the run ended
    db.close()
    assert runner.run_agent(spec, "manual")["status"] == "completed"   # and free to run again


def test_a_tick_with_a_stale_plan_does_not_rerun_what_another_tick_just_ran():
    """Two ticks plan at the same moment. One finishes an agent before the other reaches it:
    the second must look again once it holds the claim, and skip."""
    ran = []
    now = _local(2026, 11, 11, 8, 0)

    def first(ctx):
        ran.append("first")
        if ran.count("first") == 1:
            runner.tick("in_process", now=now)      # a whole second tick runs while this one is mid-plan
        return AgentResult(summary="ok")

    def second(ctx):
        ran.append("second")
        return AgentResult(summary="ok")

    _spec("first", first)
    _spec("second", second)
    outer = runner.tick("schedule", now=now)
    assert ran.count("first") == 1 and ran.count("second") == 1
    assert [r["key"] for r in outer["ran"]] == ["first"]
    assert {"key": "second", "reason": "already ran"} in outer["skipped"]


def test_a_claim_left_by_a_dead_process_expires():
    spec = _spec("stuck", lambda ctx: AgentResult(summary="ok"))
    db = _db()
    row = runner.agent_row(db, spec)
    row.claimed_at = datetime.utcnow() - timedelta(minutes=5)       # a live run
    db.commit()
    assert runner.run_agent(spec, "manual")["status"] == "busy"
    row.claimed_at = datetime.utcnow() - timedelta(hours=2)         # the process died long ago
    db.commit()
    db.close()
    assert runner.run_agent(spec, "manual")["status"] == "completed"


def test_a_failed_run_releases_its_claim():
    def boom(ctx):
        raise RuntimeError("no")

    spec = _spec("fails", boom)
    assert runner.run_agent(spec, "manual")["status"] == "failed"
    db = _db()
    assert db.query(models.Agent).filter_by(key="fails").one().claimed_at is None
    db.close()
