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
        "changes": updates.get("changes", []),
    }
    if updates.get("suggestions"):
        result["suggestions_pending"] = len(updates["suggestions"])
    return result, summary


# ════════════════════════════════════════════════════════════════
# Tool 2 — get_dashboard_state
# ════════════════════════════════════════════════════════════════

def get_dashboard_state(db):
    """Live snapshot: KPIs, habits done/total, active todos, overdue count, 10K progress,
    weakest subject, upcoming exams."""
    import main
    import syllabus

    today = date.today()
    habits = db.query(models.Habit).filter(models.Habit.date == today).all()
    habits_done = sum(1 for h in habits if h.done)
    open_todos = db.query(models.Todo).filter(models.Todo.done == False).all()
    overdue = sum(1 for t in open_todos if t.due_date and t.due_date < today)

    ai_revenue = main._revenue_usd_total(db, since=TEN_K_START_DATE)

    weakest, _ = surface_weakest_topics(db, limit=1)
    weak_list = weakest.get("topics", [])
    weakest_subject = weak_list[0]["subject_name"] if weak_list else None

    exams = [e for e in syllabus.upcoming_exams(db, today) if e["days_away"] >= 0][:4]

    state = {
        "kpis": {k: v.get("value") for k, v in main._kpi_state.items()},
        "habits": {"done": habits_done, "total": len(habits)},
        "todos_active": len(open_todos),
        "todos_overdue": overdue,
        "ten_k_progress": {
            "target_usd": 10000,
            "total_progress_usd": ai_revenue,
            "ai_revenue_usd": ai_revenue,
        },
        "weakest_subject": weakest_subject,
        "upcoming_exams": exams,
    }
    summary = (
        f"habits {habits_done}/{len(habits)}, {len(open_todos)} active todos "
        f"({overdue} overdue), 10K at ${ai_revenue}"
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


def _check_non_negotiable_completion_below(db, params) -> bool:
    """Completion of the ACTIVE non-negotiables over the window. Days with no habit row
    for an active key count as not done, so skipping the app does not hide drift."""
    threshold = float(params.get("threshold_pct", 60))
    window = int(params.get("window_days", 7))
    today = date.today()
    floor = today - timedelta(days=window - 1)
    keys = [n.key for n in db.query(models.NonNegotiable).filter(models.NonNegotiable.is_active == True).all()]
    if not keys:
        return False
    rows = db.query(models.Habit).filter(
        models.Habit.date >= floor, models.Habit.date <= today, models.Habit.key.in_(keys)
    ).all()
    if not rows:
        return False   # nothing logged in the window at all — no signal rather than a false alarm
    days_with_data = {h.date for h in rows}
    total = len(keys) * len(days_with_data)
    done = sum(1 for h in rows if h.done)
    pct = (done / total) * 100 if total else 100
    return pct < threshold


# Legacy name kept so older rule files still evaluate.
_check_habit_completion_below = _check_non_negotiable_completion_below


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


def _check_syllabus_weak_before_exam(db, params) -> bool:
    """Red/yellow syllabus topics whose exam is within ``days``."""
    import syllabus
    days = int(params.get("days", 60))
    min_topics = int(params.get("min_topics", 1))
    today = date.today()
    weak = 0
    for p in db.query(models.SyllabusPoint).filter(
            models.SyllabusPoint.status.in_(["not_started", "learned"])).all():
        exam = syllabus.get_exam_date(db, p.subject_code, p.paper)
        if exam and 0 <= (exam - today).days <= days:
            weak += 1
            if weak >= min_topics:
                return True
    return False


_RULE_CHECKS = {
    "no_revenue_since": _check_no_revenue_since,
    "non_negotiable_completion_below": _check_non_negotiable_completion_below,
    "habit_completion_below": _check_habit_completion_below,   # legacy alias
    "annual_target_status": _check_annual_target_status,
    "syllabus_weak_before_exam": _check_syllabus_weak_before_exam,
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
    # RoadmapTask has no title column — task_id is the human-readable key (e.g. "sprint-acc").
    shipped = [t.task_id for t in db.query(models.RoadmapTask).filter(
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
    weakest, _ = surface_weakest_topics(db, limit=1)
    if weakest.get("topics"):
        w = weakest["topics"][0]
        next_items.append(f"Weakest: {w['topic_code']} {w['title']} ({w['subject_name']})")

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
# Tool 5 — surface_weakest_topics  (syllabus_points: status, staleness, notes, exam proximity)
# ════════════════════════════════════════════════════════════════

def surface_weakest_topics(db, limit: int = 5, subject: str | None = None):
    import syllabus
    rows = syllabus.weakest_topics(db, limit=limit * 3 if subject else limit)
    if subject:
        code = syllabus.resolve_subject_code(db, subject)
        rows = [r for r in rows if r["subject_code"] == code][:limit]
    result = {"topics": rows}
    if rows:
        top = rows[0]
        summary = (f"weakest: {top['subject_name']} {top['paper'] or ''} {top['topic_code']} {top['title']} "
                   f"({top['status']}; {top['reason']})").replace("  ", " ")
    else:
        summary = "no syllabus topics imported yet"
    return result, summary


def surface_weakest_subtopic(db, limit: int = 1):
    """Legacy wrapper — the assistant no longer reads the old subtopic tables."""
    return surface_weakest_topics(db, limit=limit)


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
    import change_log
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
        change_log.record_create(db, todo, f"Added task: {str(text)[:80]}")
        created.append({"id": todo.id, "text": todo.text})
    db.commit()
    result = {"created": len(created), "todos": created}
    summary = f"added {len(created)} todo(s)"
    return result, summary


# ════════════════════════════════════════════════════════════════
# Tool 8 — query_cold_archive
# ════════════════════════════════════════════════════════════════

def query_cold_archive(db, query: str, limit: int = 5):
    """Search Bandile's Obsidian vault (cold archive) by meaning, falling back to
    keywords when embeddings are unavailable. Called whenever the vault is relevant."""
    from vault_sync import search_vault, search_vault_semantic

    if not query or len(query) < 2:
        return {"query": query, "results": []}, "query too short"

    semantic = search_vault_semantic(query, db, limit)
    raw = semantic if semantic else search_vault(query, db, limit=limit)
    results = []
    for r in raw:
        results.append({
            "file_title": r["file_title"],
            "file_path": r["file_path"],
            "excerpt": r["content"][:400] + ("…" if len(r["content"]) > 400 else ""),
            "chunk_index": r["chunk_index"],
            "score": r.get("score"),
        })

    mode = "semantic" if semantic else "keyword"
    summary = f"{len(results)} vault match(es) for '{query}' ({mode})"
    return {"query": query, "mode": mode, "results": results}, summary


# ════════════════════════════════════════════════════════════════
# Tool 9 — run_market_research_agent
# ═══════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════
# Tool 10 — promote_research_opportunity
# ═══════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════
# Tool 11 — remember  (long-term memory)
# ═══════════════════════════════════════════════════════════════

def remember(db, text: str, kind: str = "fact", tags: list | None = None):
    """Store a durable fact, decision or preference in long-term memory."""
    import memory_service
    row = memory_service.remember(db, text, kind=kind, tags=tags)
    result = {"id": row.id, "kind": row.kind, "text": row.context_data, "tags": row.tags}
    return result, f"remembered ({row.kind}): {row.context_data[:80]}"


# ═══════════════════════════════════════════════════════════════
# Tools 12-14 — deletes need confirmation
# ═══════════════════════════════════════════════════════════════

def propose_delete(db, table: str, record_id: int, reason: str | None = None):
    """Record a delete the user asked for as a pending action. Deletes NOTHING.
    The assistant must ask for an explicit yes, then call confirm_action."""
    import actions
    pending = actions.propose_delete(db, table, int(record_id), reason)
    return pending, f"awaiting confirmation: {pending['summary']}"


def confirm_action(db, pending_id: int):
    """Execute a pending action after the user has explicitly confirmed it."""
    import actions
    result = actions.confirm_action(db, int(pending_id))
    if result.get("executed"):
        return result, f"done: {result['summary']}"
    return result, f"not executed: {result.get('reason', 'unknown')}"


def cancel_action(db, pending_id: int):
    import actions
    result = actions.cancel_action(db, int(pending_id))
    return result, f"cancelled: {result['summary']}"


# ═══════════════════════════════════════════════════════════════
# Tools 15-21 — tasks, notes, deadlines, exam dates
# ═══════════════════════════════════════════════════════════════

def complete_task(db, query: str | None = None, todo_id: int | None = None):
    """Complete an open task by id or by fuzzy text. Ambiguity becomes suggestions."""
    import main
    import actions
    import change_log

    if todo_id:
        todo = db.query(models.Todo).filter(models.Todo.id == int(todo_id)).first()
        if todo is None:
            return {"error": f"task {todo_id} not found"}, f"task {todo_id} not found"
        candidates = []
    else:
        todo, candidates = main.resolve_todo_completion(db, query or "")

    if todo is not None:
        if todo.done:
            return {"id": todo.id, "text": todo.text, "already_done": True}, f"'{todo.text}' was already done"
        before = change_log.snapshot(todo)
        todo.done = True
        todo.completed_at = date.today()
        change = change_log.record_update(db, todo, before, f"Completed task: {todo.text[:80]}")
        db.commit()
        return {"id": todo.id, "text": todo.text, "completed": True, "change": change}, f"completed '{todo.text}'"

    if candidates:
        suggestions = [actions.add_suggestion(
            db, "todo_complete", {"todo_id": c.id, "text": c.text, "fragment": query, "score": score},
            confidence=score, reason=f"'{query}' could mean this task — tap to complete it",
            source_text=query, source="assistant",
        ) for score, c in candidates]
        db.commit()
        return ({"completed": False, "ambiguous": True,
                 "candidates": [{"id": c.id, "text": c.text, "score": sc} for sc, c in candidates],
                 "suggestions": suggestions},
                f"{len(candidates)} tasks could match '{query}' — suggestions created")
    return {"completed": False, "matched": False}, f"no open task matches '{query}'"


def notes_search(db, query: str, kind: str | None = None, limit: int = 5):
    import notes
    results = notes.search_notes(db, query, kind=kind, limit=limit)
    return {"query": query, "results": results}, f"{len(results)} note(s) for '{query}'"


def notes_read(db, title: str | None = None, note_id: int | None = None):
    import notes
    note = None
    if note_id:
        note = db.query(models.Note).filter(models.Note.id == int(note_id)).first()
    elif title:
        note = notes.find_note_fuzzy(db, title)
    if note is None:
        return {"error": f"note not found: {title or note_id}"}, "note not found"
    return notes.serialize(note), f"read note '{note.title}' ({len(note.body)} chars)"


def notes_append(db, title: str, text: str, kind: str = "general", date_str: str | None = None):
    import notes
    on = None
    if date_str:
        try:
            on = date.fromisoformat(date_str)
        except ValueError:
            on = None
    note = notes.append_entry(db, title, kind, text, on=on, fuzzy=True)
    db.commit()
    return notes.serialize(note, body=False), f"appended to note '{note.title}'"


def notes_create(db, title: str, kind: str = "general", body: str = "", tags: list | None = None):
    import notes
    existing = notes.find_note(db, title, kind)
    if existing:
        return {**notes.serialize(existing, body=False), "already_exists": True}, f"note '{title}' already exists"
    note = notes.create_note(db, title, kind, body, tags)
    db.commit()
    return notes.serialize(note, body=False), f"created note '{note.title}'"


def get_deadlines(db, days: int = 14):
    """Overdue and upcoming tasks (by real due date) plus exams within the window."""
    import syllabus
    today = date.today()
    horizon = today + timedelta(days=int(days))
    open_todos = db.query(models.Todo).filter(models.Todo.done == False).all()

    def _t(t):
        return {"id": t.id, "text": t.text, "due": t.due_date.isoformat() if t.due_date else None,
                "days": (t.due_date - today).days if t.due_date else None,
                "priority": t.priority, "category": t.category}

    overdue = sorted([_t(t) for t in open_todos if t.due_date and t.due_date < today], key=lambda x: x["due"])
    due_today = [_t(t) for t in open_todos if t.due_date == today]
    upcoming = sorted([_t(t) for t in open_todos if t.due_date and today < t.due_date <= horizon], key=lambda x: x["due"])
    undated = [_t(t) for t in open_todos if not t.due_date]
    exams = [e for e in syllabus.upcoming_exams(db, today) if 0 <= e["days_away"] <= max(int(days), 60)]
    result = {"today": today.isoformat(), "window_days": int(days), "overdue": overdue,
              "due_today": due_today, "upcoming": upcoming, "undated_count": len(undated), "exams": exams}
    summary = f"{len(overdue)} overdue, {len(due_today)} due today, {len(upcoming)} upcoming, {len(exams)} exam(s) soon"
    return result, summary


def update_syllabus(db, topic: str, status: str | None = None, weakness_note: str | None = None,
                    subject: str | None = None, paper: str | None = None, past_paper_score: float | None = None):
    """'Revised S1 probability today, still shaky on conditional probability' →
    status revised, last_revised today, weakness note appended."""
    import syllabus
    result = syllabus.update_syllabus(db, topic, status=status, weakness_note=weakness_note,
                                      subject=subject, paper=paper, past_paper_score=past_paper_score)
    if result.get("matched"):
        db.commit()
        label = f"{result['subject_name']} {result['paper'] or ''} {result['topic_code']} {result['title']}".replace("  ", " ")
        return result, f"{label} → {result['status']}" + (" (note added)" if weakness_note else "")
    if result.get("ambiguous"):
        return result, f"{len(result['candidates'])} topics could match '{topic}' — ask which"
    return result, f"no syllabus topic matches '{topic}'"


def set_exam_date(db, subject: str, date_text: str, paper: str | None = None):
    import syllabus
    d = syllabus.parse_date_text(date_text)
    if d is None:
        return {"error": f"could not read a date from '{date_text}'"}, "bad date"
    result = syllabus.set_exam_date(db, subject, d, paper)
    db.commit()
    label = f"{result['subject']}{' ' + result['paper'] if result['paper'] else ''}"
    return result, f"{label} exam set to {result['exam_date']}"


# ═══════════════════════════════════════════════════════════════

# Dispatch + JSON tool schemas for the model
# ════════════════════════════════════════════════════════════════

_TOOL_DISPATCH = {
    "route_brain_dump": lambda db, a: route_brain_dump(db, a.get("text", "")),
    "get_dashboard_state": lambda db, a: get_dashboard_state(db),
    "detect_off_track": lambda db, a: detect_off_track(db),
    "synthesize_weekly_review": lambda db, a: synthesize_weekly_review(db, a.get("week_start")),
    "surface_weakest_topics": lambda db, a: surface_weakest_topics(db, int(a.get("limit", 5)), a.get("subject")),
    "surface_weakest_subtopic": lambda db, a: surface_weakest_topics(db, int(a.get("limit", 1))),   # legacy name
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
    "remember": lambda db, a: remember(
        db,
        text=a.get("text", ""),
        kind=a.get("kind", "fact"),
        tags=a.get("tags"),
    ),
    "propose_delete": lambda db, a: propose_delete(
        db, table=a.get("table", ""), record_id=int(a.get("record_id", 0)), reason=a.get("reason"),
    ),
    "confirm_action": lambda db, a: confirm_action(db, int(a.get("pending_id", 0))),
    "cancel_action": lambda db, a: cancel_action(db, int(a.get("pending_id", 0))),
    "complete_task": lambda db, a: complete_task(db, query=a.get("query"), todo_id=a.get("todo_id")),
    "notes_search": lambda db, a: notes_search(db, a.get("query", ""), a.get("kind"), int(a.get("limit", 5))),
    "notes_read": lambda db, a: notes_read(db, title=a.get("title"), note_id=a.get("note_id")),
    "notes_append": lambda db, a: notes_append(
        db, title=a.get("title", ""), text=a.get("text", ""), kind=a.get("kind", "general"), date_str=a.get("date"),
    ),
    "notes_create": lambda db, a: notes_create(
        db, title=a.get("title", ""), kind=a.get("kind", "general"), body=a.get("body", ""), tags=a.get("tags"),
    ),
    "get_deadlines": lambda db, a: get_deadlines(db, int(a.get("days", 14))),
    "update_syllabus": lambda db, a: update_syllabus(
        db, topic=a.get("topic", ""), status=a.get("status"), weakness_note=a.get("weakness_note"),
        subject=a.get("subject"), paper=a.get("paper"), past_paper_score=a.get("past_paper_score"),
    ),
    "set_exam_date": lambda db, a: set_exam_date(
        db, subject=a.get("subject", ""), date_text=a.get("date", ""), paper=a.get("paper"),
    ),
}


def run_tool(name: str, args: dict, db):
    """Execute a tool by name. Returns ``(result_dict, summary_str)``.

    Never raises. Unknown tools and tools that throw both return a structured
    ``{"error": ...}`` payload so the model can recover and the reply still completes
    and is saved. A failed tool's partial writes are rolled back.
    """
    fn = _TOOL_DISPATCH.get(name)
    if fn is None:
        return {"error": f"unknown tool: {name}"}, f"unknown tool: {name}"
    try:
        return fn(db, args or {})
    except Exception as exc:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        msg = f"{type(exc).__name__}: {exc}"
        print(f"[orchestrator] tool {name} failed: {msg}")
        return {"error": msg, "tool": name, "args": args or {}}, f"{name} failed — {msg}"


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
                "Get a live snapshot: KPIs, habits done/total, active and overdue todos, "
                "10K revenue progress, weakest subject and upcoming exams."
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
            "name": "surface_weakest_topics",
            "description": (
                "Return the weakest syllabus topics, ranked by status (red/yellow first), how long "
                "since they were revised, weakness notes and how close the exam is. Use for 'what "
                "should I study' questions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "How many to return. Default 5."},
                    "subject": {"type": "string", "description": "Optional subject name or code to filter by."},
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
                "Search Bandile's Obsidian vault (Apple Notes exports, journal entries, past "
                "project documents, personal archives) by meaning. Use it whenever past notes "
                "could sharpen the answer: when he asks about something he wrote, planned or "
                "researched before, when a question touches a project or person he has history "
                "with, or when he asks what he thinks about a topic. Not needed for plain logging."
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
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": (
                "Store something durable in long-term memory: a fact about Bandile or his "
                "world, a decision he has made, or a preference about how he wants things "
                "done. Use it when he states something that should still be true next week "
                "(e.g. 'my Further Maths exam is 12 May', 'I prefer evening study blocks', "
                "'decided to drop the newsletter idea'). Do not store transient chatter or "
                "things already tracked as tasks."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The memory, as one self-contained sentence."},
                    "kind": {
                        "type": "string",
                        "enum": ["fact", "decision", "preference"],
                        "description": "What kind of memory this is. Default 'fact'.",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional short tags, e.g. ['exams', 'further-maths'].",
                    },
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_delete",
            "description": (
                "Propose deleting one record. This deletes NOTHING — it registers a pending "
                "action and returns its id and a summary. You must then ask Bandile to confirm "
                "in plain words and only call confirm_action after he explicitly says yes. "
                "Use it whenever he asks to remove, delete or clear something."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "table": {
                        "type": "string",
                        "enum": ["todos", "notes", "revenue", "ai_memory", "books",
                                 "daily_bible_log", "clients", "lift_logs", "daily_logs"],
                        "description": "Which table the record lives in.",
                    },
                    "record_id": {"type": "integer", "description": "The record's id."},
                    "reason": {"type": "string", "description": "Why, in Bandile's words. Optional."},
                },
                "required": ["table", "record_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "confirm_action",
            "description": (
                "Execute a pending action (a delete) AFTER Bandile has explicitly confirmed it "
                "in this conversation ('yes', 'do it', 'confirm'). Never call it without that "
                "explicit yes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pending_id": {"type": "integer", "description": "The pending action id from propose_delete."},
                },
                "required": ["pending_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_action",
            "description": "Cancel a pending action when Bandile says no or changes his mind.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pending_id": {"type": "integer"},
                },
                "required": ["pending_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "complete_task",
            "description": (
                "Mark an existing task done. Pass the distinguishing words of the task "
                "('Business essay') or its id. One clear match is completed immediately; "
                "several plausible matches become one-tap suggestions instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Words identifying the task."},
                    "todo_id": {"type": "integer", "description": "Exact task id, if known."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notes_search",
            "description": (
                "Search Bandile's notes (books, Bible, journal, Inbox ideas, general) by meaning. "
                "Use when he asks about something he noted, read or jotted down."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "kind": {"type": "string", "enum": ["book", "bible", "journal", "inbox", "general"]},
                    "limit": {"type": "integer", "description": "Default 5."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notes_read",
            "description": "Read one note in full, by title (fuzzy) or id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "note_id": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notes_append",
            "description": (
                "Append a dated entry to a note, creating the note if it does not exist. Use for "
                "reflections, reading progress ('finished chapter 6'), journal lines and ideas. "
                "Title is the book name for kind 'book', 'Bible' for kind 'bible', 'Inbox' for ideas."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "text": {"type": "string"},
                    "kind": {"type": "string", "enum": ["book", "bible", "journal", "inbox", "general"]},
                    "date": {"type": "string", "description": "YYYY-MM-DD; defaults to today."},
                },
                "required": ["title", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notes_create",
            "description": "Create a new note with an optional body and tags.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "kind": {"type": "string", "enum": ["book", "bible", "journal", "inbox", "general"]},
                    "body": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_deadlines",
            "description": "Overdue tasks, tasks due today, tasks due within N days, and upcoming exams.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "Look-ahead window in days. Default 14."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_exam_date",
            "description": (
                "Set or correct an exam date from conversation, e.g. 'Further Maths P1 is on 12 May' → "
                "subject 'Further Maths', paper 'P1', date '12 May'. Omit paper for the whole subject."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string", "description": "Subject name or code (9709, Further Maths, Business…)."},
                    "date": {"type": "string", "description": "The date as said: '12 May', '2026-05-12'."},
                    "paper": {"type": "string", "description": "Paper such as P1, S1, P3. Optional."},
                },
                "required": ["subject", "date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_syllabus",
            "description": (
                "Update one syllabus topic from conversation. 'Revised S1 probability today, still "
                "shaky on conditional probability' → topic 'probability', paper 'S1', status 'revised', "
                "weakness_note 'still shaky on conditional probability'. last_revised is set "
                "automatically. Statuses: not_started, learned (learned but weak), revised (not yet "
                "100%), bulletproof. Topics match by code ('1.3') or title, fuzzily."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "Topic code or distinguishing title words."},
                    "status": {"type": "string", "enum": ["not_started", "learned", "revised", "bulletproof"]},
                    "weakness_note": {"type": "string", "description": "What is still weak, in Bandile's words."},
                    "subject": {"type": "string", "description": "Subject name or code, if said."},
                    "paper": {"type": "string", "description": "Paper such as P1, S1, if said."},
                    "past_paper_score": {"type": "number", "description": "Past-paper score %, if said."},
                },
                "required": ["topic"],
            },
        },
    },
]
