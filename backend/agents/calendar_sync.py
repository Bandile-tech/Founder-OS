"""
Calendar sync — refreshes the cache of Google Calendar every half hour (no model calls).

Does nothing until the calendar is connected. The sync itself emits ``calendar.changed``
when anything changed, so the briefing and the couriers see the new day.
"""

import agents
from agents.base import AgentResult, AgentSpec, Permissions, Schedule


def run(ctx) -> AgentResult:
    try:
        import integrations_google
    except ImportError:
        return AgentResult(status="skipped", summary="calendar not built")
    if not integrations_google.connected(ctx.db):
        return AgentResult(status="skipped", summary="calendar not connected")
    counts = integrations_google.sync(ctx.db, now=ctx.now)
    if not counts["ok"]:
        ctx.log(f"sync failed: {counts['error']}")
        return AgentResult(summary=f"sync failed: {counts['error']}", result=counts)
    changed = counts["created"] + counts["updated"] + counts["deleted"]
    if not changed:
        return AgentResult(status="skipped", summary="calendar unchanged", result=counts)
    return AgentResult(summary=f"{counts['created']} new, {counts['updated']} changed, {counts['deleted']} removed",
                       result=counts)


SPEC = agents.register(AgentSpec(
    key="calendar_sync", name="Calendar sync",
    description="Keeps a copy of Google Calendar up to date so the briefing knows what is timed today.",
    schedule=Schedule(interval_minutes=30), events=[],
    permissions=Permissions(tools=[], tables_write=["calendar_events", "integrations"],
                            may_emit=["calendar.changed"], max_llm_calls=0),
    run=run, phase="6",
))
