"""
Email sync — reads the inbox every half hour and keeps what known people sent (no model calls).

Only mail from a lead or a client's contact is stored, as a snippet. A lead that had been
contacted and writes back becomes ``replied``, its follow-up task is closed and
``lead.replied`` is emitted — but only when the mail server vouches for the sender
(Authentication-Results). A message that fails that check is stored, changes nothing and
becomes a one-tap suggestion. Does nothing until the email address and App Password are set.
"""

import agents
from agents.base import AgentResult, AgentSpec, Permissions, Schedule


def run(ctx) -> AgentResult:
    try:
        import integrations_email
    except ImportError:
        return AgentResult(status="skipped", summary="email not built")
    if not integrations_email.configured():
        return AgentResult(status="skipped", summary="email not configured")
    counts = integrations_email.fetch(ctx.db)
    if not counts["ok"]:
        ctx.log(f"fetch failed: {counts['error']}")
        return AgentResult(summary=f"fetch failed: {counts['error']}", result=counts)
    if not counts["stored"]:
        return AgentResult(status="skipped", summary=f"nothing new from known people ({counts['ignored']} ignored)",
                           result=counts)
    unverified = f", {counts['unverified']} with an unverified sender (nothing changed)" if counts.get("unverified") else ""
    return AgentResult(summary=f"{counts['stored']} new message(s): {counts['matched_leads']} from leads, "
                               f"{counts['matched_clients']} from clients{unverified}", result=counts)


SPEC = agents.register(AgentSpec(
    key="email_sync", name="Email sync",
    description="Checks the inbox for replies from leads and clients, and records them. "
                "Mail from anyone else is never stored.",
    schedule=Schedule(interval_minutes=30), events=[],
    permissions=Permissions(tools=[], tables_write=["email_messages", "communications", "leads", "todos",
                                                    "integrations", "suggestions"],
                            may_emit=["lead.replied"], max_llm_calls=0),
    run=run, phase="6",
))
