"""
Briefing courier — sends the morning briefing (and the optional evening review) to Telegram.

Runs on every tick. The times live in the notification settings, not in the schedule: once
the local clock passes ``morning_time`` it queues today's briefing under the key
``briefing:<date>``, so every later run that day is a no-op. Does nothing until Telegram
is paired.
"""

import agents
from agents import schedule
from agents.base import AgentResult, AgentSpec, Permissions, Schedule


def _due(now_local, time_text) -> bool:
    at = schedule.parse_time(time_text)
    return at is not None and (now_local.hour, now_local.minute) >= at


def run(ctx) -> AgentResult:
    try:
        import integrations_telegram
        import models
        import notify
    except ImportError:
        return AgentResult(status="skipped", summary="notifications not built")
    settings = notify.settings(ctx.db)
    if not settings.enabled:
        return AgentResult(status="skipped", summary="notifications are off")
    if not integrations_telegram.is_paired(ctx.db):
        return AgentResult(status="skipped", summary="Telegram is not paired")

    def exists(key: str) -> bool:
        return ctx.db.query(models.Notification).filter(models.Notification.dedupe_key == key).first() is not None

    queued = []
    day = ctx.today.isoformat()
    if _due(ctx.now_local, settings.morning_time) and not exists(f"briefing:{day}"):
        ctx.notify(kind="briefing", title="Morning briefing", body=notify.briefing_text(ctx.db, ctx.now),
                   dedupe_key=f"briefing:{day}")
        queued.append("briefing")
    if settings.evening_time and _due(ctx.now_local, settings.evening_time) and not exists(f"evening:{day}"):
        import briefing
        data = briefing.build_briefing(ctx.db, now=ctx.now_local.replace(tzinfo=None), with_summary=False)
        ctx.notify(kind="evening", title="Evening review", body=notify.render_evening(data),
                   dedupe_key=f"evening:{day}")
        queued.append("evening review")
    ctx.db.commit()
    outcome = notify.dispatch(ctx.db, ctx.now)
    if not queued and not any(outcome.values()):
        return AgentResult(status="skipped", summary="nothing to send", result=outcome)
    parts = [f"{' and '.join(queued)} queued"] if queued else []
    parts += [f"{count} {label}" for label, count in outcome.items() if count]
    return AgentResult(summary=", ".join(parts), result={"queued": queued, **outcome})


SPEC = agents.register(AgentSpec(
    key="briefing_courier", name="Briefing courier",
    description="Sends the morning briefing to Telegram once the time set in Settings has passed, "
                "and the evening review when one is set.",
    schedule=Schedule(interval_minutes=15), events=[],
    permissions=Permissions(tools=[], tables_write=[], may_notify=True, max_llm_calls=2),
    run=run, phase="6",
))
