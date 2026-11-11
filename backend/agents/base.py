"""
Agent building blocks: the spec an agent ships with, and the context a run receives.

An agent is a plain function ``run(ctx) -> AgentResult`` plus a declaration of when it runs
and what it may touch. The runner supplies everything else (session, change-log batch, cost
attribution, the audit). Agents never ask questions and never raise on purpose.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable

LOG_LIMIT_BYTES = 20_000


@dataclass
class Schedule:
    interval_minutes: int | None = None      # run when now - last_run >= interval
    at_time: str | None = None               # "06:00" local; run once per day after this time
    weekdays: list[int] | None = None        # 0=Mon … 6=Sun; None = every day
    min_gap_minutes: int = 10                # never run twice inside this gap (double ticks)


@dataclass
class Permissions:
    tools: list[str] = field(default_factory=list)          # orchestrator tools it may call via ctx.run_tool
    tables_write: list[str] = field(default_factory=list)   # tables its change-log entries may touch
    may_notify: bool = False                                 # may call ctx.notify
    may_emit: list[str] = field(default_factory=list)       # event kinds it may emit
    max_llm_calls: int = 20                                  # hard cap per run
    max_seconds: int = 60


@dataclass
class AgentResult:
    summary: str                                             # one human line
    result: dict = field(default_factory=dict)
    status: str = "completed"                                # completed | skipped (nothing to do)


@dataclass
class AgentSpec:
    key: str
    name: str
    description: str
    schedule: Schedule
    events: list[str]                                        # event kinds that trigger it
    permissions: Permissions
    run: Callable[["AgentContext"], AgentResult]
    enabled_by_default: bool = True
    phase: str = "5"                                         # for display
    default_config: dict = field(default_factory=dict)


@dataclass
class AgentContext:
    db: Any                                                  # the run's own SQLAlchemy session
    now: datetime                                            # naive UTC
    now_local: datetime                                      # aware, in FOUNDER_OS_TZ
    today: date                                              # local date
    run: Any                                                 # models.AgentRun
    spec: AgentSpec
    row: Any                                                 # models.Agent
    events: list = field(default_factory=list)               # the events that triggered this run
    config: dict = field(default_factory=dict)               # row.config_json merged over spec defaults

    def run_tool(self, name: str, args: dict | None = None) -> tuple[dict, str]:
        """Call an orchestrator tool the agent is allowed to use. Never raises."""
        if name not in self.spec.permissions.tools:
            msg = f"tool '{name}' is not allowed for agent {self.spec.key}"
            self.log(msg)
            return {"error": msg}, msg
        import orchestrator_tools
        return orchestrator_tools.run_tool(name, args or {}, self.db)

    def notify(self, *, kind: str, title: str, body: str, dedupe_key: str, send_at=None) -> dict:
        """Queue a notification to Bandile. Refused unless the agent may notify."""
        if not self.spec.permissions.may_notify:
            msg = f"agent {self.spec.key} may not notify"
            self.log(msg)
            return {"error": msg}
        try:
            import notify
        except ImportError:
            return {"error": "6D not built"}
        return notify.queue(self.db, kind=kind, title=title, body=body, dedupe_key=dedupe_key,
                            send_at=send_at, source=f"agent:{self.spec.key}")

    def emit(self, kind: str, payload: dict | None = None) -> bool:
        """Emit an event for other agents. Refused unless the kind is in may_emit."""
        if kind not in self.spec.permissions.may_emit:
            self.log(f"emit '{kind}' refused: not in may_emit")
            return False
        import agents
        agents.emit(self.db, kind, payload or {}, source=f"agent:{self.spec.key}")
        return True

    def log(self, line: str) -> None:
        """Append a line to the run's log (kept to about 20 KB)."""
        text = (self.run.log_text or "") + str(line).rstrip() + "\n"
        if len(text.encode("utf-8")) > LOG_LIMIT_BYTES:
            text = text.encode("utf-8")[-LOG_LIMIT_BYTES:].decode("utf-8", "ignore")
        self.run.log_text = text
