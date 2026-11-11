"""
Market intelligence — runs standing research objectives when they fall due.

Every hour it looks for objectives whose cadence has elapsed and runs **one** (the one that
has waited longest), so a tick stays inside its budget; the rest are reported as deferred
and run on later ticks. A run surfaces findings; it promotes nothing. Does nothing without
an AI key.
"""

import agents
from agents.base import AgentResult, AgentSpec, Permissions, Schedule


def run(ctx) -> AgentResult:
    try:
        import llm
        from market_intel import objectives
    except ImportError:
        return AgentResult(status="skipped", summary="market intelligence not built")
    due = objectives.due_objectives(ctx.db, ctx.now)
    if not due:
        return AgentResult(status="skipped", summary="no research objective is due")
    if not llm.is_configured():
        return AgentResult(status="skipped", summary="no AI key configured",
                           result={"due": [o.id for o in due]})
    objective, deferred = due[0], due[1:]
    outcome = objectives.run_objective(ctx.db, objective)
    result = {"objective_id": objective.id, "project_id": outcome.get("project_id"),
              "deferred": [o.id for o in deferred]}
    if outcome.get("error"):
        ctx.log(outcome["error"])
        return AgentResult(summary=f"research on '{objective.objective[:50]}' failed",
                           result={**result, "error": outcome["error"]})
    surfaced = len(outcome.get("opportunities") or [])
    repeated = len(outcome.get("repeated") or [])
    ctx.emit("research.completed", {"project_id": outcome.get("project_id"), "objective_id": objective.id,
                                    "surfaced": surfaced})
    ctx.db.commit()
    summary = f"researched '{objective.objective[:50]}': {surfaced} new, {repeated} seen before"
    if deferred:
        summary += f"; {len(deferred)} objective(s) deferred"
    return AgentResult(summary=summary, result={**result, "surfaced": surfaced, "repeated": repeated,
                                                "cost_usd": outcome.get("cost_usd")})


SPEC = agents.register(AgentSpec(
    key="market_intel", name="Market intelligence",
    description="A five-role research department. On schedule it runs the standing research "
                "objective that has waited longest; it surfaces findings and promotes nothing.",
    schedule=Schedule(interval_minutes=60), events=[],
    permissions=Permissions(tools=[], tables_write=["research_projects", "research_findings",
                                                     "research_objectives", "research_memory_notes",
                                                     "embeddings"],
                            may_emit=["research.completed"], max_llm_calls=12, max_seconds=120),
    run=run, phase="5",
))
