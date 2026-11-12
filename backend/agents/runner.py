"""
The agent runner and the tick.

  run_agent(spec, trigger, …)   one run: own session, change-log batch `agent:<key>`, cost
                                attribution, the permission audit. Never raises.
  tick(trigger, now=None)       run every due agent, sequentially, inside a time budget, and
                                record a job_ticks row. Never raises.

Permissions are enforced by the change log, not by trust: after a run every change in its
batch is checked, and a write to a table outside ``tables_write`` undoes the whole batch
and marks the run ``violation``.

One run of an agent at a time. The in-process timer, ``POST /jobs/tick`` and a Run now tap
can overlap (and on Render they can be different processes), so a run first claims its
agent in the database (``agents.claimed_at``, one atomic UPDATE) and releases it when it
ends. A run that cannot claim reports ``busy`` and does nothing. A tick that does claim
looks at the agent again before running it, because another tick may have just run it.
"""

import json
import os
import time
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError

import agents
import change_log
import llm
import models
from agents import schedule
from agents.base import AgentContext, AgentResult
from database import SessionLocal

DEFAULT_TICK_BUDGET_SECONDS = 80
EVENT_TTL_HOURS = 24
CLAIM_TTL_MINUTES = 30          # a claim older than this was left by a process that died


def tick_budget_seconds() -> float:
    try:
        return max(0.0, float(os.getenv("JOBS_TICK_BUDGET", DEFAULT_TICK_BUDGET_SECONDS)))
    except ValueError:
        return float(DEFAULT_TICK_BUDGET_SECONDS)


# ── Rows ─────────────────────────────────────────────────────

def _find_row(db, key: str):
    return db.query(models.Agent).filter(models.Agent.key == key).first()


def agent_row(db, spec) -> models.Agent:
    """The agents row for a spec, created on first sight (the registry is the source of
    truth; the table only holds toggles, overrides and last-run facts).

    Two requests can reach this at the same moment (the Agents page asks for the registry
    and the scheduler status together). ``key`` is unique, so the loser of that race gets an
    IntegrityError inside a savepoint, so only its own insert is undone (never the caller's
    uncommitted work), and it reads the row the winner wrote."""
    row = _find_row(db, spec.key)
    if row is None:
        row = models.Agent(key=spec.key, enabled=bool(spec.enabled_by_default), runs_count=0,
                           cost_usd_total=0.0, created_at=datetime.utcnow(), updated_at=datetime.utcnow())
        try:
            with db.begin_nested():             # a savepoint: losing the race undoes only this insert
                db.add(row)
                db.flush()
        except IntegrityError:
            row = _find_row(db, spec.key)
            if row is None:
                raise
        else:
            db.commit()
            db.refresh(row)
    return row


def _config(spec, row) -> dict:
    merged = dict(spec.default_config or {})
    try:
        stored = json.loads(row.config_json) if row.config_json else {}
    except (ValueError, TypeError):
        stored = {}
    if isinstance(stored, dict):
        merged.update(stored)
    return merged


def serialize_run(run: models.AgentRun) -> dict:
    try:
        result = json.loads(run.result_json) if run.result_json else {}
    except (ValueError, TypeError):
        result = {}
    duration = None
    if run.finished_at and run.started_at:
        duration = int((run.finished_at - run.started_at).total_seconds() * 1000)
    return {"id": run.id, "agent_key": run.agent_key, "trigger": run.trigger, "tick_id": run.tick_id,
            "started_at": run.started_at, "finished_at": run.finished_at, "duration_ms": duration,
            "status": run.status, "summary": run.summary, "result": result, "error": run.error,
            "log": run.log_text, "llm_calls": run.llm_calls or 0, "cost_usd": run.cost_usd or 0.0,
            "changes_count": run.changes_count or 0, "batch_id": run.batch_id,
            "over_time": bool(run.over_time)}


def _safe(db, text) -> str:
    """Exception text with every stored secret taken out, before it is kept or printed."""
    try:
        import integrations
        return integrations.redact(text, db)
    except Exception:  # noqa: BLE001 — never fail a run over its own error message
        return "error text withheld"


# ── The claim ────────────────────────────────────────────────

def _claim(db, key: str):
    """Take the agent for one run. Returns the claim's stamp, or None when another run holds
    it. One UPDATE decides: of two sessions that try at once, the database lets one through
    (the other's WHERE no longer matches once the first commits)."""
    stamp = datetime.utcnow()
    cutoff = stamp - timedelta(minutes=CLAIM_TTL_MINUTES)
    taken = db.query(models.Agent).filter(
        models.Agent.key == key,
        (models.Agent.claimed_at.is_(None)) | (models.Agent.claimed_at < cutoff),
    ).update({models.Agent.claimed_at: stamp}, synchronize_session=False)
    db.commit()
    return stamp if taken == 1 else None


def _release(db, key: str, stamp) -> None:
    """Give the agent back. Only our own claim: a stale one may have been taken over."""
    try:
        db.rollback()
        db.query(models.Agent).filter(models.Agent.key == key, models.Agent.claimed_at == stamp).update(
            {models.Agent.claimed_at: None}, synchronize_session=False)
        db.commit()
    except Exception as exc:  # noqa: BLE001 — the claim expires on its own
        print(f"[agents] could not release the claim on {key}: {type(exc).__name__}: {exc}")
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


def _not_run(spec, status: str, summary: str) -> dict:
    """What run_agent returns when it did not run (no agent_runs row is written)."""
    return {"agent_key": spec.key, "status": status, "summary": summary, "error": None, "changes_count": 0}


# ── One run ──────────────────────────────────────────────────

def _audit(db, batch_id: str, allowed: list[str]) -> tuple[int, str | None]:
    """(number of changes in the batch, the first table written outside the allow-list)."""
    rows = db.query(models.ChangeLog).filter(models.ChangeLog.batch_id == batch_id).all()
    for row in rows:
        if row.table_name not in allowed:
            return len(rows), row.table_name
    return len(rows), None


def run_agent(spec, trigger: str = "manual", event_ids: list[int] | None = None,
              tick_id: int | None = None, now: datetime | None = None, *, still_due=None) -> dict:
    """Run one agent. Returns the serialised ``agent_runs`` row. Never raises.

    When another run of the same agent is in progress nothing happens and the result has
    ``status: "busy"``. ``still_due(db, row)`` is the tick's second look, taken once the
    claim is held: it returns the event ids to deliver, or None when another tick has run
    the agent in the meantime (``status: "not_due"``)."""
    now = now or datetime.utcnow()
    started = time.monotonic()
    run = None
    db = None
    claim = None
    try:
        db = SessionLocal()
        row = agent_row(db, spec)
        claim = _claim(db, spec.key)
        if claim is None:
            return _not_run(spec, "busy", f"{spec.name} is already running")
        if still_due is not None:
            db.refresh(row)
            event_ids = still_due(db, row)
            if event_ids is None:
                return _not_run(spec, "not_due", f"{spec.name} already ran")
        run = models.AgentRun(agent_key=spec.key, trigger=trigger, tick_id=tick_id,
                              started_at=datetime.utcnow(), status="running")
        db.add(run)
        db.commit()
        db.refresh(run)
        run_id = run.id

        batch_id = change_log.begin_batch(db, f"agent:{spec.key}")
        db.info["agent_run_id"] = run_id
        llm.set_run_context(run_id, max_calls=spec.permissions.max_llm_calls)

        events = (db.query(models.AgentEvent).filter(models.AgentEvent.id.in_(event_ids)).all()
                  if event_ids else [])
        now_local = schedule.to_local(now)
        ctx = AgentContext(db=db, now=now, now_local=now_local, today=now_local.date(), run=run,
                           spec=spec, row=row, events=events, config=_config(spec, row))

        status, summary, result, error = "completed", "", {}, None
        try:
            outcome = spec.run(ctx)
            if not isinstance(outcome, AgentResult):
                outcome = AgentResult(summary=str(outcome or "done"))
            status = outcome.status if outcome.status in ("completed", "skipped") else "completed"
            summary, result = outcome.summary, outcome.result or {}
            db.commit()
        except Exception as exc:  # noqa: BLE001 — a failed agent is a recorded outcome, not a crash
            db.rollback()
            status, error = "failed", _safe(db, f"{type(exc).__name__}: {exc}")[:2000]
            summary = f"failed: {type(exc).__name__}"
            print(f"[agents] {spec.key} failed: {error}")

        # Permission audit — runs for failed runs too (their committed partial work counts).
        changes_count, offending = _audit(db, batch_id, spec.permissions.tables_write)
        if offending is not None:
            try:
                change_log.undo_batch(db, batch_id)
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                print(f"[agents] {spec.key}: could not undo a violating batch: {type(exc).__name__}")
            status, error = "violation", f"wrote to {offending}"
            summary = f"reverted: wrote to {offending}, which this agent may not touch"

        change_log.end_batch(db)
        db.info.pop("agent_run_id", None)
        elapsed = time.monotonic() - started

        run = db.query(models.AgentRun).filter(models.AgentRun.id == run_id).first()
        usage = db.query(models.LLMUsage).filter(models.LLMUsage.agent_run_id == run_id).all()
        run.finished_at = datetime.utcnow()
        run.status, run.summary, run.error = status, (summary or "")[:500], error
        run.result_json = json.dumps(result, default=str)
        run.llm_calls = len(usage)
        run.cost_usd = round(sum(u.estimated_cost_usd or 0.0 for u in usage), 6)
        run.changes_count, run.batch_id = changes_count, batch_id
        run.over_time = elapsed > spec.permissions.max_seconds
        row = db.query(models.Agent).filter(models.Agent.key == spec.key).first()
        row.last_run_at, row.last_status, row.last_summary = now, status, run.summary
        row.runs_count = (row.runs_count or 0) + 1
        row.cost_usd_total = round((row.cost_usd_total or 0.0) + run.cost_usd, 6)
        row.updated_at = datetime.utcnow()
        # The events were handed to this agent (whatever became of the run): say so, in the
        # same commit as the run's outcome. Other subscribers still have theirs to come.
        handed = [e.id for e in events]
        if handed:
            have = {d.event_id for d in db.query(models.AgentEventDelivery).filter(
                models.AgentEventDelivery.agent_key == spec.key,
                models.AgentEventDelivery.event_id.in_(handed)).all()}
            for event_id in handed:
                if event_id not in have:
                    db.add(models.AgentEventDelivery(event_id=event_id, agent_key=spec.key,
                                                     delivered_at=datetime.utcnow()))
        db.commit()
        return {**serialize_run(run), "event_ids": handed}
    except Exception as exc:  # noqa: BLE001 — the runner never raises
        try:
            if db is not None:
                db.rollback()
        except Exception:  # noqa: BLE001
            pass
        problem = _safe(db, f"{type(exc).__name__}: {exc}")[:2000]
        print(f"[agents] runner error for {spec.key}: {problem}")
        try:
            if db is not None and run is not None and run.id is not None:
                stuck = db.query(models.AgentRun).filter(models.AgentRun.id == run.id).first()
                if stuck is not None and stuck.status == "running":
                    stuck.status, stuck.finished_at = "failed", datetime.utcnow()
                    stuck.error = f"runner: {problem}"[:2000]
                    db.commit()
                    return serialize_run(stuck)
        except Exception:  # noqa: BLE001
            pass
        return {"agent_key": spec.key, "status": "failed", "summary": "runner error", "error": problem}
    finally:
        llm.clear_run_context()
        if db is not None:
            try:
                db.info.pop("agent_run_id", None)
                change_log.end_batch(db)
            except Exception:  # noqa: BLE001
                pass
            if claim is not None:
                _release(db, spec.key, claim)
            db.close()


# ── The tick ─────────────────────────────────────────────────

def _deliveries(db, event_ids: list[int]) -> set:
    """{(event id, agent key)} for the events given: who has already been handed what."""
    if not event_ids:
        return set()
    rows = db.query(models.AgentEventDelivery).filter(models.AgentEventDelivery.event_id.in_(event_ids)).all()
    return {(d.event_id, d.agent_key) for d in rows}


def due_now(db, now: datetime | None = None) -> list[str]:
    """Keys of the enabled agents whose schedule says they are due (events aside)."""
    now = now or datetime.utcnow()
    now_local = schedule.to_local(now)
    out = []
    for spec in agents.ordered_specs():
        row = agent_row(db, spec)
        if row.enabled and schedule.due(spec, row, now_local) and not schedule.inside_min_gap(spec, row, now):
            out.append(spec.key)
    return out


def tick(trigger: str = "manual", *, now: datetime | None = None) -> dict:
    """Run every due agent once. Returns a summary. Never raises."""
    started = time.monotonic()
    now = now or datetime.utcnow()
    now_local = schedule.to_local(now)
    budget = tick_budget_seconds()
    summary = {"tick_id": None, "trigger": trigger, "ran": [], "skipped": [], "deferred": [],
               "events_consumed": 0, "events_dropped": 0, "errors": 0, "duration_ms": 0}
    db = None
    try:
        db = SessionLocal()
        tick_row = models.JobTick(trigger=trigger, started_at=datetime.utcnow())
        db.add(tick_row)
        db.commit()
        db.refresh(tick_row)
        summary["tick_id"] = tick_row.id

        pending = db.query(models.AgentEvent).filter(
            models.AgentEvent.consumed_at.is_(None)).order_by(models.AgentEvent.id).all()
        cutoff = now - timedelta(hours=EVENT_TTL_HOURS)
        fresh = [e for e in pending if e.created_at >= cutoff]
        stale = [e for e in pending if e.created_at < cutoff]
        fresh_by_id = {e.id: e.kind for e in fresh}
        # An event goes to every subscriber, and each gets it once: what has already been
        # handed over is on record, so a subscriber this tick defers gets it next time.
        handed = _deliveries(db, list(fresh_by_id))
        listening: dict[str, set] = {}          # event kind → the enabled agents that subscribe to it

        plan = []          # (spec, trigger, event ids)
        for spec in agents.ordered_specs():
            row = agent_row(db, spec)
            if not row.enabled:
                summary["skipped"].append({"key": spec.key, "reason": "disabled"})
                continue
            for kind in spec.events or []:
                listening.setdefault(kind, set()).add(spec.key)
            matching = [eid for eid, kind in fresh_by_id.items()
                        if kind in (spec.events or []) and (eid, spec.key) not in handed]
            scheduled = schedule.due(spec, row, now_local)
            if not scheduled and not matching:
                continue
            if schedule.inside_min_gap(spec, row, now):
                summary["skipped"].append({"key": spec.key, "reason": "ran moments ago"})
                continue
            plan.append((spec, trigger if scheduled else "event", matching))
        db.commit()

        for index, (spec, why, event_ids) in enumerate(plan):
            if index > 0 and (time.monotonic() - started) >= budget:
                summary["deferred"].append(spec.key)
                continue
            how = why if why == "event" else ("in_process" if trigger == "in_process" else "schedule")
            if trigger == "manual" and why != "event":
                how = "manual"
            def still_due(run_db, row, spec=spec, event_ids=event_ids, scheduled=(why != "event")):
                """Looked at again with the claim held: another tick may have run it since
                this one made its plan."""
                if schedule.inside_min_gap(spec, row, now):
                    return None
                if scheduled and schedule.due(spec, row, now_local):
                    return event_ids
                waiting = [e.id for e in run_db.query(models.AgentEvent).filter(
                    models.AgentEvent.id.in_(event_ids), models.AgentEvent.consumed_at.is_(None)).all()
                ] if event_ids else []
                already = _deliveries(run_db, waiting)
                waiting = [eid for eid in waiting if (eid, spec.key) not in already]
                return waiting or None

            outcome = run_agent(spec, how, event_ids, tick_id=tick_row.id, now=now, still_due=still_due)
            if outcome.get("status") in ("busy", "not_due"):
                summary["skipped"].append({"key": spec.key, "reason": "already running"
                                           if outcome["status"] == "busy" else "already ran"})
                continue
            summary["ran"].append({"key": spec.key, "status": outcome.get("status"),
                                   "summary": outcome.get("summary")})
            if outcome.get("status") in ("failed", "violation"):
                summary["errors"] += 1

        # An event is consumed once every enabled subscriber has been handed it. One with
        # no subscriber waits; one older than a day is dropped.
        stamp = datetime.utcnow()
        db.expire_all()
        handed = _deliveries(db, list(fresh_by_id))
        stale_ids = {e.id for e in stale}
        for event in db.query(models.AgentEvent).filter(
                models.AgentEvent.id.in_(list(fresh_by_id) + list(stale_ids)),
                models.AgentEvent.consumed_at.is_(None)).all() if (fresh_by_id or stale_ids) else []:
            if event.id in stale_ids:
                event.consumed_at, event.dropped = stamp, True
                summary["events_dropped"] += 1
                continue
            wanted = listening.get(event.kind) or set()
            if wanted and all((event.id, key) in handed for key in wanted):
                event.consumed_at = stamp
                summary["events_consumed"] += 1

        summary["duration_ms"] = int((time.monotonic() - started) * 1000)
        tick_row = db.query(models.JobTick).filter(models.JobTick.id == tick_row.id).first()
        tick_row.finished_at = datetime.utcnow()
        tick_row.agents_run, tick_row.agents_skipped = len(summary["ran"]), len(summary["skipped"]) + len(summary["deferred"])
        tick_row.events_consumed, tick_row.errors = summary["events_consumed"], summary["errors"]
        tick_row.duration_ms = summary["duration_ms"]
        tick_row.summary_json = json.dumps(summary, default=str)
        db.commit()
    except Exception as exc:  # noqa: BLE001 — a tick never raises
        try:
            if db is not None:
                db.rollback()
        except Exception:  # noqa: BLE001
            pass
        problem = _safe(db, f"{type(exc).__name__}: {exc}")[:500]
        print(f"[agents] tick failed: {problem}")
        summary["errors"] += 1
        summary["error"] = problem
    finally:
        if db is not None:
            db.close()
    return summary


# ── Toggles and overrides ────────────────────────────────────

def update_agent(db, spec, *, enabled: bool | None = None, interval_minutes: int | None = None,
                 at_time: str | None = None, weekdays: list | None = None,
                 config: dict | None = None) -> dict:
    """Change an agent's toggle, schedule overrides or settings. Logged on the ``agents``
    row, so a toggle can be undone. The caller commits. Returns ``{"row", "change"}``.

    ``interval_minutes=0``, ``at_time=""`` and ``weekdays=[]`` clear that override."""
    row = agent_row(db, spec)
    before = change_log.snapshot(row)
    if enabled is not None:
        row.enabled = bool(enabled)
    if interval_minutes is not None:
        value = int(interval_minutes)
        if value < 0:
            raise ValueError("interval_minutes must be zero or more")
        row.interval_minutes = value or None
    if at_time is not None:
        text = str(at_time).strip()
        if text and schedule.parse_time(text) is None:
            raise ValueError("at_time must look like 06:00")
        row.at_time = ("%02d:%02d" % schedule.parse_time(text)) if text else None
    if weekdays is not None:
        days = sorted({int(d) for d in weekdays})
        if any(d < 0 or d > 6 for d in days):
            raise ValueError("weekdays are 0 (Monday) to 6 (Sunday)")
        row.weekdays_json = json.dumps(days) if days else None
    if config is not None:
        if not isinstance(config, dict):
            raise ValueError("config must be an object")
        merged = _config(spec, row)
        merged.update(config)
        row.config_json = json.dumps(merged, default=str)
    if change_log.snapshot(row) != before:
        row.updated_at = datetime.utcnow()
    state = "on" if row.enabled else "off"
    change = change_log.record_update(db, row, before, f"Agent {spec.name}: {state}, {schedule.describe(schedule.effective(spec, row))}")
    return {"row": row, "change": change}
