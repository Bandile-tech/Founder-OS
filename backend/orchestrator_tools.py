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
from datetime import date, datetime, timedelta

from sqlalchemy import func

import models

_RULES_PATH = os.path.join(
    os.path.dirname(__file__), "..", "context", "core", "off_track_rules.json"
)

# ════════════════════════════════════════════════════════════════
# Tool 1 — route_brain_dump
# ════════════════════════════════════════════════════════════════

def route_brain_dump(db, text: str):
    """Route a logging statement through the existing parse pipeline.

    This is the ONLY tool that writes to the system, and it does so exclusively via
    ``main.apply_parse_updates`` — the same audited path used by /parse and /input.
    """
    import main
    import assistant_state as st

    context = {
        "today": str(date.today()),
        "non_negotiables": [(n.key, n.label) for n in st.active_non_negotiables(db)],
    }
    parsed = main.get_parse_response(text, context)
    updates = main.apply_parse_updates(db, parsed, date.today())

    summary = parsed.get("summary") or "Entry logged."
    result = {"summary": summary}
    changed = [c.get("summary") for c in updates.get("changes", []) if c.get("summary")]
    result["changed"] = changed or ["Nothing was written."]
    if updates.get("ideas_inboxed"):
        result["ideas sent to the Inbox note"] = updates["ideas_inboxed"]
    if updates.get("suggestions"):
        n = len(updates["suggestions"])
        result["waiting for one tap"] = f"{n} low-confidence item{'s' if n != 1 else ''} saved as suggestions"
    if parsed.get("advisory"):
        result["advisory"] = parsed["advisory"]
    return result, summary


# ════════════════════════════════════════════════════════════════
# Tool 2 — get_dashboard_state
# ════════════════════════════════════════════════════════════════

def get_dashboard_state(db):
    """Live snapshot in plain language, from the current sources only: syllabus progress,
    today's non-negotiables, annual targets, revenue in Kwacha, tasks, exams, the weakest
    topic and any off-track alerts."""
    import syllabus
    import assistant_state as st

    today = date.today()
    open_todos = db.query(models.Todo).filter(models.Todo.done == False).all()  # noqa: E712
    overdue = sum(1 for t in open_todos if t.due_date and t.due_date < today)
    due_today = sum(1 for t in open_todos if t.due_date == today)

    academics = st.academics_lines(db, today) or ["No syllabus imported yet."]
    weakest = syllabus.weakest_topics(db, limit=1, today=today)
    if weakest:
        academics.append("Weakest topic right now: " + st.topic_line(weakest[0]))

    targets = [st.target_line(t) for t in st.targets(db)] or ["No annual targets set."]

    tasks = f"{st.plural(len(open_todos), 'open task')}: {overdue} overdue, {due_today} due today."

    exams = [e for e in syllabus.upcoming_exams(db, today) if e["days_away"] >= 0][:4]
    exam_lines = [f"{e['subject']}{' ' + e['paper'] if e['paper'] else ''} on "
                  f"{st.day_label(date.fromisoformat(e['exam_date']), today)} "
                  f"({st.plural(e['days_away'], 'day')} away)" for e in exams] or ["No exam dates set."]

    alerts, _ = detect_off_track(db)
    alert_lines = [a["message"] for a in alerts] or ["No alerts."]

    state = {
        "as of": today.isoformat(),
        "academics": academics,
        "non-negotiables today": st.routine_text(db, today),
        "annual targets": targets,
        "revenue": st.revenue_text(db, today),
        "tasks": tasks,
        "upcoming exams": exam_lines,
        "alerts": alert_lines,
    }
    r = st.routine_day(db, today)
    summary = (f"non-negotiables {r['done']} of {r['total']}, {st.plural(len(open_todos), 'open task')} "
               f"({overdue} overdue), revenue this year {st.fmt_zmw(st.revenue(db, today)['year_zmw'])}")
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
    import assistant_state as st
    threshold = float(params.get("threshold_pct", 60))
    window = int(params.get("window_days", 7))
    today = date.today()
    floor = today - timedelta(days=window - 1)
    c = st.routine_completion(db, floor, today, only_days_with_data=True)
    if not c["has_data"] or not c["possible"]:
        return False   # nothing logged in the window at all — no signal rather than a false alarm
    return (c["done"] / c["possible"]) * 100 < threshold


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
                "check": rule["check"],
                "severity": rule.get("severity", "info"),
                "message": rule.get("message", rule["id"]),
            })
    summary = f"{len(alerts)} alert(s)" if alerts else "no alerts"
    return alerts, summary


def off_track_report(db):
    """The detect_off_track tool: the same rules, as plain sentences."""
    alerts, summary = detect_off_track(db)
    words = {"critical": "Critical", "warn": "Warning", "info": "Note"}
    lines = [f"{words.get(a['severity'], 'Note')}: {a['message']}" for a in alerts]
    return {"alerts": lines or ["Nothing is off track."]}, summary


# ════════════════════════════════════════════════════════════════
# Tool 4 — synthesize_weekly_review
# ════════════════════════════════════════════════════════════════

def synthesize_weekly_review(db, week_start: str | None = None):
    """Four-section weekly review computed from live DB data, in plain language:
    SHIPPED (tasks completed, syllabus topics revised, non-negotiable completion),
    REVENUE (in Kwacha), NEGOTIATED (clients touched), NEXT."""
    import syllabus
    import assistant_state as st

    if week_start:
        start = date.fromisoformat(week_start)
    else:
        today = date.today()
        start = today - timedelta(days=today.weekday())   # Monday of current week
    end = start + timedelta(days=6)
    start_dt = datetime_min(start)
    end_dt = datetime_max(end)
    last_day = min(end, date.today()) if start <= date.today() else end

    # SHIPPED — tasks completed, syllabus topics revised, non-negotiables kept this week
    shipped = [f"Completed: {t.text}" for t in db.query(models.Todo).filter(
        models.Todo.done == True, models.Todo.completed_at >= start, models.Todo.completed_at <= end,  # noqa: E712
    ).order_by(models.Todo.completed_at.asc()).all()]
    for p in db.query(models.SyllabusPoint).filter(
            models.SyllabusPoint.last_revised >= start, models.SyllabusPoint.last_revised <= end).all():
        shipped.append(f"Syllabus: {p.subject_name}{' ' + p.paper if p.paper else ''} {p.topic_code} "
                       f"{p.title} is now {st.STATUS_WORDS.get(p.status, p.status)}")
    routine = st.routine_completion(db, start, last_day)
    if routine["possible"]:
        shipped.append(f"Non-negotiables: {routine['done']} of {routine['possible']} kept "
                       f"({routine['pct']}%) over {st.plural(routine['days'], 'day')}")

    # REVENUE — revenue rows dated within the week, totalled in Kwacha
    rev_rows = db.query(models.Revenue).filter(
        models.Revenue.date >= start, models.Revenue.date <= end
    ).order_by(models.Revenue.date.asc()).all()
    revenue_total = round(sum(st.to_zmw(r.amount, r.currency) for r in rev_rows), 2)
    revenue = [f"{st.fmt_amount(r.amount, r.currency)}{' from ' + r.source if r.source else ''} "
               f"on {st.day_label(r.date)}" for r in rev_rows]
    revenue.append(f"Total this week: {st.fmt_zmw(revenue_total)}"
                   + (f" (dollars converted at K{st.fmt_number(st.zmw_per_usd())} per $1)"
                      if any((r.currency or 'USD').upper() == 'USD' for r in rev_rows) else ""))

    # NEGOTIATED — clients created or updated this week
    clients = db.query(models.Client).filter(
        models.Client.updated_at >= start_dt, models.Client.updated_at <= end_dt
    ).all()
    negotiated = [f"{c.name}: {c.status}" + (f", worth {st.fmt_number(c.value)}" if c.value else "")
                  for c in clients] or ["No client activity."]

    # NEXT — top active todos + weakest syllabus topic
    next_items = [t.text for t in db.query(models.Todo).filter(
        models.Todo.done == False  # noqa: E712
    ).order_by(models.Todo.priority.asc()).limit(5).all()]
    weakest = syllabus.weakest_topics(db, limit=1)
    if weakest:
        next_items.append("Weakest topic: " + st.topic_line(weakest[0]))

    result = {
        "week": f"{st.day_label(start)} to {st.day_label(end)}",
        "SHIPPED": shipped or ["Nothing recorded as shipped."],
        "REVENUE": revenue,
        "NEGOTIATED": negotiated,
        "NEXT": next_items or ["No open tasks."],
    }
    summary = (
        f"week {start}: {len(shipped)} shipped, {st.fmt_zmw(revenue_total)} revenue, "
        f"{len(clients)} negotiated, {len(next_items)} next"
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
    import assistant_state as st
    rows = syllabus.weakest_topics(db, limit=10_000 if subject else limit)
    if subject:
        code = syllabus.resolve_subject_code(db, subject)
        rows = [r for r in rows if r["subject_code"] == code][:limit]
    result = {"topics": [st.topic_line(r) for r in rows] or ["No syllabus topics imported yet."]}
    if rows:
        top = rows[0]
        summary = (f"weakest: {top['subject_name']} {top['paper'] or ''} {top['topic_code']} {top['title']} "
                   f"({top['reason']})").replace("  ", " ")
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

    import change_log
    with change_log.batch_scope(db, "assistant"):
        before = change_log.snapshot(finding)
        entry = models.OpportunityPipeline(finding_id=finding.id, stage="discovered", notes=notes)
        finding.status = "promoted"
        finding.reviewed_at = datetime.utcnow()
        db.add(entry)
        changes = [
            change_log.record_update(db, finding, before, f"Promoted: {finding.problem[:70]}"),
            change_log.record_create(db, entry, f"Added to the pipeline: {finding.problem[:60]}"),
        ]
        db.commit()
    db.refresh(entry)

    summary = f"finding {finding.id} promoted to pipeline (discovered)"
    result = {
        "finding_id": finding.id,
        "pipeline_id": entry.id,
        "stage": entry.stage,
        "problem": finding.problem,
        "already_promoted": False,
        "changes": [c for c in changes if c],
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


def deadlines_report(db, days: int = 14):
    """The get_deadlines tool: the same data as sentences."""
    import assistant_state as st
    data, summary = get_deadlines(db, days)

    def line(t):
        due = date.fromisoformat(t["due"]) if t["due"] else None
        when = ""
        if due is not None:
            when = (f" — {abs(t['days'])} day{'s' if abs(t['days']) != 1 else ''} overdue" if t["days"] < 0
                    else f" — due {st.day_label(due)}")
        return f"{t['text']}{when} (task {t['id']}, priority {t['priority']})"

    exams = [f"{e['subject']}{' ' + e['paper'] if e['paper'] else ''} exam on "
             f"{st.day_label(date.fromisoformat(e['exam_date']))} ({st.plural(e['days_away'], 'day')} away)"
             for e in data["exams"]]
    result = {
        "window": f"the next {st.plural(data['window_days'], 'day')}",
        "overdue": [line(t) for t in data["overdue"]] or ["Nothing overdue."],
        "due today": [line(t) for t in data["due_today"]] or ["Nothing due today."],
        "coming up": [line(t) for t in data["upcoming"]] or ["Nothing else due in the window."],
        "exams": exams or ["No exams in the window."],
    }
    if data["undated_count"]:
        result["undated"] = f"{st.plural(data['undated_count'], 'open task')} without a due date"
    return result, summary


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
# Tool 23 — show_view  (UI directive: the orb opens a view on the stage)
# ═══════════════════════════════════════════════════════════════

# Fixed whitelist. The model only ever sends a view name and parameters — never HTML.
SHOW_VIEWS = {
    "syllabus":  {"subject", "paper"},
    "brief":     set(),
    "deadlines": {"days"},
    "note":      {"title"},
    "changes":   {"limit"},
    "progress":  set(),
    "history":   {"q"},
    "close":     set(),
}
_VIEW_ALIASES = {"home": "close", "todays_brief": "brief", "today": "brief", "tasks": "brief",
                 "notes": "note", "recent_changes": "changes", "undo": "changes", "exams": "deadlines",
                 "conversations": "history", "conversation": "history", "chat_history": "history",
                 "past_conversations": "history", "chats": "history"}


def show_view(db, view: str, params: dict | None = None):
    """Validate a view request and return a UI directive for the frontend stage."""
    import syllabus as syl
    import notes as notes_mod

    import re as _re
    name = _re.sub(r"[^a-z0-9]+", "_", (view or "").strip().lower().replace("'", "")).strip("_")
    name = _VIEW_ALIASES.get(name, name)
    if name not in SHOW_VIEWS:
        return ({"error": f"unknown view '{view}'. Allowed: {', '.join(sorted(SHOW_VIEWS))}"},
                f"unknown view '{view}'")
    raw = params or {}
    allowed = SHOW_VIEWS[name]
    clean: dict = {}
    for k, v in raw.items():
        if k in allowed and v not in (None, ""):
            clean[k] = v if isinstance(v, (int, float, bool)) else str(v)[:120]

    label = name
    if name == "syllabus":
        if clean.get("subject"):
            code = syl.resolve_subject_code(db, clean["subject"])
            if code is None:
                return ({"error": f"unknown subject '{clean['subject']}'"}, f"unknown subject '{clean['subject']}'")
            clean["subject"] = code
            clean["subject_name"] = syl.subject_name(db, code)
        if clean.get("paper"):
            clean["paper"] = syl.normalise_paper(clean["paper"])
        label = "syllabus" + (f" {clean['subject_name']}" if clean.get("subject_name") else "") + (f" {clean['paper']}" if clean.get("paper") else "")
    elif name == "note":
        title = clean.get("title", "")
        note = notes_mod.find_note_fuzzy(db, title) if title else None
        if note is None:
            hits = notes_mod.search_notes(db, title, limit=1) if title else []
            if hits:
                note = db.query(models.Note).filter(models.Note.id == hits[0]["id"]).first()
        if note is None:
            return ({"error": f"no note matches '{title}'"}, f"no note matches '{title}'")
        clean = {"note_id": note.id, "title": note.title}
        label = f"note '{note.title}'"
    elif name == "deadlines":
        try:
            clean["days"] = max(1, min(90, int(clean.get("days", 14))))
        except (TypeError, ValueError):
            clean["days"] = 14
    elif name == "changes":
        try:
            clean["limit"] = max(1, min(100, int(clean.get("limit", 30))))
        except (TypeError, ValueError):
            clean["limit"] = 30
    elif name == "history":
        label = "past conversations" + (f" matching '{clean['q']}'" if clean.get("q") else "")

    directive = {"view": name, "params": clean}
    result = {"ui_directive": directive, "view": name, "params": clean,
              "shown": name != "close", "message": ("Closed the stage" if name == "close" else f"Showing {label}")}
    return result, ("closing the stage" if name == "close" else f"showing {label}")


# ═══════════════════════════════════════════════════════════════

# Dispatch + JSON tool schemas for the model
# ════════════════════════════════════════════════════════════════

_TOOL_DISPATCH = {
    "route_brain_dump": lambda db, a: route_brain_dump(db, a.get("text", "")),
    "get_dashboard_state": lambda db, a: get_dashboard_state(db),
    "detect_off_track": lambda db, a: off_track_report(db),
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
    "get_deadlines": lambda db, a: deadlines_report(db, int(a.get("days", 14))),
    "show_view": lambda db, a: show_view(db, view=a.get("view", ""), params=a.get("params") or {k: v for k, v in a.items() if k != "view"}),
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


# ════════════════════════════════════════════════════════════════
# What the model sees — plain language, never raw booleans or codes
# ════════════════════════════════════════════════════════════════

_HIDDEN_KEYS = {"ui_directive", "change", "changes", "batch_id", "colour", "score", "created_at",
                "updated_at", "expires_at", "requires_confirmation", "kind_code"}


def _words(value):
    import assistant_state as st
    if isinstance(value, str):
        if value in st.STATUS_WORDS:
            return st.STATUS_WORDS[value]
        if "_" in value and value in st.TARGET_STATUS_WORDS:
            return st.TARGET_STATUS_WORDS[value]
    return value


def _plain(value):
    """Recursively turn a tool result into plain language: yes/no for booleans, words for
    status codes, spaces for underscores in keys, no empty values, no internal plumbing."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in _HIDDEN_KEYS or v is None or v == [] or v == {}:
                continue
            out[str(k).replace("_", " ")] = _plain(v)
        return out
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (date,)):
        return value.isoformat()
    return _words(value)


def _topic_label(r: dict) -> str:
    return (f"{r.get('subject_name') or r.get('subject') or ''} {r.get('paper') or ''} "
            f"{r.get('topic_code') or ''} {r.get('title') or ''}").replace("  ", " ").strip()


def _readable(name: str, result):
    """Per-tool phrasing for results whose internal shape is structured."""
    import assistant_state as st
    if not isinstance(result, dict) or "error" in result:
        return result
    if name == "update_syllabus":
        if result.get("matched"):
            text = f"{_topic_label(result)} is now {st.STATUS_WORDS.get(result['status'], result['status'])}."
            if result.get("last_revised") == date.today().isoformat():
                text += " Marked as revised today."
            return {"updated": text}
        if result.get("ambiguous"):
            return {"not updated": f"Several topics could match '{result.get('topic')}'.",
                    "candidates": [f"{_topic_label(c)} (topic id {c['id']})" for c in result["candidates"]],
                    "next step": "Ask Bandile which one he means."}
        return {"not updated": f"No syllabus topic matches '{result.get('topic')}'."}
    if name == "complete_task":
        if result.get("completed") is True:
            return {"completed": f"{result['text']} (task {result['id']})"}
        if result.get("already_done"):
            return {"no change": f"'{result['text']}' was already done."}
        if result.get("ambiguous"):
            return {"not completed": "Several open tasks could match.",
                    "candidates": [f"{c['text']} (task {c['id']})" for c in result["candidates"]],
                    "next step": "Each is now a one-tap suggestion; tell Bandile they are waiting."}
        return {"not completed": "No open task matches."}
    if name == "propose_delete":
        return {"pending action id": result["id"], "would delete": result["summary"],
                "next step": "Ask Bandile to confirm in plain words. Call confirm_action only after he says yes."}
    if name == "confirm_action":
        if result.get("executed"):
            return {"done": result["summary"]}
        return {"not done": f"{result['summary']} — {result.get('reason', 'not executed')}"}
    if name == "cancel_action":
        return {"cancelled": result["summary"]}
    if name == "show_view":
        return {"on screen": result.get("message")}
    if name == "add_todos":
        return {"added": [f"{t['text']} (task {t['id']})" for t in result.get("todos", [])]}
    if name == "set_exam_date":
        d = date.fromisoformat(result["exam_date"])
        return {"set": f"{result['subject']}{' ' + result['paper'] if result.get('paper') else ''} exam on "
                       f"{st.day_label(d)} ({st.plural(result['days_away'], 'day')} away)."}
    return result


def for_model(name: str, result) -> str:
    """The JSON string a tool result becomes in the model's context (typed and voice)."""
    try:
        shaped = _plain(_readable(name, result))
    except Exception:  # noqa: BLE001 — phrasing must never lose a result
        shaped = _plain(result)
    return json.dumps(shaped, default=str, ensure_ascii=False)


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
                "Get what is true right now, in plain language: syllabus progress per subject "
                "(from the syllabus tracker), today's non-negotiables by name, annual targets "
                "with pace, revenue in Kwacha, open and overdue tasks, upcoming exams, the "
                "weakest topic and any off-track alerts. Call it for status, 'what do you know' "
                "and 'how am I doing' questions; never quote numbers from memory instead."
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
            "description": "Produce a weekly review with SHIPPED (tasks completed, syllabus topics revised, non-negotiables kept), REVENUE (in Kwacha), NEGOTIATED and NEXT sections.",
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
                                 "daily_bible_log", "clients", "lift_logs", "daily_logs",
                                 "leads", "outreach_messages"],
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
    {
        "type": "function",
        "function": {
            "name": "show_view",
            "description": (
                "Open a view on the screen next to you (the orb steps aside and the view "
                "renders full-screen while the conversation continues). Use it whenever Bandile "
                "asks to see, show, open or look at something. Views: 'syllabus' (optional "
                "subject and paper, e.g. 'Show me my S1 topics' → view syllabus, paper S1), "
                "'brief' (today's brief), 'deadlines' (optional days), 'note' (by title), "
                "'changes' (recent changes with undo), 'progress', 'history' (past conversations, "
                "optional search q). Use view 'close' when he says "
                "close, go back or home. Send only the view name and parameters — never HTML."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "view": {"type": "string", "enum": ["syllabus", "brief", "deadlines", "note", "changes", "progress", "history", "close"]},
                    "params": {
                        "type": "object",
                        "properties": {
                            "subject": {"type": "string", "description": "Subject name or code (syllabus)."},
                            "paper": {"type": "string", "description": "Paper such as S1, P1 (syllabus)."},
                            "days": {"type": "integer", "description": "Look-ahead days (deadlines)."},
                            "title": {"type": "string", "description": "Note title (note)."},
                            "limit": {"type": "integer", "description": "How many changes (changes)."},
                            "q": {"type": "string", "description": "Search words (history)."},
                        },
                    },
                },
                "required": ["view"],
            },
        },
    },
]


# ════════════════════════════════════════════════════════════════
# Phase 5 and 6 tools
#
# Registered here, after the rebuild's tools, so the earlier schema list stays
# byte-stable. Each tool is a thin wrapper over its module's primitives.
# ════════════════════════════════════════════════════════════════

def _register(name: str, fn, description: str, properties: dict | None = None,
              required: list | None = None) -> None:
    """Add a tool to the dispatch table and the schema list in one place."""
    _TOOL_DISPATCH[name] = fn
    parameters = {"type": "object", "properties": properties or {}}
    if required:
        parameters["required"] = required
    TOOL_SCHEMAS.append({"type": "function", "function": {
        "name": name, "description": description, "parameters": parameters}})


def _str(description: str, enum: list | None = None) -> dict:
    out = {"type": "string", "description": description}
    if enum:
        out["enum"] = enum
    return out


def _int(description: str) -> dict:
    return {"type": "integer", "description": description}


def _num(description: str) -> dict:
    return {"type": "number", "description": description}


def _candidates(scored, label) -> list:
    return [{"id": row.id, "name": label(row)} for _score, row in scored[:3]]


# ── 5B: prospecting ──────────────────────────────────────────

_LEAD_FIELDS = ("name", "company", "role", "email", "phone", "whatsapp", "linkedin_url",
                "website", "location", "segment", "notes")


def _lead_brief(lead) -> dict:
    return {"id": lead.id, "name": lead.name, "company": lead.company, "segment": lead.segment,
            "status": lead.status, "qualification_score": lead.qualification_score,
            "email": lead.email, "whatsapp": lead.whatsapp or lead.phone,
            "location": lead.location, "next_follow_up": lead.next_follow_up_at}


def add_lead(db, **fields):
    """Create a lead, or update the one the details already describe."""
    import change_log
    import leads
    with change_log.batch_scope(db, "assistant"):
        try:
            lead, deduped, change = leads.create_lead(
                db, {k: v for k, v in fields.items() if k in _LEAD_FIELDS}, source="conversation")
        except ValueError as exc:
            return {"error": str(exc)}, str(exc)
        db.commit()
    result = {**_lead_brief(lead), "deduped": deduped, "change": change}
    return result, (f"updated lead {lead.name}" if deduped else f"added lead {lead.name}")


def find_leads(db, query: str | None = None, segment: str | None = None,
               status: str | None = None, limit: int = 10):
    """Read-only list of leads for the model."""
    import leads
    rows = leads.find_leads(db, query=query, segment=segment, status=status, limit=limit)
    return ({"leads": [_lead_brief(l) for l in rows], "count": len(rows)},
            f"{len(rows)} lead(s) found")


_register(
    "add_lead", lambda db, a: add_lead(db, **{k: a.get(k) for k in _LEAD_FIELDS if a.get(k)}),
    "Add a prospect to the lead database, or update the lead the details already describe. "
    "ONLY when Bandile names a person or business he intends to approach.",
    {
        "name": _str("The person's or business's name."),
        "company": _str("Business name, if the lead is a person."),
        "role": _str("Their role, e.g. owner."),
        "email": _str("Email address."),
        "phone": _str("Phone number as given."),
        "whatsapp": _str("WhatsApp number if different from the phone."),
        "linkedin_url": _str("LinkedIn profile URL."),
        "website": _str("Website."),
        "location": _str("Town or area."),
        "segment": _str("Free-text segment, e.g. 'lusaka pharmacies'."),
        "notes": _str("Anything else Bandile said about them."),
    },
    ["name"],
)

_register(
    "find_leads",
    lambda db, a: find_leads(db, query=a.get("query"), segment=a.get("segment"),
                             status=a.get("status"), limit=int(a.get("limit", 10))),
    "List leads from the prospect database. Read-only.",
    {
        "query": _str("Part of a name or company."),
        "segment": _str("Segment to filter by."),
        "status": _str("Lead status.", ["new", "qualified", "disqualified", "contacted",
                                         "replied", "converted", "lost"]),
        "limit": _int("How many to return (default 10)."),
    },
)
