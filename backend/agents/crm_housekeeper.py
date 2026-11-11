"""
CRM housekeeper — keeps payments and client health current (deterministic, no model calls).

Every hour: create the due payment for each recurring period that has arrived, flip due to
overdue, link stray revenue rows to open payments, store client health, and emit
``payment.overdue`` once per overdue payment per day.
"""

import json
from datetime import timedelta

import agents
from agents.base import AgentResult, AgentSpec, Permissions, Schedule


def _already_emitted(ctx, payment_id: int) -> bool:
    import models
    since = ctx.now - timedelta(hours=24)
    for event in ctx.db.query(models.AgentEvent).filter(
            models.AgentEvent.kind == "payment.overdue", models.AgentEvent.created_at >= since).all():
        try:
            if int(json.loads(event.payload_json or "{}").get("payment_id")) == payment_id:
                return True
        except (ValueError, TypeError):
            continue
    return False


def run(ctx) -> AgentResult:
    try:
        import crm
        import models
    except ImportError:
        return AgentResult(status="skipped", summary="crm not built")
    generated = crm.generate_due(ctx.db, ctx.today)
    flipped = crm.refresh_statuses(ctx.db, ctx.today)
    reconciled = crm.reconcile(ctx.db)
    health = crm.refresh_health(ctx.db)
    ctx.db.commit()

    emitted = 0
    overdue = ctx.db.query(models.Payment).filter(models.Payment.status == "overdue").all()
    for payment in overdue:
        if _already_emitted(ctx, payment.id):
            continue
        days = (ctx.today - payment.due_date).days if payment.due_date else None
        if ctx.emit("payment.overdue", {"payment_id": payment.id, "client_id": payment.client_id,
                                        "amount": payment.amount, "currency": payment.currency,
                                        "days_overdue": days}):
            emitted += 1
    ctx.db.commit()

    result = {"generated": generated, "status_changes": flipped, "reconciled": reconciled,
              "health_changes": health, "overdue": len(overdue), "events_emitted": emitted}
    if not any((generated, flipped, reconciled, health, emitted)):
        return AgentResult(status="skipped", summary="payments and client health already current", result=result)
    parts = []
    if generated:
        parts.append(f"{generated} instalment(s) raised")
    if reconciled:
        parts.append(f"{reconciled} payment(s) matched to revenue")
    if flipped:
        parts.append(f"{flipped} status change(s)")
    if overdue:
        parts.append(f"{len(overdue)} overdue")
    return AgentResult(summary=", ".join(parts) or "client health refreshed", result=result)


SPEC = agents.register(AgentSpec(
    key="crm_housekeeper", name="CRM housekeeper",
    description="Raises recurring instalments as they fall due, marks late payments overdue, "
                "matches logged revenue to open payments and keeps client health current.",
    schedule=Schedule(interval_minutes=60), events=[],
    permissions=Permissions(tools=[], tables_write=["payments", "recurring_revenue", "clients", "projects"],
                            may_emit=["payment.overdue"], max_llm_calls=0),
    run=run, phase="5",
))
