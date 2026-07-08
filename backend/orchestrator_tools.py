"""
Phase 6 — Orchestrator tools.

The tools the orchestrator can call. Every tool takes a live SQLAlchemy ``Session``
plus keyword args and returns ``(result_dict, summary_str)`` where ``summary_str`` is the
short human line surfaced in the SSE ``tool_result`` event.

These tools reuse the existing, audited application logic (``apply_parse_updates``,
``_compute_gate``, ``/subjects/weakest``, ``/context/search``) rather than duplicating it.
``main`` is imported lazily inside each tool to avoid a circular import
(main -> orchestrator -> orchestrator_tools -> main).
"""

import os
import json
from datetime import date, timedelta

from sqlalchemy import func

import models

_RULES_PATH = os.path.join(
    os.path.dirname(__file__), "..", "context", "core", "off_track_rules.json"
)

TEN_K_START_DATE = date(2026, 6, 1)


# ════════════════════════════════════════════════════════════════
# Tool 1 — route_brain_dump
# ════════════════════════════════════════════════════════════════

def route_brain_dump(db, text: str):
    """Route a logging statement through the existing parse pipeline.

    This is the ONLY tool that writes to the system, and it does so exclusively via
    ``main.apply_parse_updates`` — the same audited path used by /parse and /input.
    """
    import main

    roadmap_ids = [t.task_id for t in db.query(models.RoadmapTask).all()]
    context = {"roadmap_ids": roadmap_ids, "today": str(date.today())}
    parsed = main.get_parse_response(text, context)
    updates = main.apply_parse_updates(db, parsed, date.today())

    summary = parsed.get("summary") or "Entry logged."
    result = {
        "summary": summary,
        "advisory": parsed.get("advisory"),
        "updates": updates,
    }
    if updates.get("gate_locked_warning"):
        result["gate_locked_warning"] = updates["gate_locked_warning"]
    return result, summary


# ════════════════════════════════════════════════════════════════
# Tool 2 — get_dashboard_state
# ════════════════════════════════════════════════════════════════

def get_dashboard_state(db):
    """Live snapshot: KPIs, habits done/total, active todos, gate status, 10K progress,
    weakest subject."""
    import main

    today = date.today()
    gate = main._compute_gate(db)

    habits = db.query(models.Habit).filter(models.Habit.date == today).all()
    habits_done = sum(1 for h in habits if h.done)
    todos_active = db.query(models.Todo).filter(models.Todo.done == False).count()

    ai_revenue = float(db.query(func.sum(models.Revenue.amount)).filter(
        models.Revenue.date >= TEN_K_START_DATE
    ).scalar() or 0.0)
    trading_pl = float(db.query(func.sum(models.LiveTrade.net_pl_usd)).filter(
        models.LiveTrade.date >= TEN_K_START_DATE,
        models.LiveTrade.net_pl_usd.isnot(None),
    ).scalar() or 0.0)
    ten_k_total = round(ai_revenue + trading_pl, 2)

    weakest, _ = surface_weakest_subtopic(db, limit=1)
    weak_list = weakest.get("subtopics", [])
    weakest_subject = weak_list[0]["subject_name"] if weak_list else None

    state = {
        "kpis": {k: v.get("value") for k, v in main._kpi_state.items()},
        "habits": {"done": habits_done, "total": len(habits)},
        "todos_active": todos_active,
        "gate_status": gate["status"],
        "gate": gate,
        "ten_k_progress": {
            "target_usd": 10000,
            "total_progress_usd": ten_k_total,
            "ai_revenue_usd": round(ai_revenue, 2),
            "trading_pl_usd": round(trading_pl, 2),
        },
        "weakest_subject": weakest_subject,
    }
    summary = (
        f"gate {gate['status']}, habits {habits_done}/{len(habits)}, "
        f"{todos_active} active todos, 10K at ${ten_k_total}"
    )
    return state, summary


# ════════════════════════════════════════════════════════════════
# Tool 3 — detect_off_track  (data-driven rules)
# ════════════════════════════════════════════════════════════════

def _load_rules() -> list:
    with open(_RULES_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh).get("rules", [])


def _check_no_revenue_since(db, params) -> bool:
    """True (fires) when no revenue exists in the trailing window, but only once we are
    past ``after_date`` (so the alert can't fire before the first-revenue phase opens)."""
    today = date.today()
    after = date.fromisoformat(params.get("after_date", "2026-06-01"))
    if today < after:
        return False
    window = int(params.get("window_days", 30))
    floor = max(after, today - timedelta(days=window))
    count = db.query(models.Revenue).filter(models.Revenue.date >= floor).count()
    return count == 0


def _check_habit_completion_below(db, params) -> bool:
    threshold = float(params.get("threshold_pct", 60))
    window = int(params.get("window_days", 7))
    today = date.today()
    floor = today - timedelta(days=window - 1)
    rows = db.query(models.Habit).filter(
        models.Habit.date >= floor, models.Habit.date <= today
    ).all()
    if not rows:
        return False
    done = sum(1 for h in rows if h.done)
    pct = (done / len(rows)) * 100
    return pct < threshold


def _check_annual_target_status(db, params) -> bool:
    import main
    target_status = params.get("status", "critical")
    year_pct = main._year_pct()
    targets = db.query(models.AnnualTarget).filter(
        models.AnnualTarget.is_active == True
    ).all()
    for t in targets:
        ser = main._serialize_annual_target(t, year_pct)
        if ser.get("status") == target_status:
            return True
    return False


def _check_backtest_stalled(db, params) -> bool:
    import main
    gate = main._compute_gate(db)
    if gate["status"] != "LOCKED":
        return False
    window = int(params.get("window_days", 7))
    floor = date.today() - timedelta(days=window)
    recent = db.query(models.BacktestTrade).filter(
        models.BacktestTrade.date > floor
    ).count()
    return recent == 0


def _check_zero_mastery_high_weight(db, params) -> bool:
    min_weight = int(params.get("min_weight", 7))
    row = (
        db.query(models.Subtopic)
        .join(models.Topic, models.Subtopic.topic_id == models.Topic.id)
        .filter(models.Subtopic.mastery_level == 0)
        .filter(models.Topic.syllabus_weight >= min_weight)
        .first()
    )
    return row is not None


_RULE_CHECKS = {
    "no_revenue_since": _check_no_revenue_since,
    "habit_completion_below": _check_habit_completion_below,
    "annual_target_status": _check_annual_target_status,
    "backtest_stalled": _check_backtest_stalled,
    "zero_mastery_high_weight": _check_zero_mastery_high_weight,
}


def detect_off_track(db):
    """Evaluate every enabled rule from off_track_rules.json. Returns a list of alerts.

    Adding a rule that reuses an existing ``check`` type requires NO code change.
    """
    alerts = []
    for rule in _load_rules():
        if not rule.get("enabled", True):
            continue
        check = _RULE_CHECKS.get(rule.get("check"))
        if check is None:
            continue
        if check(db, rule.get("params", {})):
            alerts.append({
                "rule_id": rule["id"],
                "severity": rule.get("severity", "info"),
                "message": rule.get("message", rule["id"]),
            })
    summary = f"{len(alerts)} alert(s)" if alerts else "no alerts"
    return alerts, summary


# ════════════════════════════════════════════════════════════════
# Tool 4 — synthesize_weekly_review
# ════════════════════════════════════════════════════════════════

def synthesize_weekly_review(db, week_start: str | None = None):
    """Four-section weekly review computed from live DB data:
    SHIPPED, REVENUE, NEGOTIATED, NEXT."""
    if week_start:
        start = date.fromisoformat(week_start)
    else:
        today = date.today()
        start = today - timedelta(days=today.weekday())   # Monday of current week
    end = start + timedelta(days=6)
    start_dt = datetime_min(start)
    end_dt = datetime_max(end)

    # SHIPPED — roadmap tasks completed + habits done this week
    shipped = [t.title for t in db.query(models.RoadmapTask).filter(
        models.RoadmapTask.done == True
    ).all()]
    habit_done = db.query(models.Habit).filter(
        models.Habit.date >= start, models.Habit.date <= end, models.Habit.done == True
    ).count()
    if habit_done:
        shipped.append(f"{habit_done} habit completions")

    # REVENUE — revenue rows dated within the week
    rev_rows = db.query(models.Revenue).filter(
        models.Revenue.date >= start, models.Revenue.date <= end
    ).all()
    revenue = [
        {"amount": r.amount, "source": r.source, "date": str(r.date)} for r in rev_rows
    ]
    revenue_total = round(sum(r.amount for r in rev_rows), 2)

    # NEGOTIATED — clients created or updated this week
    clients = db.query(models.Client).filter(
        models.Client.updated_at >= start_dt, models.Client.updated_at <= end_dt
    ).all()
    negotiated = [
        {"name": c.name, "status": c.status, "value": c.value} for c in clients
    ]

    # NEXT — top active todos + weakest subtopic
    next_items = [t.text for t in db.query(models.Todo).filter(
        models.Todo.done == False
    ).order_by(models.Todo.priority.asc()).limit(5).all()]
    weakest, _ = surface_weakest_subtopic(db, limit=1)
    if weakest.get("subtopics"):
        w = weakest["subtopics"][0]
        next_items.append(f"Weakest: {w['name']} ({w['subject_name']})")

    result = {
        "week_start": str(start),
        "week_end": str(end),
        "SHIPPED": shipped,
        "REVENUE": {"entries": revenue, "total_usd": revenue_total},
        "NEGOTIATED": negotiated,
        "NEXT": next_items,
    }
    summary = (
        f"week {start}: {len(shipped)} shipped, ${revenue_total} revenue, "
        f"{len(negotiated)} negotiated, {len(next_items)} next"
    )
    return result, summary


def datetime_min(d: date):
    from datetime import datetime
    return datetime(d.year, d.month, d.day, 0, 0, 0)


def datetime_max(d: date):
    from datetime import datetime
    return datetime(d.year, d.month, d.day, 23, 59, 59)


# ════════════════════════════════════════════════════════════════
# Tool 5 — surface_weakest_subtopic  (wraps /subjects/weakest)
# ════════════════════════════════════════════════════════════════

def surface_weakest_subtopic(db, limit: int = 1):
    import main
    rows = main.weakest_subtopics(limit=limit, db=db)
    result = {"subtopics": rows}
    if rows:
        top = rows[0]
        summary = f"weakest: {top['name']} ({top['subject_name']}, mastery {top['mastery_level']})"
    else:
        summary = "no subtopics found"
    return result, summary


# ════════════════════════════════════════════════════════════════
# Tool 6 — query_war_room  (wraps /context/search)
# ════════════════════════════════════════════════════════════════

def query_war_room(db, q: str):
    import main
    if not q or len(q) < 2:
        return {"query": q, "results": []}, "query too short"
    out = main.search_context(q=q, db=db)
    results = out.get("results", [])
    return out, f"{len(results)} match(es) for '{q}'"


# ════════════════════════════════════════════════════════════════
# Tool 7 — add_todos
# ════════════════════════════════════════════════════════════════

def add_todos(db, items: list, category: str = "personal", priority: int = 5):
    """Create Todo rows for each item. Only called when the user explicitly instructs.

    Never called for advisory or query responses. Source is always "orchestrator".
    """
    today = str(date.today())
    created = []
    for text in items:
        todo = models.Todo(
            text=text,
            priority=priority,
            done=False,
            category=category,
            due=today,
            source="orchestrator",
            roadmap_id=None,
            completed_at=None,
        )
        db.add(todo)
        db.flush()
        created.append({"id": todo.id, "text": todo.text})
    db.commit()
    result = {"created": len(created), "todos": created}
    summary = f"added {len(created)} todo(s)"
    return result, summary


# ════════════════════════════════════════════════════════════════
# Tool 8 — query_cold_archive
# ════════════════════════════════════════════════════════════════

def query_cold_archive(db, query: str, limit: int = 5):
    """Search Bandile's Obsidian vault (cold archive).

    ONLY called when the user explicitly instructs a vault/notes/second-brain search.
    Never called autonomously.
    """
    from vault_sync import search_vault

    if not query or len(query) < 2:
        return {"query": query, "results": []}, "query too short"

    raw = search_vault(query, db, limit=limit)
    results = []
    for r in raw:
        results.append({
            "file_title": r["file_title"],
            "file_path": r["file_path"],
            "excerpt": r["content"][:400] + ("…" if len(r["content"]) > 400 else ""),
            "chunk_index": r["chunk_index"],
        })

    summary = f"{len(results)} vault match(es) for '{query}'"
    return {"query": query, "results": results}, summary


# ════════════════════════════════════════════════════════════════
# Tool 9 — run_market_research_agent
# ════════════════════════════════════════════════════════════════

def run_market_research_agent(db, objective: str, max_opportunities: int = 3):
    """Run the Market Intelligence Agent on a research objective.

    SURFACES findings only (status='surfaced'). Never writes to the opportunity
    pipeline — promotion is a separate explicit action (promote_research_opportunity).
    Slow tool: multiple LLM calls + source searches (~30-90s).
    """
    from market_intel.agent import run_market_research

    result = run_market_research(db, objective, max_opportunities)
    if result.get("error"):
        return result, result["error"]
    n = len(result.get("opportunities", []))
    summary = (
        f"{n} opportunit{'y' if n == 1 else 'ies'} surfaced "
        f"({result.get('rejected_count', 0)} rejected by verifier) — "
        f"project #{result.get('project_id')}"
    )
    return result, summary


# ════════════════════════════════════════════════════════════════
# Tool 10 — promote_research_opportunity
# ════════════════════════════════════════════════════════════════

def promote_research_opportunity(db, finding_id: int, notes: str = None):
    """Promote a surfaced finding into the opportunity pipeline (stage 'discovered').

    ONLY called when the user explicitly instructs a save/promote. This is the
    single write path into the pipeline. Idempotent: promoting an already-promoted
    finding is reported, not duplicated.
    """
    finding = db.query(models.ResearchFinding).filter(
        models.ResearchFinding.id == finding_id
    ).first()
    if finding is None:
        return {"error": f"finding {finding_id} not found"}, f"finding {finding_id} not found"

    existing = db.query(models.OpportunityPipeline).filter(
        models.OpportunityPipeline.finding_id == finding.id
    ).first()
    if existing is not None:
        summary = f"finding {finding.id} already in pipeline (stage {existing.stage})"
        return {
            "finding_id": finding.id,
            "pipeline_id": existing.id,
            "stage": existing.stage,
            "already_promoted": True,
        }, summary

    entry = models.OpportunityPipeline(finding_id=finding.id, stage="discovered", notes=notes)
    finding.status = "promoted"
    db.add(entry)
    db.commit()
    db.refresh(entry)

    summary = f"finding {finding.id} promoted to pipeline (discovered)"
    result = {
        "finding_id": finding.id,
        "pipeline_id": entry.id,
        "stage": entry.stage,
        "problem": finding.problem,
        "already_promoted": False,
    }
    return result, summary


# ════════════════════════════════════════════════════════════════
# Dispatch + JSON tool schemas for the model
# ════════════════════════════════════════════════════════════════

_TOOL_DISPATCH = {
    "route_brain_dump": lambda db, a: route_brain_dump(db, a.get("text", "")),
    "get_dashboard_state": lambda db, a: get_dashboard_state(db),
    "detect_off_track": lambda db, a: detect_off_track(db),
    "synthesize_weekly_review": lambda db, a: synthesize_weekly_review(db, a.get("week_start")),
    "surface_weakest_subtopic": lambda db, a: surface_weakest_subtopic(db, int(a.get("limit", 1))),
    "query_war_room": lambda db, a: query_war_room(db, a.get("q", "")),
    "add_todos": lambda db, a: add_todos(
        db,
        items=a.get("items", []),
        category=a.get("category", "personal"),
        priority=int(a.get("priority", 5)),
    ),
    "query_cold_archive": lambda db, a: query_cold_archive(
        db,
        query=a.get("query", ""),
        limit=int(a.get("limit", 5)),
    ),
    "run_market_research_agent": lambda db, a: run_market_research_agent(
        db,
        objective=a.get("objective", ""),
        max_opportunities=int(a.get("max_opportunities", 3)),
    ),
    "promote_research_opportunity": lambda db, a: promote_research_opportunity(
        db,
        finding_id=int(a.get("finding_id", 0)),
        notes=a.get("notes"),
    ),
}


def run_tool(name: str, args: dict, db):
    """Execute a tool by name. Returns ``(result_dict, summary_str)``.

    Unknown tools return an error payload rather than raising, so a hallucinated tool
    name never crashes the stream.
    """
    fn = _TOOL_DISPATCH.get(name)
    if fn is None:
        return {"error": f"unknown tool: {name}"}, f"unknown tool: {name}"
    return fn(db, args or {})


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "route_brain_dump",
            "description": (
                "Log a statement of something that happened (habit done, workout, revenue, "
                "task completed, progress). Routes through the audited parse pipeline. This is "
                "the only way to write to the system. Use only when the user is logging."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The raw log text."}
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_dashboard_state",
            "description": (
                "Get a live snapshot: KPIs, habits done/total, active todos, trading gate "
                "status, 10K progress, and weakest subject."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "detect_off_track",
            "description": "Evaluate off-track rules and return any active alerts.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "synthesize_weekly_review",
            "description": "Produce a weekly review with SHIPPED, REVENUE, NEGOTIATED, NEXT sections.",
            "parameters": {
                "type": "object",
                "properties": {
                    "week_start": {
                        "type": "string",
                        "description": "ISO date (YYYY-MM-DD) for the Monday of the week. Optional.",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "surface_weakest_subtopic",
            "description": "Return the weakest academic subtopic(s), weighted by syllabus weight.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "How many to return. Default 1."}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_war_room",
            "description": "Keyword search across War Room context documents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "q": {"type": "string", "description": "Search query."}
                },
                "required": ["q"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_todos",
            "description": (
                "Add tasks explicitly named by the user to today's execution stack. "
                "ONLY call this when the user has used explicit add/log/stack/save language "
                "(e.g. 'add these to my stack', 'put these in my todos', 'log these as tasks'). "
                "Do NOT call when the user is asking a question, brainstorming, or asking for advice."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Exact task texts the user named. Do not add, invent, or round out.",
                    },
                    "category": {
                        "type": "string",
                        "description": "Category for all items. Default 'personal'.",
                    },
                    "priority": {
                        "type": "integer",
                        "description": "Priority for all items (1=high, 9=low). Default 5.",
                    },
                },
                "required": ["items"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_cold_archive",
            "description": (
                "Query the cold archive — Bandile's Obsidian vault containing Apple Notes dumps, "
                "journal entries, past project documents, and personal archives. ONLY call this "
                "tool when the user explicitly asks to search their second brain, memory, or vault. "
                "Trigger phrases: 'look in my vault', 'check my notes', 'search my second brain', "
                "'do I have anything about X in my archive', 'find in my obsidian'. Do NOT call "
                "this tool during normal conversation, brain dumps, or when answering questions "
                "from general knowledge. The vault is cold storage — it is never queried automatically."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The keyword(s) to search for in the vault.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum results to return. Default 5.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_market_research_agent",
            "description": (
                "Run the Market Intelligence Agent: a multi-role research pipeline "
                "(planner, parallel researchers, analyst, founder advisor, verifier) that "
                "investigates a business research objective across web, War Room, and vault "
                "sources and returns structured, evidence-backed opportunities with scores. "
                "ONLY call when the user explicitly commands research (e.g. 'research X', "
                "'find painful problems in Y', 'run market research on Z'). It SURFACES "
                "findings only — it never saves them to the opportunity pipeline. Slow tool "
                "(30-90s); never call it speculatively or during normal conversation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "objective": {
                        "type": "string",
                        "description": "The research objective, e.g. 'find painful problems in Zambian SMEs where AI automation could create a valuable business'.",
                    },
                    "max_opportunities": {
                        "type": "integer",
                        "description": "Maximum opportunities to surface (1-5). Default 3.",
                    },
                },
                "required": ["objective"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "promote_research_opportunity",
            "description": (
                "Save a surfaced research finding into the opportunity pipeline (stage "
                "'discovered'). ONLY call when the user explicitly instructs a save/promote "
                "with clear language ('save opportunity 12', 'promote that finding', 'add it "
                "to the pipeline'). NEVER call as a side effect of research, and never on "
                "your own initiative. Requires the finding id from a prior research result."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "finding_id": {
                        "type": "integer",
                        "description": "The id of the research finding to promote.",
                    },
                    "notes": {
                        "type": "string",
                        "description": "Optional note to attach to the pipeline entry.",
                    },
                },
                "required": ["finding_id"],
            },
        },
    },
]
