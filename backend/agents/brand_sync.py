"""
Brand sync — pulls the latest numbers for every connected social account (no model calls).

Daily at 04:00: syncs each connected account through its platform adapter and emits
``brand.sync_failed`` for one that errors. Real platform data only: an account that cannot
be read is reported, never estimated.
"""

import agents
from agents.base import AgentResult, AgentSpec, Permissions, Schedule


def run(ctx) -> AgentResult:
    try:
        import models
        from brand import sync as brand_sync
        accounts_model = models.SocialAccount
    except (ImportError, AttributeError):
        return AgentResult(status="skipped", summary="brand not built")
    accounts = ctx.db.query(accounts_model).filter(
        accounts_model.status.in_(("connected", "error")), accounts_model.sync_mode == "api").all()
    if not accounts:
        return AgentResult(status="skipped", summary="no connected accounts to sync")
    synced = failed = posts_new = 0
    for account in accounts:
        outcome = brand_sync.sync(ctx.db, account)
        ctx.db.commit()
        if outcome.get("ok"):
            synced += 1
            posts_new += int(outcome.get("posts_new") or 0)
        else:
            failed += 1
            ctx.log(f"{account.platform} {account.handle or ''}: {outcome.get('error')}")
            ctx.emit("brand.sync_failed", {"account_id": account.id, "platform": account.platform,
                                           "error": outcome.get("error")})
    ctx.db.commit()
    summary = f"{synced} account(s) synced, {posts_new} new post(s)"
    if failed:
        summary += f", {failed} failed"
    return AgentResult(summary=summary, result={"synced": synced, "failed": failed, "posts_new": posts_new})


SPEC = agents.register(AgentSpec(
    key="brand_sync", name="Brand sync",
    description="Fetches the latest posts and numbers for each connected social account.",
    schedule=Schedule(at_time="04:00"), events=[],
    permissions=Permissions(tools=[], tables_write=["social_accounts", "content_posts", "content_metrics",
                                                     "account_metrics"],
                            may_emit=["brand.sync_failed"], max_llm_calls=0),
    run=run, phase="5",
))
