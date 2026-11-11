"""
Agents — everything that runs on its own (Phase 6B, ADR-0003).

One registry of plain Python modules, run sequentially by one runner on each tick. No
queue, no worker process, no framework.

  REGISTRY            key → AgentSpec
  register(spec)      add an agent (each agent module calls this at import)
  ordered_specs()     the registry in run order: couriers last, so they see this tick's results
  emit(db, kind, …)   record an event for the next tick (modules call it in their own transaction)
  load_all()          import every shipped agent module so it registers itself

Modules:
  base      AgentSpec, AgentContext, AgentResult, Schedule, Permissions
  schedule  due(), the user's time zone
  runner    run_agent(), tick(), the permission audit, cost attribution
"""

import json
from datetime import datetime

from agents.base import AgentSpec

REGISTRY: dict[str, AgentSpec] = {}

# Shipped agent modules, imported by load_all(). A module that fails to import is skipped:
# the rest of the registry still works.
_MODULES: list[str] = [
    "crm_housekeeper", "lead_qualifier", "follow_up_sentinel", "portfolio_reviewer", "brand_sync",
    "market_intel", "application_watch",
]

EVENT_KINDS = (
    "lead.replied", "lead.stale", "payment.overdue", "research.completed", "email.received",
    "calendar.changed", "application.deadline_near", "brand.sync_failed",
)


def register(spec: AgentSpec) -> AgentSpec:
    REGISTRY[spec.key] = spec
    return spec


def ordered_specs() -> list[AgentSpec]:
    """Registration order, with the couriers moved to the end."""
    specs = list(REGISTRY.values())
    return [s for s in specs if not s.key.endswith("_courier")] + [s for s in specs if s.key.endswith("_courier")]


def emit(db, kind: str, payload: dict | None = None, source: str | None = None):
    """Insert an ``agent_events`` row. The caller owns the transaction."""
    import models
    row = models.AgentEvent(kind=kind, payload_json=json.dumps(payload or {}, default=str),
                            created_at=datetime.utcnow(), source=source)
    db.add(row)
    db.flush()
    return row


def load_all() -> None:
    """Import every shipped agent module (idempotent)."""
    import importlib
    for name in _MODULES:
        try:
            importlib.import_module(f"agents.{name}")
        except Exception as exc:  # noqa: BLE001 — one broken agent must not take the rest down
            print(f"[agents] could not load {name}: {type(exc).__name__}: {exc}")
