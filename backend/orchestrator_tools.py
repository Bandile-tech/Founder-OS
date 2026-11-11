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
    repeated = len(result.get("repeated") or [])
    summary = (
        f"{n} opportunit{'y' if n == 1 else 'ies'} surfaced "
        f"({result.get('rejected_count', 0)} rejected by verifier"
        + (f", {repeated} seen before" if repeated else "")
        + f") — project #{result.get('project_id')}"
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
    return result, f"not executed: {result.get('reason') or result.get('error') or 'unknown'}"


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
            if result.get("open_url"):
                return {"done": result["summary"], "open link": result["open_url"],
                        "next step": "Tell Bandile to tap the link to open WhatsApp and send it."}
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
                                 "leads", "outreach_messages", "contacts", "deals", "projects",
                                 "payments", "recurring_revenue", "communications",
                                 "project_updates", "applications", "application_requirements",
                                 "content_ideas"],
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


# ── 5C: clients, deals, communications ───────────────────────

def _ambiguous(kind: str, fragment: str, scored, label):
    return ({"ambiguous": True, "candidates": _candidates(scored, label)},
            f"{len(scored)} {kind}s could match '{fragment}' — ask which")


def _client_label(c) -> str:
    return f"{c.name} ({c.company})" if c.company and c.company != c.name else c.name


def crm_lookup(db, query: str, kind: str = "any"):
    """Read-only lookup of a client, contact, deal or project by name."""
    import crm
    kind = kind if kind in ("client", "contact", "deal", "project", "any") else "any"
    candidates = []
    if kind in ("client", "any"):
        client, scored = crm.find_client(db, query)
        if client is not None:
            return {"kind": "client", "client": crm.client_detail(db, client)}, f"client {client.name}"
        candidates += [{"kind": "client", "id": c.id, "name": _client_label(c)} for _s, c in scored[:3]]
    if kind in ("deal", "any"):
        deal, scored = crm.find_deal(db, query)
        if deal is not None:
            return {"kind": "deal", "deal": crm.serialize_deal(db, deal)}, f"deal {deal.title}"
        candidates += [{"kind": "deal", "id": d.id, "name": d.title} for _s, d in scored[:3]]
    if kind in ("project", "any"):
        project, scored = crm.find_project(db, query)
        if project is not None:
            return {"kind": "project", "project": crm.serialize_project(db, project)}, f"project {project.title}"
        candidates += [{"kind": "project", "id": p.id, "name": p.title} for _s, p in scored[:3]]
    if kind in ("contact", "any"):
        contact, scored = crm.find_contact(db, query)
        if contact is not None:
            client = db.query(models.Client).filter(models.Client.id == contact.client_id).first()
            return ({"kind": "contact", "contact": crm.serialize_contact(contact),
                     "client_name": client.name if client else None}, f"contact {contact.name}")
        candidates += [{"kind": "contact", "id": c.id, "name": c.name} for _s, c in scored[:3]]
    if candidates:
        return {"ambiguous": True, "candidates": candidates}, f"{len(candidates)} records could match '{query}'"
    return {"found": False, "query": query}, f"nothing in the CRM matches '{query}'"


def log_communication(db, who: str, summary: str, channel: str = "whatsapp",
                      direction: str = "out", when: str | None = None):
    """Log a conversation with a client, or with a lead when no client matches."""
    import business_common
    import change_log
    import crm
    import leads
    occurred = None
    if when:
        day = business_common.parse_day(when, past=True)
        if day is None:
            return {"error": f"could not read the date '{when}'"}, f"could not read the date '{when}'"
        if day != date.today():
            occurred = datetime(day.year, day.month, day.day, 12, 0, 0)
    client, client_scored = crm.find_client(db, who)
    lead = None
    if client is None:
        if client_scored:
            return _ambiguous("client", who, client_scored, _client_label)
        lead, lead_scored = leads.pick_lead(db, who)
        if lead is None:
            if lead_scored:
                return _ambiguous("lead", who, lead_scored, lambda l: l.name)
            return {"error": f"no client or lead matches '{who}'"}, f"no client or lead matches '{who}'"
    with change_log.batch_scope(db, "assistant"):
        try:
            result = crm.log_communication(db, client=client, lead=lead, channel=channel,
                                           direction=direction, summary=summary,
                                           occurred_at=occurred, source="conversation")
        except ValueError as exc:
            return {"error": str(exc)}, str(exc)
        db.commit()
    return result, f"logged {result['channel']} with {result['with']}"


def update_deal(db, deal: str | None = None, client: str | None = None, stage: str | None = None,
                amount: float | None = None, currency: str | None = None,
                expected_close: str | None = None, lost_reason: str | None = None,
                create: bool = False):
    """Update a deal, or create it when ``create`` is true and nothing matches."""
    import business_common
    import change_log
    import crm
    close = None
    if expected_close:
        close = business_common.parse_day(expected_close)
        if close is None:
            msg = f"could not read the date '{expected_close}'"
            return {"error": msg}, msg
    owner = None
    if client:
        owner, scored = crm.find_client(db, client)
        if owner is None:
            if scored:
                return _ambiguous("client", client, scored, _client_label)
            return {"error": f"no client matches '{client}'"}, f"no client matches '{client}'"
    found, scored = crm.find_deal(db, deal, owner) if deal else (None, [])
    if found is None and owner is not None and not deal and not create:
        open_deals = db.query(models.Deal).filter(
            models.Deal.client_id == owner.id, models.Deal.stage.notin_(("won", "lost"))).all()
        if len(open_deals) == 1:
            found = open_deals[0]
        elif open_deals:
            scored = [(1.0, d) for d in open_deals]
    with change_log.batch_scope(db, "assistant"):
        try:
            if found is not None:
                result = crm.update_deal(db, found, stage=stage, amount=amount, currency=currency,
                                         expected_close=close, lost_reason=lost_reason)
                verb = "updated"
            elif scored and not create:
                return _ambiguous("deal", deal or client or "", scored, lambda d: d.title)
            elif create:
                if owner is None:
                    msg = "a new deal needs a client that is already on file"
                    return {"error": msg}, msg
                result = crm.create_deal(db, owner, deal or f"{owner.name} deal", amount, currency,
                                         stage=stage or "lead", expected_close=close)
                verb = "created"
            else:
                msg = f"no deal matches '{deal or client or ''}'"
                return {"error": msg}, msg
        except ValueError as exc:
            return {"error": str(exc)}, str(exc)
        db.commit()
    summary = f"deal '{result['title']}' {verb} — {result['stage']}"
    if result.get("project"):
        summary += f"; project '{result['project']['title']}' created"
    return result, summary


def add_contact(db, client: str, name: str, role: str | None = None, email: str | None = None,
                phone: str | None = None, whatsapp: str | None = None, is_primary: bool = False):
    import change_log
    import crm
    owner, scored = crm.find_client(db, client)
    if owner is None:
        if scored:
            return _ambiguous("client", client, scored, _client_label)
        return {"error": f"no client matches '{client}'"}, f"no client matches '{client}'"
    with change_log.batch_scope(db, "assistant"):
        try:
            result = crm.add_contact(db, owner, name=name, role=role, email=email, phone=phone,
                                     whatsapp=whatsapp, is_primary=bool(is_primary))
        except ValueError as exc:
            return {"error": str(exc)}, str(exc)
        db.commit()
    return result, f"added contact {result['name']} at {owner.name}"


_register(
    "crm_lookup", lambda db, a: crm_lookup(db, query=a.get("query", ""), kind=a.get("kind", "any")),
    "Look up a client, contact, deal or project by name. Read-only. Returns the full record "
    "for one clear match and candidates otherwise.",
    {"query": _str("Name or part of it."),
     "kind": _str("What to look for (default any).", ["client", "contact", "deal", "project", "any"])},
    ["query"],
)

_register(
    "log_communication",
    lambda db, a: log_communication(db, who=a.get("who", ""), summary=a.get("summary", ""),
                                    channel=a.get("channel", "whatsapp"),
                                    direction=a.get("direction", "out"), when=a.get("when")),
    "Record a conversation Bandile had with a client (or a lead): a call, meeting, message. "
    "ONLY when he states that it happened.",
    {"who": _str("Client or lead name, or part of it."),
     "summary": _str("One or two lines on what was said."),
     "channel": _str("How (default whatsapp).", ["whatsapp", "email", "call", "meeting", "linkedin", "other"]),
     "direction": _str("out when Bandile reached out, in when they did (default out).", ["out", "in"]),
     "when": _str("When it happened, e.g. 'yesterday', '12 May'. Default now.")},
    ["who", "summary"],
)

_register(
    "update_deal",
    lambda db, a: update_deal(db, deal=a.get("deal"), client=a.get("client"), stage=a.get("stage"),
                              amount=a.get("amount"), currency=a.get("currency"),
                              expected_close=a.get("expected_close"), lost_reason=a.get("lost_reason"),
                              create=bool(a.get("create", False))),
    "Change a deal's stage, amount or expected close date. ONLY when Bandile states the "
    "change; never move a stage on your own assessment. Set create true only when he "
    "explicitly describes a new deal (the client must already exist).",
    {"deal": _str("Deal title, part of it, or its id."),
     "client": _str("The client the deal belongs to."),
     "stage": _str("New stage.", ["lead", "proposal", "negotiation", "won", "lost"]),
     "amount": _num("Deal amount."),
     "currency": _str("ZMW or USD (default ZMW)."),
     "expected_close": _str("Expected close date as he said it."),
     "lost_reason": _str("Why it was lost."),
     "create": {"type": "boolean", "description": "Create the deal when none matches."}},
)

_register(
    "add_contact",
    lambda db, a: add_contact(db, client=a.get("client", ""), name=a.get("name", ""), role=a.get("role"),
                              email=a.get("email"), phone=a.get("phone"), whatsapp=a.get("whatsapp"),
                              is_primary=bool(a.get("is_primary", False))),
    "Add a person at a client. ONLY when Bandile names a new contact for a client on file.",
    {"client": _str("Client name or part of it."), "name": _str("The contact's name."),
     "role": _str("Their role."), "email": _str("Email."), "phone": _str("Phone as given."),
     "whatsapp": _str("WhatsApp number if different."),
     "is_primary": {"type": "boolean", "description": "True if this is now the main contact."}},
    ["client", "name"],
)


# ── 5C: payments and the CRM summary ─────────────────────────

def record_payment(db, client: str, amount: float, currency: str = "ZMW", status: str = "paid",
                   due_date: str | None = None, title: str | None = None):
    """Record money invoiced to (due) or received from (paid) a client."""
    import business_common
    import change_log
    import crm
    owner, scored = crm.find_client(db, client)
    if owner is None:
        if scored:
            return _ambiguous("client", client, scored, _client_label)
        return {"error": f"no client matches '{client}'"}, f"no client matches '{client}'"
    status = "due" if str(status or "paid").lower() == "due" else "paid"
    due = None
    if due_date:
        due = business_common.parse_day(due_date)
        if due is None:
            msg = f"could not read the date '{due_date}'"
            return {"error": msg}, msg
    import main
    cur = main._normalise_currency(currency or "ZMW")
    with change_log.batch_scope(db, "assistant"):
        try:
            existing = crm.find_open_payment(db, owner.id, float(amount or 0), cur) if status == "paid" else None
            if existing is not None:
                # He was already invoiced for this: settle that payment, do not add a second.
                result = crm.mark_paid(db, existing)
                result["settled_existing"] = True
            else:
                result = crm.record_payment(db, owner, amount, cur, status=status, title=title, due_date=due)
        except (ValueError, TypeError) as exc:
            return {"error": str(exc)}, str(exc)
        crm.refresh_statuses(db)
        db.commit()
    label = crm.money_label(result["amount"], result["currency"])
    result["client_name"] = owner.name
    if status == "paid":
        return result, f"{label} received from {owner.name}"
    return result, f"{label} due from {owner.name}" + (f" on {due.isoformat()}" if due else "")


def crm_summary(db):
    import crm
    crm.housekeeping(db)
    result = crm.summary(db)
    p = result["payments"]
    return result, (f"{result['clients']['active']} active client(s), "
                    f"${p['overdue_usd']:,.0f} overdue, ${result['mrr']['total_usd']:,.0f} MRR")


_register(
    "record_payment",
    lambda db, a: record_payment(db, client=a.get("client", ""), amount=a.get("amount"),
                                 currency=a.get("currency", "ZMW"), status=a.get("status", "paid"),
                                 due_date=a.get("due_date"), title=a.get("title")),
    "Record money a client was invoiced (status due) or has paid (status paid, which also "
    "logs the revenue). ONLY when Bandile states an invoice or a payment for a named client.",
    {"client": _str("Client name or part of it."), "amount": _num("The amount."),
     "currency": _str("ZMW or USD (default ZMW)."),
     "status": _str("paid when the money arrived, due when it was invoiced (default paid).", ["paid", "due"]),
     "due_date": _str("When it is due, as he said it (for status due)."),
     "title": _str("What it is for, e.g. 'Deposit'.")},
    ["client", "amount"],
)

_register(
    "crm_summary", lambda db, a: crm_summary(db),
    "Clients, deals by stage, what is due and overdue, monthly recurring revenue and client "
    "health, in one picture. Read-only.",
)


def _check_payment_overdue(db, params):
    """True when any open client payment is more than ``days`` days past its due date."""
    days = int(params.get("days", 14))
    cutoff = date.today() - timedelta(days=days)
    return db.query(models.Payment).filter(
        models.Payment.status.in_(("due", "overdue")), models.Payment.due_date.isnot(None),
        models.Payment.due_date < cutoff).first() is not None


_RULE_CHECKS["payment_overdue"] = _check_payment_overdue


# ── 5B: qualification, drafts, the approval gate, follow-ups ─

def _pick_lead(db, ref):
    """(lead, error_result). Resolves an id or a name fragment; never guesses."""
    import leads
    lead, scored = leads.pick_lead(db, ref)
    if lead is not None:
        return lead, None
    if scored:
        return None, _ambiguous("lead", str(ref), scored, lambda l: f"{l.name} ({l.company})" if l.company else l.name)
    return None, ({"error": f"no lead matches '{ref}'"}, f"no lead matches '{ref}'")


def qualify_leads(db, lead_ids: list | None = None, segment: str | None = None):
    """Score leads against the ideal customer profile (at most 20 per call)."""
    import change_log
    import leads
    if lead_ids:
        ids = [int(i) for i in lead_ids]
    elif segment:
        ids = [l.id for l in db.query(models.Lead).filter(
            models.Lead.segment == leads.normalise_segment(segment), models.Lead.status == "new",
        ).order_by(models.Lead.id).limit(leads.QUALIFY_MAX).all()]
    else:
        return {"error": "give lead_ids or a segment"}, "give lead_ids or a segment"
    with change_log.batch_scope(db, "assistant"):
        try:
            results = leads.qualify(db, ids)
        except ValueError as exc:
            return {"error": str(exc)}, str(exc)
        db.commit()
    qualified = sum(1 for r in results if r.get("outcome") == "qualified")
    disqualified = sum(1 for r in results if r.get("outcome") == "disqualified")
    shaped = [{"lead_id": r["lead_id"], "name": r.get("name"), "qualification_score": r.get("score"),
               "status": r.get("status"), "fit": r.get("fit"), "reasons": r.get("reasons"),
               "disqualifiers": r.get("disqualifiers"), "suggested_angle": r.get("suggested_angle"),
               "error": r.get("error"), "skipped_llm": r.get("skipped_llm")} for r in results]
    result = {"results": shaped, "qualified": qualified, "disqualified": disqualified,
              "changes": [r["change"] for r in results if r.get("change")]}
    if results and all(r.get("skipped_llm") for r in results):
        result["note"] = "No AI key is configured, so only the basic score was computed and nothing changed."
        return result, f"scored {len(results)} lead(s) without AI — nothing changed"
    return result, f"qualified {qualified} of {len(results)} ({disqualified} disqualified)"


def draft_outreach(db, lead, channel: str, brief: str | None = None, message_id: int | None = None):
    """Draft (or revise) a message to a lead. Never sends."""
    import change_log
    import llm as _llm
    import outreach
    target, problem = _pick_lead(db, lead)
    if target is None:
        return problem
    with change_log.batch_scope(db, "assistant"):
        try:
            result = outreach.draft(db, target, channel, brief,
                                    message_id=int(message_id) if message_id else None)
        except _llm.AIUnavailable:
            msg = "drafting needs the AI key, which is not configured"
            return {"error": msg}, msg
        except ValueError as exc:
            return {"error": str(exc)}, str(exc)
        db.commit()
    out = {"message_id": result["id"], "lead": target.name, "channel": result["channel"],
           "subject": result["subject"], "body": result["body"], "version": result["version"],
           "status": "draft — not sent", "changes": result["changes"],
           "next_step": "Show Bandile the draft. It is sent only when he taps Approve & send or says to send it."}
    return out, f"drafted {result['channel']} to {target.name} (v{result['version']})"


def request_send(db, message_id: int):
    """Ask for approval to send a draft. Sends nothing: it registers a pending action."""
    import outreach
    message = db.query(models.OutreachMessage).filter(
        models.OutreachMessage.id == int(message_id or 0)).first()
    if message is None:
        return {"error": f"message {message_id} not found"}, f"message {message_id} not found"
    try:
        pending = outreach.request_send(db, message)
    except ValueError as exc:
        return {"error": str(exc)}, str(exc)
    pending["next_step"] = ("Nothing has been sent. Ask Bandile for a plain yes. Call confirm_action "
                            "only after he answers, in a later turn — never in this one.")
    return pending, f"awaiting your yes: {pending['summary']}"


def set_follow_up(db, lead, date_text: str, reason: str | None = None):
    """Create or move the follow-up task for a lead."""
    import business_common
    import change_log
    import leads
    target, problem = _pick_lead(db, lead)
    if target is None:
        return problem
    due = business_common.parse_day(date_text)
    if due is None:
        msg = f"could not read the date '{date_text}'"
        return {"error": msg}, msg
    with change_log.batch_scope(db, "assistant"):
        todo, change = leads.set_follow_up(db, target, due, reason)
        db.commit()
    return ({"lead": target.name, "todo_id": todo.id, "follow_up_on": due, "change": change},
            f"follow-up with {target.name} on {due.isoformat()}")


_register(
    "qualify_leads",
    lambda db, a: qualify_leads(db, lead_ids=a.get("lead_ids"), segment=a.get("segment")),
    "Score leads against the ideal customer profile (at most 20 per call). ONLY when Bandile "
    "asks to qualify leads or asks which leads are worth pursuing.",
    {"lead_ids": {"type": "array", "items": {"type": "integer"}, "description": "Lead ids (at most 20)."},
     "segment": _str("Or a segment: its first 20 unqualified leads.")},
)

_register(
    "draft_outreach",
    lambda db, a: draft_outreach(db, lead=a.get("lead", ""), channel=a.get("channel", "whatsapp"),
                                 brief=a.get("brief"), message_id=a.get("message_id")),
    "Draft a message to a lead, or revise a draft when given its message_id. It only ever "
    "creates a draft; it never sends.",
    {"lead": _str("Lead name, part of it, or the lead id."),
     "channel": _str("Where it will be sent.", ["whatsapp", "email", "linkedin", "call", "other"]),
     "brief": _str("What Bandile asked for, e.g. 'intro, mention the bakery site' or 'shorter'."),
     "message_id": _int("The draft to revise. Omit for a new draft.")},
    ["lead", "channel"],
)

_register(
    "request_send",
    lambda db, a: request_send(db, message_id=a.get("message_id")),
    "Register a request to send a drafted message. This sends NOTHING: it creates a pending "
    "action. Ask Bandile for a plain yes and call confirm_action only after he gives it, in "
    "a later turn.",
    {"message_id": _int("The draft's message id.")},
    ["message_id"],
)

_register(
    "set_follow_up",
    lambda db, a: set_follow_up(db, lead=a.get("lead", ""), date_text=a.get("date", ""), reason=a.get("reason")),
    "Set or move the follow-up reminder for a lead. ONLY when Bandile says when to chase them.",
    {"lead": _str("Lead name, part of it, or the lead id."),
     "date": _str("When, as he said it: 'Friday', '12 May', 'in 3 days'."),
     "reason": _str("What the follow-up is about. Optional.")},
    ["lead", "date"],
)


# ── 5B: replies, conversion, the stalled-outreach alert ──────

def log_lead_reply(db, lead, summary: str, channel: str | None = None):
    """Record that a lead answered."""
    import change_log
    import leads
    target, problem = _pick_lead(db, lead)
    if target is None:
        return problem
    with change_log.batch_scope(db, "assistant"):
        result = leads.log_reply(db, target, summary, channel)
        db.commit()
    return ({"lead": target.name, "status": result["status"], "reply": (summary or "").strip(),
             "changes": result["changes"]}, f"{target.name} replied — follow-up closed")


def convert_lead(db, lead):
    """Turn a lead into a client."""
    import change_log
    import leads
    target, problem = _pick_lead(db, lead)
    if target is None:
        return problem
    with change_log.batch_scope(db, "assistant"):
        result = leads.convert(db, target)
        db.commit()
    out = {"lead": target.name, "client_id": result["client"]["id"], "client": result["client"]["name"],
           "already_converted": bool(result.get("already_converted")), "changes": result["changes"]}
    if out["already_converted"]:
        return out, f"{target.name} is already a client"
    return out, f"{target.name} is now a client"


_register(
    "log_lead_reply",
    lambda db, a: log_lead_reply(db, lead=a.get("lead", ""), summary=a.get("summary", ""), channel=a.get("channel")),
    "Record that a lead replied. ONLY when Bandile reports the reply.",
    {"lead": _str("Lead name, part of it, or the lead id."),
     "summary": _str("What they said, in a line."),
     "channel": _str("Where they replied.", ["whatsapp", "email", "linkedin", "call", "other"])},
    ["lead", "summary"],
)

_register(
    "convert_lead",
    lambda db, a: convert_lead(db, lead=a.get("lead", "")),
    "Turn a lead into a client. ONLY on an explicit statement that they are now a client, "
    "have signed or have paid.",
    {"lead": _str("Lead name, part of it, or the lead id.")},
    ["lead"],
)


def _check_no_outreach_sent(db, params):
    """True when at least ``min_qualified`` leads are qualified and nothing has been sent
    in the last ``window_days`` days."""
    window = int(params.get("window_days", 7))
    minimum = int(params.get("min_qualified", 3))
    qualified = db.query(models.Lead).filter(models.Lead.status == "qualified").count()
    if qualified < minimum:
        return False
    since = datetime.utcnow() - timedelta(days=window)
    return db.query(models.OutreachMessage).filter(
        models.OutreachMessage.direction == "out", models.OutreachMessage.status == "sent",
        models.OutreachMessage.sent_at >= since).first() is None


_RULE_CHECKS["no_outreach_sent"] = _check_no_outreach_sent


# ── 5E: project portfolio ────────────────────────────────────

def _pick_project(db, ref):
    import crm
    project, scored = crm.find_project(db, ref)
    if project is not None:
        return project, None
    if scored:
        return None, _ambiguous("project", str(ref), scored, lambda p: p.title)
    return None, ({"error": f"no project matches '{ref}'"}, f"no project matches '{ref}'")


def portfolio_review(db, include_paused: bool = True):
    """The ranked portfolio. Read-only, except that it files pause suggestions."""
    import change_log
    import portfolio
    with change_log.batch_scope(db, "assistant"):
        result = portfolio.full_review(db, include_paused=bool(include_paused))

    def line(e):
        p = e["project"]
        return {"project_id": p["id"], "title": p["title"], "client": p["client_name"],
                "status": p["status"], "rank": e["rank"], "recommendation": e["recommendation"],
                "attention_score": e["attention"]["score"], "bottleneck": e["bottleneck"],
                "due_date": p["due_date"], "next_action": p["next_action"]}

    shaped = {
        "ranked": [line(e) for e in result["ranked"]],
        "paused": [line(e) for e in result["paused"]],
        "pause_suggestions_waiting": [s["payload"].get("title") for s in result["pause_suggestions"]],
        "totals": result["totals"],
        "note": "Recommendations are advice. Nothing is paused until Bandile taps or says so.",
    }
    focus_n = sum(1 for e in result["ranked"] if e["recommendation"] == "focus")
    return shaped, (f"{focus_n} focus, {result['totals']['blocked']} blocked, "
                    f"{len(result['pause_suggestions'])} pause suggestion(s) waiting")


def update_project(db, project=None, create: bool = False, title: str | None = None,
                   kind: str | None = None, client: str | None = None, status: str | None = None,
                   priority: int | None = None, expected_value: float | None = None,
                   currency: str | None = None, due_date: str | None = None,
                   effort_hours_week: float | None = None, next_action: str | None = None,
                   blocked_reason: str | None = None, explicit: bool = False):
    """Create or change a project. Pausing needs ``explicit`` (Bandile literally said pause)."""
    import business_common
    import change_log
    import crm
    import portfolio
    if status == "paused" and not explicit:
        msg = "pausing needs a tap or an explicit 'pause X'"
        return {"error": msg}, msg
    due = None
    if due_date:
        due = business_common.parse_day(due_date)
        if due is None:
            msg = f"could not read the date '{due_date}'"
            return {"error": msg}, msg
    owner = None
    if client:
        owner, scored = crm.find_client(db, client)
        if owner is None:
            if scored:
                return _ambiguous("client", client, scored, _client_label)
            return {"error": f"no client matches '{client}'"}, f"no client matches '{client}'"
    fields = dict(kind=kind, priority=priority, expected_value=expected_value, currency=currency,
                  due_date=due, effort_hours_week=effort_hours_week, next_action=next_action)
    with change_log.batch_scope(db, "assistant"):
        try:
            if create:
                name = (title or (project if isinstance(project, str) else "") or "").strip()
                result = portfolio.create_project(
                    db, title=name, client=owner, status=status or "active",
                    **{**fields, "kind": kind or ("client" if owner else "internal")})
                verb = "created"
            else:
                target, problem = _pick_project(db, project or "")
                if target is None:
                    return problem
                result = portfolio.update_project(db, target, title=title, client=owner, status=status,
                                                  blocked_reason=blocked_reason, **fields)
                verb = "updated"
        except ValueError as exc:
            return {"error": str(exc)}, str(exc)
        db.commit()
    return result, f"project '{result['title']}' {verb} — {result['status']}"


def log_project_update(db, project, text: str, kind: str = "note"):
    import change_log
    import portfolio
    target, problem = _pick_project(db, project)
    if target is None:
        return problem
    with change_log.batch_scope(db, "assistant"):
        try:
            result = portfolio.log_update(db, target, text, kind or "note")
        except ValueError as exc:
            return {"error": str(exc)}, str(exc)
        db.commit()
    return result, f"logged {result['kind']} on {target.title}"


_register(
    "portfolio_review",
    lambda db, a: portfolio_review(db, include_paused=bool(a.get("include_paused", True))),
    "Rank every live project by how much attention it deserves, with the bottleneck on each "
    "and which to focus on, maintain, pause or close. Use it for 'what should I work on this "
    "week' and 'what should I pause'.",
    {"include_paused": {"type": "boolean", "description": "Also list paused projects (default true)."}},
)

_register(
    "update_project",
    lambda db, a: update_project(
        db, project=a.get("project"), create=bool(a.get("create", False)), title=a.get("title"),
        kind=a.get("kind"), client=a.get("client"), status=a.get("status"), priority=a.get("priority"),
        expected_value=a.get("expected_value"), currency=a.get("currency"), due_date=a.get("due_date"),
        effort_hours_week=a.get("effort_hours_week"), next_action=a.get("next_action"),
        blocked_reason=a.get("blocked_reason"), explicit=bool(a.get("explicit", False))),
    "Change a project, or create one when create is true. ONLY when Bandile states the "
    "change or announces a new project. Status 'paused' is refused unless explicit is true, "
    "and explicit may be true only when he literally said to pause that project.",
    {"project": _str("Project title, part of it, or its id."),
     "create": {"type": "boolean", "description": "Create a new project (give title)."},
     "title": _str("Title (for a new project, or to rename)."),
     "kind": _str("client or internal.", ["client", "internal"]),
     "client": _str("Client the project is for."),
     "status": _str("New status.", ["planned", "active", "paused", "delivered", "closed", "cancelled"]),
     "priority": _int("1 (highest) to 5."),
     "expected_value": _num("What it is worth."),
     "currency": _str("ZMW or USD (default ZMW)."),
     "due_date": _str("Due date as he said it."),
     "effort_hours_week": _num("Hours a week it takes."),
     "next_action": _str("The next concrete step."),
     "blocked_reason": _str("What it is blocked on. An empty string clears the block."),
     "explicit": {"type": "boolean", "description": "True only if he literally said to pause it."}},
)

_register(
    "log_project_update",
    lambda db, a: log_project_update(db, project=a.get("project", ""), text=a.get("text", ""), kind=a.get("kind", "note")),
    "Log progress on a project: what shipped, a milestone, a blocker or that it is unblocked. "
    "ONLY when Bandile reports it.",
    {"project": _str("Project title, part of it, or its id."),
     "text": _str("What happened, in a line."),
     "kind": _str("note (default), milestone, blocker or unblocked.", ["note", "milestone", "blocker", "unblocked"])},
    ["project", "text"],
)


def _check_projects_blocked(db, params):
    """True when at least ``min`` active projects have a blocked reason."""
    minimum = int(params.get("min", 2))
    rows = db.query(models.Project).filter(models.Project.status == "active").all()
    return sum(1 for p in rows if (p.blocked_reason or "").strip()) >= minimum


_RULE_CHECKS["projects_blocked"] = _check_projects_blocked


# ── 5A: standing research objectives and dismissing findings ─

def schedule_research(db, objective: str, cadence_days: int = 14, max_opportunities: int = 3):
    """Make research on an objective recur (or change how often it does)."""
    import change_log
    from market_intel import objectives
    with change_log.batch_scope(db, "assistant"):
        try:
            result = objectives.schedule(db, objective, cadence_days, max_opportunities)
        except ValueError as exc:
            return {"error": str(exc)}, str(exc)
        db.commit()
    return result, f"research scheduled every {result['cadence_days']} days"


def dismiss_research_finding(db, finding_id: int, reason: str | None = None):
    """Mark a surfaced finding as not worth pursuing."""
    import change_log
    from market_intel.agent import dismiss_finding
    with change_log.batch_scope(db, "assistant"):
        result = dismiss_finding(db, int(finding_id or 0), reason)
        if result.get("error"):
            return result, result["error"]
        db.commit()
    out = {"finding_id": result["id"], "problem": result["problem"], "status": result["status"],
           "reason": result.get("dismissed_reason"), "change": result.get("change")}
    if result.get("already_dismissed"):
        return out, f"finding {result['id']} was already dismissed"
    return out, f"finding {result['id']} dismissed"


_register(
    "schedule_research",
    lambda db, a: schedule_research(db, objective=a.get("objective", ""),
                                    cadence_days=int(a.get("cadence_days", 14) or 14),
                                    max_opportunities=int(a.get("max_opportunities", 3) or 3)),
    "Make research on an objective recur every cadence_days. ONLY when Bandile explicitly "
    "asks for research to repeat (e.g. 'keep researching X every two weeks'). NEVER on your "
    "own initiative and never as a side effect of a one-off research run.",
    {"objective": _str("The research objective, as a full question."),
     "cadence_days": _int("How often, in days (default 14)."),
     "max_opportunities": _int("How many opportunities to surface per run (default 3).")},
    ["objective"],
)

_register(
    "dismiss_research_finding",
    lambda db, a: dismiss_research_finding(db, finding_id=a.get("finding_id"), reason=a.get("reason")),
    "Mark a surfaced research finding as not worth pursuing. ONLY when Bandile explicitly "
    "says a specific finding is not worth it (e.g. 'drop finding 12', 'that one is not for "
    "me'). NEVER on your own judgement of the finding.",
    {"finding_id": _int("The id of the finding to dismiss."),
     "reason": _str("Why, in his words. Optional.")},
    ["finding_id"],
)


# ── 6B: agents ───────────────────────────────────────────────

def _agent_spec(key: str):
    import agents
    agents.load_all()
    key = (key or "").strip().lower().replace(" ", "_").replace("-", "_")
    if key in agents.REGISTRY:
        return agents.REGISTRY[key], None
    names = {s.name.lower(): s for s in agents.REGISTRY.values()}
    if key.replace("_", " ") in names:
        return names[key.replace("_", " ")], None
    known = ", ".join(sorted(agents.REGISTRY))
    return None, ({"error": f"no agent called '{key}'", "agents": sorted(agents.REGISTRY)},
                  f"no agent called '{key}' (known: {known})")


def _agent_line(a: dict) -> dict:
    last = a.get("last_run") or {}
    return {"key": a["key"], "name": a["name"], "enabled": a["enabled"],
            "schedule": a["schedule"]["effective"], "last_run_at": last.get("at"),
            "last_status": last.get("status"), "last_summary": last.get("summary"),
            "runs": a["runs_count"], "cost_usd_this_month": a["cost_usd_month"]}


def agent_status(db, key: str | None = None):
    """What the agents are and what they last did. Read-only."""
    import agents
    from agents import api
    agents.load_all()
    if key:
        spec, problem = _agent_spec(key)
        if spec is None:
            return problem
        detail = api.serialize_agent(db, spec)
        runs = [{"started_at": r["started_at"], "status": r["status"], "summary": r["summary"],
                 "changes": r["changes_count"], "error": r["error"]} for r in api._runs(db, key=spec.key, limit=5)]
        return {**_agent_line(detail), "description": detail["description"], "recent_runs": runs}, \
            f"{spec.name}: {detail['last_run']['status'] if detail['last_run'] else 'never run'}"
    listed = [_agent_line(a) for a in api.list_agents(db)]
    last_tick = db.query(models.JobTick).order_by(models.JobTick.id.desc()).first()
    return ({"agents": listed, "last_tick_at": last_tick.started_at if last_tick else None},
            f"{len(listed)} agent(s), {sum(1 for a in listed if a['enabled'])} enabled")


def run_agent(db, key: str):
    """Run one agent now."""
    from agents import runner
    spec, problem = _agent_spec(key)
    if spec is None:
        return problem
    db.commit()                      # the agent works in its own session: let it see this turn's writes
    outcome = runner.run_agent(spec, "manual")
    result = {"agent": spec.name, "status": outcome.get("status"), "summary": outcome.get("summary"),
              "changes_made": outcome.get("changes_count"), "error": outcome.get("error")}
    return result, f"{spec.name}: {outcome.get('summary') or outcome.get('status')}"


def set_agent_schedule(db, key: str, enabled: bool | None = None, interval_minutes: int | None = None,
                       at_time: str | None = None, weekdays: list | None = None):
    """Enable or disable an agent, or change when it runs."""
    import change_log
    from agents import api, runner
    spec, problem = _agent_spec(key)
    if spec is None:
        return problem
    fields = {k: v for k, v in {"enabled": enabled, "interval_minutes": interval_minutes,
                                "at_time": at_time, "weekdays": weekdays}.items() if v is not None}
    if not fields:
        return {"error": "nothing to change"}, "nothing to change"
    with change_log.batch_scope(db, "assistant"):
        try:
            out = runner.update_agent(db, spec, **fields)
        except (ValueError, TypeError) as exc:
            return {"error": str(exc)}, str(exc)
        db.commit()
    detail = api.serialize_agent(db, spec, out["row"])
    return ({**_agent_line(detail), "change": out["change"]},
            f"{spec.name}: {'on' if detail['enabled'] else 'off'}, {detail['schedule']['effective']}")


_register(
    "agent_status", lambda db, a: agent_status(db, key=a.get("key")),
    "What the agents are, when they run and what each last did. Read-only. Use it to answer "
    "'did the research run?' or 'what did the agents do overnight?'.",
    {"key": _str("One agent's key, e.g. market_intel. Omit for all of them.")},
)

_register(
    "run_agent", lambda db, a: run_agent(db, key=a.get("key", "")),
    "Run one agent now. ONLY when Bandile explicitly asks for that agent to run. NEVER on "
    "your own initiative.",
    {"key": _str("The agent's key, e.g. portfolio_reviewer.")},
    ["key"],
)

_register(
    "set_agent_schedule",
    lambda db, a: set_agent_schedule(db, key=a.get("key", ""), enabled=a.get("enabled"),
                                     interval_minutes=a.get("interval_minutes"), at_time=a.get("at_time"),
                                     weekdays=a.get("weekdays")),
    "Enable or disable an agent, or change when it runs. ONLY on Bandile's explicit "
    "instruction. NEVER enable, disable or reschedule an agent on your own judgement.",
    {"key": _str("The agent's key."),
     "enabled": {"type": "boolean", "description": "Turn the agent on or off."},
     "interval_minutes": _int("Run every this many minutes (0 clears the override)."),
     "at_time": _str("Run once a day at this local time, HH:MM (empty clears the override)."),
     "weekdays": {"type": "array", "items": {"type": "integer"},
                  "description": "Days it may run: 0 Monday … 6 Sunday (empty list clears)."}},
    ["key"],
)


# ── 6A: applications ─────────────────────────────────────────

def _pick_application(db, ref):
    import applications
    app, scored = applications.find_application(db, ref)
    if app is not None:
        return app, None
    if scored:
        return None, _ambiguous("application", str(ref), scored, lambda a: a.name)
    return None, ({"error": f"no application matches '{ref}'"}, f"no application matches '{ref}'")


def _deadline_note(app) -> str:
    if app.deadline is None:
        return ("unknown — neither Bandile nor a pasted document has given one. Do not state a "
                "date from general knowledge; ask him for it.")
    source = {"user": "Bandile stated it", "document": "read from text he pasted"}.get(app.deadline_source, "source unknown")
    return f"{app.deadline.isoformat()} ({source})"


def add_application(db, name: str, institution: str | None = None, programme: str | None = None,
                    kind: str | None = None, deadline: str | None = None, priority: int | None = None,
                    portal_url: str | None = None):
    """Start tracking an application, or update the one the name already describes."""
    import applications
    import business_common
    import change_log
    due = None
    if deadline:
        due = business_common.parse_day(deadline)
        if due is None:
            msg = f"could not read the date '{deadline}'"
            return {"error": msg}, msg
    existing, _scored = applications.find_application(db, name or "")
    with change_log.batch_scope(db, "assistant"):
        try:
            if existing is not None:
                before = change_log.snapshot(existing)
                if institution:
                    existing.institution = institution.strip()
                if programme:
                    existing.programme = programme.strip()
                if kind in applications.KINDS:
                    existing.kind = kind
                if due is not None:
                    existing.deadline, existing.deadline_source = due, "user"
                if priority is not None:
                    existing.priority = max(1, min(3, int(priority)))
                if portal_url:
                    existing.portal_url = portal_url.strip()
                existing.updated_at = datetime.utcnow()
                change = change_log.record_update(db, existing, before, f"Updated application: {existing.name[:60]}")
                app, verb = existing, "updated"
            else:
                made = applications.create(db, name, institution=institution, programme=programme,
                                           kind=kind or "university", deadline=due,
                                           priority=priority or 2, portal_url=portal_url)
                app = db.query(models.Application).filter(models.Application.id == made["id"]).first()
                change, verb = made["change"], "added"
        except ValueError as exc:
            return {"error": str(exc)}, str(exc)
        db.commit()
    result = {"application_id": app.id, "name": app.name, "status": app.status, "deadline": app.deadline,
              "deadline_source": app.deadline_source or "unknown", "deadline_note": _deadline_note(app),
              "change": change}
    return result, f"application '{app.name}' {verb}" + (f", deadline {app.deadline.isoformat()}" if app.deadline else "")


def import_requirements(db, application, text: str):
    """Turn pasted requirements text into the application's checklist."""
    import applications
    import change_log
    import llm as _llm
    app, problem = _pick_application(db, application)
    if app is None:
        return problem
    with change_log.batch_scope(db, "assistant"):
        try:
            result = applications.import_requirements(db, app, text)
        except _llm.AIUnavailable:
            db.rollback()
            msg = "reading requirements needs the AI key, which is not configured"
            return {"error": msg}, msg
        except ValueError as exc:
            db.rollback()
            return {"error": str(exc)}, str(exc)
        db.commit()
    result["deadline_note"] = _deadline_note(app)
    summary = f"{result['created']} requirement(s) added"
    if result["updated"]:
        summary += f", {result['updated']} updated"
    if result["deadline_set"]:
        summary += f", deadline {app.deadline.strftime('%d %b %Y')} from the text"
    if result["dates_dropped"]:
        summary += f", {result['dates_dropped']} date(s) left out (not found in the text)"
    return result, summary


def update_requirement(db, application, requirement: str, status: str | None = None,
                       due_date: str | None = None, details: str | None = None, make_todo: bool = False):
    """Change one requirement of an application, optionally turning it into a task."""
    import applications
    import business_common
    import change_log
    app, problem = _pick_application(db, application)
    if app is None:
        return problem
    reqs = applications.requirements_of(db, app)
    target, scored = business_common.fuzzy_pick(reqs, lambda r: r.title, requirement or "")
    if target is None:
        if scored:
            return _ambiguous("requirement", requirement, scored, lambda r: r.title)
        msg = f"no requirement of {app.name} matches '{requirement}'"
        return {"error": msg, "requirements": [r.title for r in reqs]}, msg
    due = None
    if due_date:
        due = business_common.parse_day(due_date)
        if due is None:
            msg = f"could not read the date '{due_date}'"
            return {"error": msg}, msg
    with change_log.batch_scope(db, "assistant"):
        try:
            result = applications.update_requirement(db, target, status=status, due_date=due, details=details)
        except ValueError as exc:
            return {"error": str(exc)}, str(exc)
        changes = [result["change"]] if result.get("change") else []
        if make_todo:
            todo = applications.make_todo(db, target)
            result["todo"] = todo["todo"]
            changes += todo.get("changes") or []
        db.commit()
    result["application"] = app.name
    result["changes"] = changes
    return result, f"{app.name}: {target.title} — {target.status.replace('_', ' ')}" + (" (task added)" if make_todo else "")


def _application_view(db, app) -> dict:
    import applications
    t = applications.timeline(db, app)
    reqs = applications.requirements_of(db, app)
    return {
        "application_id": app.id, "name": app.name, "status": app.status,
        "deadline": app.deadline, "deadline_source": app.deadline_source or "unknown",
        "deadline_note": _deadline_note(app), "days_to_deadline": t["days_to_deadline"],
        "at_risk": t["at_risk"], "requirement_counts": t["requirements"],
        "missing": [r.title for r in reqs if r.status == "missing"],
        "in_progress": [r.title for r in reqs if r.status == "in_progress"],
        "next_due": t["next_due"],
        "open_gaps": [{"gap": g.gap, "severity": g.severity} for g in applications.open_gaps(db, app)],
        "gaps_reviewed_at": app.gaps_reviewed_at,
    }


def application_status(db, application: str | None = None):
    """Where an application stands (or all of them). Read-only."""
    if application:
        app, problem = _pick_application(db, application)
        if app is None:
            return problem
        view = _application_view(db, app)
        missing = len(view["missing"])
        when = f"{view['days_to_deadline']} days to the deadline" if app.deadline else "deadline unknown"
        return view, f"{app.name}: {missing} missing, {when}"
    rows = db.query(models.Application).all()
    rows.sort(key=lambda a: (a.deadline is None, a.deadline or date.max, a.id))
    return ({"applications": [_application_view(db, a) for a in rows]},
            f"{len(rows)} application(s)")


_register(
    "add_application",
    lambda db, a: add_application(db, name=a.get("name", ""), institution=a.get("institution"),
                                  programme=a.get("programme"), kind=a.get("kind"), deadline=a.get("deadline"),
                                  priority=a.get("priority"), portal_url=a.get("portal_url")),
    "Start tracking an application (a university, scholarship, competition, programme or "
    "job), or update the one the name already describes. Pass a deadline ONLY when Bandile "
    "stated it; never supply one from your own knowledge.",
    {"name": _str("What to call it, e.g. 'Wharton'."),
     "institution": _str("The institution."), "programme": _str("The programme."),
     "kind": _str("What kind of application.", ["university", "scholarship", "competition", "programme", "job", "other"]),
     "deadline": _str("The deadline exactly as Bandile said it, e.g. '1 January'."),
     "priority": _int("1 (high) to 3 (low)."), "portal_url": _str("The application portal.")},
    ["name"],
)

_register(
    "import_requirements",
    lambda db, a: import_requirements(db, application=a.get("application", ""), text=a.get("text", "")),
    "Turn requirements text into an application's checklist. ONLY when Bandile pastes or "
    "dictates the requirements; pass his text unchanged. Never write requirements or dates "
    "from your own knowledge.",
    {"application": _str("Application name, part of it, or its id."),
     "text": _str("The pasted requirements text, verbatim.")},
    ["application", "text"],
)

_register(
    "update_requirement",
    lambda db, a: update_requirement(db, application=a.get("application", ""), requirement=a.get("requirement", ""),
                                     status=a.get("status"), due_date=a.get("due_date"), details=a.get("details"),
                                     make_todo=bool(a.get("make_todo", False))),
    "Change one requirement of an application when Bandile reports progress on it, gives "
    "it a date or asks for it to become a task.",
    {"application": _str("Application name, part of it, or its id."),
     "requirement": _str("The requirement's title or part of it."),
     "status": _str("New status.", ["missing", "in_progress", "ready", "submitted", "not_required"]),
     "due_date": _str("Its due date as Bandile said it."),
     "details": _str("Details to store (word limit, prompt, who writes it)."),
     "make_todo": {"type": "boolean", "description": "Also create a dated task for it."}},
    ["application", "requirement"],
)

_register(
    "application_status",
    lambda db, a: application_status(db, application=a.get("application")),
    "Where an application stands: deadline and where it came from, what is missing, the "
    "next thing due and open gaps. Read-only. Omit application for all of them.",
    {"application": _str("Application name, part of it, or its id.")},
)


def application_gaps(db, application, refresh: bool = False):
    """Where the profile is weak relative to an application (a cached review)."""
    import applications
    import change_log
    import llm as _llm
    app, problem = _pick_application(db, application)
    if app is None:
        return problem
    with change_log.batch_scope(db, "assistant"):
        try:
            result = applications.review_gaps(db, app, refresh=bool(refresh))
        except _llm.AIUnavailable:
            db.rollback()
            msg = "the gap review needs the AI key, which is not configured"
            return {"error": msg}, msg
        except ValueError as exc:
            db.rollback()
            return {"error": str(exc)}, str(exc)
        db.commit()
    reviewed = app.gaps_reviewed_at
    shaped = {
        "application": app.name,
        "review_last_run": reviewed.strftime("%d %b %Y %H:%M UTC") if reviewed else "never",
        "from_cache": result["cached"],
        "gaps": [{"gap": g["gap"], "severity": g["severity"], "evidence": g["evidence"],
                  "suggested_action": g["suggested_action"]} for g in result["gaps"]],
        "note": "This is the system's review, not a fact about the institution. Say when it was last run.",
    }
    if result.get("stale"):
        shaped["note"] += " The requirements have changed since; the review is out of date."
    high = sum(1 for g in result["gaps"] if g["severity"] == "high")
    return shaped, f"{app.name}: {len(result['gaps'])} open gap(s), {high} high"


_register(
    "application_gaps",
    lambda db, a: application_gaps(db, application=a.get("application", ""), refresh=bool(a.get("refresh", False))),
    "Where Bandile's profile is weak relative to an application, from the system's cached "
    "review. Relay it as the system's view and say when the review was last run. Set refresh "
    "true only when he asks for a fresh review.",
    {"application": _str("Application name, part of it, or its id."),
     "refresh": {"type": "boolean", "description": "Run the review again now."}},
    ["application"],
)


def _check_application_at_risk(db, params):
    """True when an application still being prepared has its deadline inside ``days`` days
    and requirements that are not ready."""
    days = int(params.get("days", 14))
    today = date.today()
    for app in db.query(models.Application).filter(
            models.Application.status.in_(("researching", "preparing")),
            models.Application.deadline.isnot(None)).all():
        left = (app.deadline - today).days
        if left > days:
            continue
        outstanding = db.query(models.ApplicationRequirement).filter(
            models.ApplicationRequirement.application_id == app.id,
            models.ApplicationRequirement.status.in_(("missing", "in_progress"))).count()
        if outstanding:
            return True
    return False


_RULE_CHECKS["application_at_risk"] = _check_application_at_risk


# ── 5D: personal brand ───────────────────────────────────────

def brand_performance(db, platform: str | None = None, days: int = 90):
    """What the connected platforms report. Read-only; never an estimate."""
    from brand import analytics
    data = analytics.performance(db, (platform or "").strip().lower() or None, days)
    if not data["connected"]:
        msg = "No social account is connected" + (f" for {platform}" if platform else "") + ", so there are no numbers."
        return ({"connected": False, "message": msg,
                 "note": "Say that nothing is connected. Do not estimate reach, followers or engagement."},
                "no social account connected")
    row = analytics.cached_insight(db, data["platform"])
    result = {
        "connected": True, "window_days": data["days"],
        "accounts": [{"platform": a["platform"], "handle": a["handle"], "followers": a["followers"],
                      "followers_change_30_days": a["followers_delta_30d"], "last_synced_at": a["last_synced_at"],
                      "status": a["status"], "problem": a["last_error"]} for a in data["accounts"]],
        "top_posts": [{"title": t["post"]["title"], "platform": t["post"]["platform"], "format": t["post"]["kind"],
                       "views": t["views"], "impressions": t["impressions"], "engagement": t["engagement"],
                       "engagement_rate": t["engagement_rate"], "posted_at": t["posted_at"]} for t in data["top_posts"]],
        "by_format": data["by_format"], "cadence": data["cadence"],
        "posts_last_30_days": analytics.posts_last_30d(db), "target_posts_30_days": analytics.TARGET_POSTS_30D,
        "weekly_insight": row.text if row else None,
        "note": "Every number here was reported by the platform. A value that is absent was not measured: "
                "say so, never estimate it.",
    }
    return result, f"{len(data['accounts'])} account(s), {data['cadence']['posts_in_window']} post(s) in {data['days']} days"


def add_content_idea(db, title: str | None = None, angle: str | None = None, platform: str | None = None,
                     format: str | None = None, scheduled_for: str | None = None, idea_id: int | None = None):
    """Add a content idea, or update one (idea_id). A date schedules it."""
    import brand
    import business_common
    import change_log
    when = None
    if scheduled_for:
        when = business_common.parse_day(scheduled_for)
        if when is None:
            msg = f"could not read the date '{scheduled_for}'"
            return {"error": msg}, msg
    with change_log.batch_scope(db, "assistant"):
        try:
            if idea_id:
                idea = db.query(models.ContentIdea).filter(models.ContentIdea.id == int(idea_id)).first()
                if idea is None:
                    return {"error": f"idea {idea_id} not found"}, f"idea {idea_id} not found"
                result = brand.update_idea(db, idea, title=title, angle=angle, platform=platform, format=format,
                                           scheduled_for=when)
                verb = "updated"
            else:
                result = brand.create_idea(db, title or "", angle=angle, platform=platform, format=format,
                                           scheduled_for=when, source="conversation")
                verb = "added"
        except ValueError as exc:
            return {"error": str(exc)}, str(exc)
        db.commit()
    out = {"idea_id": result["id"], "title": result["title"], "status": result["status"],
           "scheduled_for": result["scheduled_for"], "platform": result["platform"], "format": result["format"],
           "change": result.get("change")}
    return out, f"idea '{result['title']}' {verb}" + (f", scheduled for {when.isoformat()}" if when else "")


def mark_posted(db, idea, url: str | None = None):
    """An idea was published."""
    import brand
    import change_log
    target, scored = brand.find_idea(db, idea)
    if target is None:
        if scored:
            return _ambiguous("idea", str(idea), scored, lambda i: i.title)
        return {"error": f"no open idea matches '{idea}'"}, f"no open idea matches '{idea}'"
    with change_log.batch_scope(db, "assistant"):
        result = brand.mark_posted(db, target, url)
        db.commit()
    out = {"idea_id": result["id"], "title": result["title"], "status": result["status"],
           "linked_to_a_stored_post": result["linked_post_id"] is not None, "changes": result["changes"]}
    return out, f"'{result['title']}' marked posted" + (" and linked to its post" if result["linked_post_id"] else "")


_register(
    "brand_performance",
    lambda db, a: brand_performance(db, platform=a.get("platform"), days=int(a.get("days", 90) or 90)),
    "What the connected social platforms report: followers, top posts, formats, posting "
    "cadence. Read-only. The only source of content numbers: when nothing is connected it "
    "says so, and you must never estimate reach, followers or engagement.",
    {"platform": _str("One platform, e.g. youtube or linkedin. Omit for all."),
     "days": _int("How far back to look (default 90).")},
)

_register(
    "add_content_idea",
    lambda db, a: add_content_idea(db, title=a.get("title"), angle=a.get("angle"), platform=a.get("platform"),
                                   format=a.get("format"), scheduled_for=a.get("scheduled_for"),
                                   idea_id=a.get("idea_id")),
    "Add a content idea, or update or reschedule one (pass idea_id). ONLY when Bandile "
    "labels something an idea for content, or asks to schedule a post.",
    {"title": _str("The idea in a line."), "angle": _str("The angle or hook."),
     "platform": _str("Where it would go, e.g. youtube, linkedin."),
     "format": _str("What it is.", ["short", "video", "post", "article", "thread"]),
     "scheduled_for": _str("The day to post it, as he said it."),
     "idea_id": _int("An existing idea to update or reschedule.")},
)

_register(
    "mark_posted",
    lambda db, a: mark_posted(db, idea=a.get("idea", ""), url=a.get("url")),
    "Mark a content idea as posted. ONLY when Bandile says he has posted it.",
    {"idea": _str("The idea's title, part of it, or its id."),
     "url": _str("The post's URL, if he gave it.")},
    ["idea"],
)


# ── 6D: reminders and notification settings ──────────────────

def _when_label(send_at, now_local) -> str:
    """'17:00', 'tomorrow 09:00' or 'Fri 14 Nov 09:00' in local time."""
    from agents import schedule
    local = schedule.to_local(send_at)
    clock = local.strftime("%H:%M")
    gap = (local.date() - now_local.date()).days
    if gap == 0:
        return clock
    if gap == 1:
        return f"tomorrow {clock}"
    return f"{local.strftime('%a')} {local.day} {local.strftime('%b')} {clock}"


def set_reminder(db, text: str, when: str):
    """Queue a reminder for a moment. Sent to Telegram by the reminder courier."""
    import business_common
    import change_log
    import notify
    from agents import schedule
    text = (text or "").strip()
    if not text:
        return {"error": "a reminder needs something to say"}, "a reminder needs something to say"
    now_local = schedule.to_local(datetime.utcnow())
    send_at = business_common.parse_when(when, now_local)
    if send_at is None:
        msg = f"could not read '{when}' as a time that is still to come"
        return {"error": msg}, msg
    label = _when_label(send_at, now_local)
    with change_log.batch_scope(db, "assistant"):
        queued = notify.queue(db, kind="reminder", title="Reminder", body=text, send_at=send_at, source="assistant")
        if queued.get("error"):
            return {"error": queued["error"]}, queued["error"]
        row = db.query(models.Notification).filter(models.Notification.id == queued["id"]).first()
        change = change_log.record_create(db, row, f"Reminder set for {label}: {text}")
        db.commit()
    paired = notify.integrations_telegram.is_paired(db)
    out = {"reminder_id": queued["id"], "text": text, "when": label, "change": change}
    if not paired:
        out["note"] = "Telegram is not paired yet, so this reminder cannot be delivered until it is."
    return out, f"reminder set for {label}"


def notification_settings(db, **fields):
    """Read the notification settings, or change the ones given."""
    import change_log
    import notify
    changes = {k: v for k, v in fields.items() if v is not None}
    with change_log.batch_scope(db, "assistant"):
        try:
            result = notify.update_settings(db, changes)
        except ValueError as exc:
            db.rollback()
            return {"error": str(exc)}, str(exc)
        db.commit()
    s = result["settings"]
    out = {"enabled": s["enabled"], "morning_time": s["morning_time"],
           "evening_time": s["evening_time"] or "off", "quiet_hours": f"{s['quiet_start']} to {s['quiet_end']}",
           "alert_min_severity": s["alert_min_severity"], "daily_cap": s["daily_cap"],
           "telegram_paired": s["paired"], "change": result["change"]}
    return out, result["summary"]


_register(
    "set_reminder",
    lambda db, a: set_reminder(db, text=a.get("text", ""), when=a.get("when", "")),
    "Set a reminder for a moment; it is sent to Bandile's Telegram then. ONLY when he asks to "
    "be reminded at a time. Something to do on a day, with no time, is a task, not a reminder.",
    {"text": _str("What to remind him of, in his words."),
     "when": _str("The moment as he said it: '17:00', 'at 5', 'in 2 hours', 'tomorrow 17:00'.")},
    ["text", "when"],
)

_register(
    "notification_settings",
    lambda db, a: notification_settings(
        db, morning_time=a.get("morning_time"), evening_time=a.get("evening_time"),
        quiet_start=a.get("quiet_start"), quiet_end=a.get("quiet_end"),
        alert_min_severity=a.get("alert_min_severity"), daily_cap=a.get("daily_cap"), enabled=a.get("enabled")),
    "Read the notification settings, or change them when Bandile says to (\"send the briefing "
    "at 5:45\", \"no messages after 9pm\"). With no arguments it only reads.",
    {"morning_time": _str("When the morning briefing goes out, HH:MM."),
     "evening_time": _str("When the evening review goes out, HH:MM, or 'off'."),
     "quiet_start": _str("Start of quiet hours, HH:MM."), "quiet_end": _str("End of quiet hours, HH:MM."),
     "alert_min_severity": _str("The least serious alert worth a message.", ["info", "warn", "critical"]),
     "daily_cap": _int("The most messages in a day."),
     "enabled": {"type": "boolean", "description": "Turn notifications on or off."}},
)
