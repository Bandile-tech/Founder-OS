"""
Vault export — once a day, writes the notes the assistant created or changed into the
vault folder ``Founder OS/`` through the GitHub API (``vault_export.export``).

Skipped until ``VAULT_REPO`` and ``VAULT_WRITE_TOKEN`` are set. It makes no model call and
writes nothing through the change log: the export record is bookkeeping, like a cache.
"""

import agents
from agents.base import AgentResult, AgentSpec, Permissions, Schedule


def run(ctx) -> AgentResult:
    import vault_export
    if not vault_export.configured():
        return AgentResult(status="skipped", summary="VAULT_REPO and VAULT_WRITE_TOKEN are not set")
    out = vault_export.export(ctx.db)
    for line in out["errors"]:
        ctx.log(line)
    if not (out["exported"] or out["removed"] or out["errors"]):
        return AgentResult(status="skipped", summary="no note has changed since the last export", result=out)
    parts = [f"{out['exported']} note(s) written to the vault"] if out["exported"] else []
    if out["removed"]:
        parts.append(f"{out['removed']} removed")
    if out["errors"]:
        parts.append(f"{len(out['errors'])} failed")
    return AgentResult(summary=", ".join(parts), result=out)


SPEC = agents.register(AgentSpec(
    key="vault_export", name="Vault export",
    description="Once a day, writes the notes the assistant created or changed into the vault folder "
                "Founder OS/, so they show up in Obsidian.",
    schedule=Schedule(at_time="19:30"), events=[],
    permissions=Permissions(tools=[], tables_write=[], may_notify=False, max_llm_calls=0, max_seconds=120),
    run=run, phase="7",
))
