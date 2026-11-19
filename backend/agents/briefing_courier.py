"""
Briefing courier — sends the morning briefing and the evening check.

Runs on every tick. The times live in the notification settings, not in the schedule: once
the local clock passes ``morning_time`` it queues today's briefing under the key
``briefing:<date>``, so every later run that day is a no-op; the evening check works the
same way under ``evening:<date>``. The message is written for the channel it leaves by: a
short notification for Web Push (the greeting line and the top three items; what is still
open and what carried over), the long text for Telegram. Does nothing while no channel can
reach Bandile.

The only model call is the day's one cached briefing summary, which the morning message
reuses. The evening check makes none.
"""

import agents
from agents import schedule
from agents.base import AgentResult, AgentSpec, Permissions, Schedule


def _due(now_local, time_text) -> bool:
    at = schedule.parse_time(time_text)
    return at is not None and (now_local.hour, now_local.minute) >= at


def run(ctx) -> AgentResult:
    try:
        import models
        import notify
    except ImportError:
        return AgentResult(status="skipped", summary="notifications not built")
    settings = notify.settings(ctx.db)
    if not settings.enabled:
        return AgentResult(status="skipped", summary="notifications are off")
    channel = notify.active_channel(ctx.db, settings)
    if channel is None:
        return AgentResult(status="skipped", summary="no device has notifications on, and Telegram is not paired")

    def exists(key: str) -> bool:
        return ctx.db.query(models.Notification).filter(models.Notification.dedupe_key == key).first() is not None

    queued = []
    day = ctx.today.isoformat()
    if _due(ctx.now_local, settings.morning_time) and not exists(f"briefing:{day}"):
        title, body = notify.briefing_message(ctx.db, ctx.now, channel)
        ctx.notify(kind="briefing", title=title, body=body, dedupe_key=f"briefing:{day}", target="home")
        queued.append("briefing")
    if settings.evening_time and _due(ctx.now_local, settings.evening_time) and not exists(f"evening:{day}"):
        title, body = notify.evening_message(ctx.db, ctx.now, channel)
        ctx.notify(kind="evening", title=title, body=body, dedupe_key=f"evening:{day}", target="home")
        queued.append("evening check")
    ctx.db.commit()
    outcome = notify.dispatch(ctx.db, ctx.now)
    if not queued and not any(outcome.values()):
        return AgentResult(status="skipped", summary="nothing to send", result=outcome)
    parts = [f"{' and '.join(queued)} queued"] if queued else []
    parts += [f"{count} {label}" for label, count in outcome.items() if count]
    return AgentResult(summary=", ".join(parts), result={"queued": queued, **outcome})


SPEC = agents.register(AgentSpec(
    key="briefing_courier", name="Briefing courier",
    description="Sends the morning briefing once the time set in Settings has passed, and the evening "
                "check when one is set, to whichever channel can reach Bandile.",
    schedule=Schedule(interval_minutes=15), events=[],
    permissions=Permissions(tools=[], tables_write=[], may_notify=True, max_llm_calls=2),
    run=run, phase="6",
))
