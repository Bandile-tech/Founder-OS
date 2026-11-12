"""
Migration 020 — Agents (Phase 6B).

New tables:
  agents        — one row per registered agent: toggle, schedule overrides, last-run facts
  agent_runs    — the activity log: what ran, what it did, what it cost
  agent_events  — events emitted by modules, consumed on the next tick
  job_ticks     — one row per tick (GitHub Actions, in-process, or manual)

Column added:
  llm_usage.agent_run_id  — attributes a model call to the agent run that made it

``agents`` rows are not created here: the registry in code is the source of truth, and
rows appear on the first tick or the first listing.

SQLite + Postgres. Idempotent. No drops. RLS enabled on the new tables (Postgres).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from database import engine
from migrations._helpers import (dialect, add_column_if_missing, create_table_if_missing,
                                 create_index, enable_rls)

TAG = "m020"

_TABLES = {
    "agents": """
CREATE TABLE IF NOT EXISTS agents (
    id               INTEGER PRIMARY KEY {autoincrement},
    key              VARCHAR NOT NULL UNIQUE,
    enabled          BOOLEAN NOT NULL DEFAULT TRUE,
    interval_minutes INTEGER,
    at_time          VARCHAR,
    weekdays_json    TEXT,
    config_json      TEXT,
    last_run_at      DATETIME,
    last_status      VARCHAR,
    last_summary     VARCHAR,
    runs_count       INTEGER DEFAULT 0,
    cost_usd_total   REAL DEFAULT 0,
    created_at       DATETIME,
    updated_at       DATETIME
)""",
    "agent_runs": """
CREATE TABLE IF NOT EXISTS agent_runs (
    id            INTEGER PRIMARY KEY {autoincrement},
    agent_key     VARCHAR NOT NULL,
    "trigger"     VARCHAR NOT NULL,
    tick_id       INTEGER,
    started_at    DATETIME NOT NULL,
    finished_at   DATETIME,
    status        VARCHAR NOT NULL,
    summary       VARCHAR,
    result_json   TEXT,
    error         TEXT,
    log_text      TEXT,
    llm_calls     INTEGER DEFAULT 0,
    cost_usd      REAL DEFAULT 0,
    changes_count INTEGER DEFAULT 0,
    batch_id      VARCHAR,
    over_time     BOOLEAN DEFAULT FALSE
)""",
    "agent_events": """
CREATE TABLE IF NOT EXISTS agent_events (
    id           INTEGER PRIMARY KEY {autoincrement},
    kind         VARCHAR NOT NULL,
    payload_json TEXT,
    created_at   DATETIME NOT NULL,
    consumed_at  DATETIME,
    dropped      BOOLEAN DEFAULT FALSE,
    source       VARCHAR
)""",
    "job_ticks": """
CREATE TABLE IF NOT EXISTS job_ticks (
    id              INTEGER PRIMARY KEY {autoincrement},
    "trigger"       VARCHAR NOT NULL,
    started_at      DATETIME,
    finished_at     DATETIME,
    agents_run      INTEGER,
    agents_skipped  INTEGER,
    events_consumed INTEGER,
    errors          INTEGER,
    duration_ms     INTEGER,
    summary_json    TEXT
)""",
}


def run(db=None):
    conn = engine.connect()
    try:
        d = dialect(conn, TAG)
        with conn.begin():
            for table, sql in _TABLES.items():
                create_table_if_missing(conn, d, table, sql, TAG)
            add_column_if_missing(conn, d, "llm_usage", "agent_run_id", "INTEGER", TAG)
            create_index(conn, "ix_agent_runs_key_started", "agent_runs", "agent_key, started_at")
            create_index(conn, "ix_agent_events_consumed", "agent_events", "consumed_at")
            create_index(conn, "ix_llm_usage_agent_run", "llm_usage", "agent_run_id")
            enable_rls(conn, d, list(_TABLES), TAG)
        print(f"[{TAG}] Agents migration complete on {d}.")
    finally:
        conn.close()


if __name__ == "__main__":
    run()
