"""
Application watch — counts down to application deadlines and keeps the gap review current.

Daily at 05:45:
  * for each application still being prepared, emits ``application.deadline_near`` when the
    deadline comes inside one of the marks (30, 14, 7, 3, 1 days by default) — once per
    application, mark and deadline, so a missed day does not skip a mark and a re-dated
    application starts its marks again;
  * re-runs the gap review for applications whose requirements changed since their last
    review (at most two model calls a run).

It only ever uses deadlines that Bandile or a pasted document supplied.
"""

import json

import agents
from agents.base import AgentResult, AgentSpec, Permissions, Schedule

DEFAULT_MARKS = [30, 14, 7, 3, 1]
MAX_REVIEWS = 2
ACTIVE = ("researching", "preparing")


def _marks(ctx) -> list[int]:
    """The countdown marks: from the notification settings when they exist, else the default."""
    import models
    marks = None
    settings_model = getattr(models, "NotificationSettings", None)
    if settings_model is not None:
        row = ctx.db.query(settings_model).first()
        if row is not None and getattr(row, "deadline_days_json", None):
            try:
                marks = [int(d) for d in json.loads(row.deadline_days_json)]
            except (ValueError, TypeError):
                marks = None
    marks = marks or ctx.config.get("marks") or DEFAULT_MARKS
    return sorted({int(m) for m in marks if int(m) >= 0}, reverse=True)


def _emitted(ctx) -> set:
    import models
    seen = set()
    for event in ctx.db.query(models.AgentEvent).filter(
            models.AgentEvent.kind == "application.deadline_near").all():
        try:
            p = json.loads(event.payload_json or "{}")
            seen.add((int(p.get("application_id")), int(p.get("mark")), str(p.get("deadline"))))
        except (ValueError, TypeError):
            continue
    return seen


def run(ctx) -> AgentResult:
    try:
        import applications
        import llm
        import models
    except ImportError:
        return AgentResult(status="skipped", summary="applications not built")
    apps = ctx.db.query(models.Application).filter(models.Application.status.in_(ACTIVE)).all()
    if not apps:
        return AgentResult(status="skipped", summary="no applications in preparation")

    marks, seen, emitted = _marks(ctx), _emitted(ctx), 0
    for app in apps:
        if app.deadline is None:
            continue
        days = (app.deadline - ctx.today).days
        if days < 0:
            continue
        due_marks = [m for m in marks if days <= m]
        if not due_marks:
            continue
        mark = min(due_marks)                       # the tightest mark the deadline is inside
        key = (app.id, mark, app.deadline.isoformat())
        if key in seen:
            continue
        timeline = applications.timeline(ctx.db, app, ctx.today)
        missing = timeline["requirements"]["missing"] + timeline["requirements"]["in_progress"]
        if ctx.emit("application.deadline_near", {"application_id": app.id, "name": app.name, "days": days,
                                                  "mark": mark, "deadline": app.deadline.isoformat(),
                                                  "missing": missing}):
            emitted += 1
            seen.add(key)
    ctx.db.commit()

    reviewed = 0
    if llm.is_configured():
        for app in apps:
            if reviewed >= MAX_REVIEWS:
                break
            if applications.review_is_fresh(app) or not applications.requirements_of(ctx.db, app):
                continue
            try:
                applications.review_gaps(ctx.db, app, refresh=True)
                ctx.db.commit()
                reviewed += 1
            except Exception as exc:                # noqa: BLE001 — one failed review must not stop the run
                ctx.db.rollback()
                ctx.log(f"gap review for {app.name} failed: {type(exc).__name__}")
    result = {"applications": len(apps), "events_emitted": emitted, "reviews": reviewed}
    if not emitted and not reviewed:
        return AgentResult(status="skipped", summary="no deadline mark reached and no review due", result=result)
    parts = []
    if emitted:
        parts.append(f"{emitted} deadline reminder(s)")
    if reviewed:
        parts.append(f"{reviewed} gap review(s) refreshed")
    return AgentResult(summary=", ".join(parts), result=result)


SPEC = agents.register(AgentSpec(
    key="application_watch", name="Application watch",
    description="Counts down to application deadlines and refreshes the gap review when an "
                "application's requirements change.",
    schedule=Schedule(at_time="05:45"), events=[],
    permissions=Permissions(tools=[], tables_write=["application_gaps", "applications"],
                            may_emit=["application.deadline_near"], max_llm_calls=MAX_REVIEWS),
    run=run, phase="6",
))
