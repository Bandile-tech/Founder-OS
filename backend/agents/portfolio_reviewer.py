"""
Portfolio reviewer — re-ranks the projects each morning (deterministic, no model calls).

Daily at 05:40: runs the portfolio review, stores each project's health and files a pause
suggestion for any project the review recommends pausing. It never pauses anything.
"""

import agents
from agents.base import AgentResult, AgentSpec, Permissions, Schedule


def run(ctx) -> AgentResult:
    try:
        import portfolio
    except ImportError:
        return AgentResult(status="skipped", summary="portfolio not built")
    before = set(portfolio.open_pause_suggestions(ctx.db))
    review = portfolio.full_review(ctx.db)
    new = len(set(portfolio.open_pause_suggestions(ctx.db)) - before)
    totals = review["totals"]
    result = {"active": totals["active"], "blocked": totals["blocked"], "overdue": totals["overdue"],
              "pause_suggestions_new": new, "pause_suggestions_open": len(review["pause_suggestions"])}
    if not totals["active"] and not totals["paused"]:
        return AgentResult(status="skipped", summary="no projects to review", result=result)
    focus = [e["project"]["title"] for e in review["ranked"] if e["recommendation"] == "focus"]
    summary = f"{totals['active']} active, {totals['blocked']} blocked, {totals['overdue']} overdue"
    if focus:
        summary += f"; focus: {focus[0]}"
    if new:
        summary += f"; {new} new pause suggestion(s)"
    return AgentResult(summary=summary, result=result)


SPEC = agents.register(AgentSpec(
    key="portfolio_reviewer", name="Portfolio reviewer",
    description="Ranks the live projects, names the bottleneck on each and suggests which to "
                "pause. Suggestions only; it changes no project.",
    schedule=Schedule(at_time="05:40"), events=[],
    permissions=Permissions(tools=[], tables_write=["projects", "suggestions"], max_llm_calls=0),
    run=run, phase="5",
))
