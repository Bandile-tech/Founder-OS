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
