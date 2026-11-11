"""
Lead qualifier — scores new leads against the ideal customer profile.

Every three hours: up to 20 leads that are still ``new``, have a way to be reached and have
never been scored. One model call per lead (the cap is 20 a run). Does nothing without an
AI key.
"""

import agents
from agents.base import AgentResult, AgentSpec, Permissions, Schedule

BATCH = 20


def run(ctx) -> AgentResult:
    try:
        import leads
        import llm
        import models
    except ImportError:
        return AgentResult(status="skipped", summary="leads not built")
    if not llm.is_configured():
        return AgentResult(status="skipped", summary="no AI key configured")
    rows = ctx.db.query(models.Lead).filter(
        models.Lead.status == "new", models.Lead.qualification_score.is_(None),
    ).order_by(models.Lead.id).all()
    reachable = [l for l in rows if l.email or l.whatsapp or l.phone][:BATCH]
    if not reachable:
        return AgentResult(status="skipped", summary="no new leads to qualify")
    results = leads.qualify(ctx.db, [l.id for l in reachable])
    ctx.db.commit()
    qualified = sum(1 for r in results if r.get("outcome") == "qualified")
    disqualified = sum(1 for r in results if r.get("outcome") == "disqualified")
    failed = sum(1 for r in results if r.get("error"))
    return AgentResult(
        summary=f"qualified {qualified} of {len(results)} ({disqualified} disqualified"
                + (f", {failed} could not be scored" if failed else "") + ")",
        result={"scored": len(results), "qualified": qualified, "disqualified": disqualified, "failed": failed})


SPEC = agents.register(AgentSpec(
    key="lead_qualifier", name="Lead qualifier",
    description="Scores new leads that can be reached against the ideal customer profile, "
                "twenty at a time.",
    schedule=Schedule(interval_minutes=180), events=[],
    permissions=Permissions(tools=[], tables_write=["leads"], max_llm_calls=BATCH),
    run=run, phase="5",
))
