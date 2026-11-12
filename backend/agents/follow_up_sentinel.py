"""
Follow-up sentinel — no contacted lead goes quiet without a reminder (deterministic).

Daily at 05:30: every lead that was contacted more than seven days ago, has not replied and
has no open follow-up gets one, due today, and a ``lead.stale`` event. A ``lead.replied``
event only wakes it to note the reply in its log: the reply itself already closed the
follow-up.
"""

import json
from datetime import timedelta

import agents
from agents.base import AgentResult, AgentSpec, Permissions, Schedule

QUIET_DAYS = 7


def run(ctx) -> AgentResult:
    try:
        import leads
        import models
    except ImportError:
        return AgentResult(status="skipped", summary="leads not built")
    for event in ctx.events:
        try:
            ctx.log(f"lead {json.loads(event.payload_json or '{}').get('lead_id')} replied; nothing to chase")
        except (ValueError, TypeError):
            ctx.log("a lead replied; nothing to chase")
    cutoff = ctx.now - timedelta(days=QUIET_DAYS)
    quiet = ctx.db.query(models.Lead).filter(
        models.Lead.status == "contacted", models.Lead.last_contact_at.isnot(None),
        models.Lead.last_contact_at < cutoff).order_by(models.Lead.last_contact_at).all()
    created = 0
    for lead in quiet:
        if leads.open_follow_up(ctx.db, lead.id) is not None:
            continue
        days = (ctx.now - lead.last_contact_at).days
        leads.set_follow_up(ctx.db, lead, ctx.today, reason=f"no reply in {days} days")
        ctx.emit("lead.stale", {"lead_id": lead.id, "days": days})
        created += 1
    ctx.db.commit()
    if not created:
        return AgentResult(status="skipped", summary="every quiet lead already has a follow-up",
                           result={"quiet": len(quiet), "created": 0})
    return AgentResult(summary=f"{created} follow-up(s) added for leads with no reply",
                       result={"quiet": len(quiet), "created": created})


SPEC = agents.register(AgentSpec(
    key="follow_up_sentinel", name="Follow-up sentinel",
    description="Adds a follow-up task for any lead contacted more than a week ago that has "
                "not replied and has no reminder.",
    schedule=Schedule(at_time="05:30"), events=["lead.replied"],
    permissions=Permissions(tools=[], tables_write=["todos", "leads"], may_emit=["lead.stale"],
                            max_llm_calls=0),
    run=run, phase="5",
))
