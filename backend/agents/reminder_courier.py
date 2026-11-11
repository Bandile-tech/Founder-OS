"""
Reminder courier — sends whatever is due: reminders Bandile asked for, and anything quiet
hours held back. It only dispatches; it never writes a notification of its own.
"""

import agents
from agents.base import AgentResult, AgentSpec, Permissions, Schedule


def run(ctx) -> AgentResult:
    try:
        import notify
    except ImportError:
        return AgentResult(status="skipped", summary="notifications not built")
    outcome = notify.dispatch(ctx.db, ctx.now)
    if not any(outcome.values()):
        return AgentResult(status="skipped", summary="nothing due", result=outcome)
    return AgentResult(summary=", ".join(f"{count} {label}" for label, count in outcome.items() if count),
                       result=outcome)


SPEC = agents.register(AgentSpec(
    key="reminder_courier", name="Reminder courier",
    description="Sends reminders when they fall due, and anything that quiet hours held back.",
    schedule=Schedule(interval_minutes=15), events=[],
    permissions=Permissions(tools=[], tables_write=[], may_notify=True, max_llm_calls=0),
    run=run, phase="6",
))
