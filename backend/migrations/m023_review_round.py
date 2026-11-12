"""
Migration 023 — review round on the Phase 5/6 branch.

New table:
  agent_event_deliveries — which subscriber has been handed which event. An event with two
                         subscribers used to be consumed when the first one ran.

Columns added:
  agents.claimed_at    — set while a run holds the agent, so overlapping ticks and manual
                         runs cannot start the same agent twice (``runner._claim``).
  change_log.undoable  — FALSE on a change that records something which has already left
                         the system (a sent email or WhatsApp message). Undo keeps those
                         rows. NULL and TRUE both mean undoable, so existing rows are.
  email_messages.auth_result — whether the receiving mail server vouched for the sender:
                         pass | fail | none | confirmed. NULL on rows stored before the check.

SQLite + Postgres. Idempotent. No drops, no data changes. RLS enabled on the new table.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from database import engine
from migrations._helpers import (dialect, add_column_if_missing, create_index, create_table_if_missing,
                                 enable_rls)

TAG = "m023"

_TABLES = {
    "agent_event_deliveries": """
CREATE TABLE IF NOT EXISTS agent_event_deliveries (
    id           INTEGER PRIMARY KEY {autoincrement},
    event_id     INTEGER NOT NULL,
    agent_key    VARCHAR NOT NULL,
    delivered_at DATETIME
)""",
}


def run(db=None):
    conn = engine.connect()
    try:
        d = dialect(conn, TAG)
        with conn.begin():
            for table, sql in _TABLES.items():
                create_table_if_missing(conn, d, table, sql, TAG)
            create_index(conn, "ux_agent_event_deliveries", "agent_event_deliveries", "event_id, agent_key",
                         unique=True)
            enable_rls(conn, d, list(_TABLES), TAG)
            add_column_if_missing(conn, d, "change_log", "undoable", "BOOLEAN DEFAULT TRUE", TAG)
            add_column_if_missing(conn, d, "agents", "claimed_at", "TIMESTAMP", TAG)
            add_column_if_missing(conn, d, "email_messages", "auth_result", "VARCHAR", TAG)
        print(f"[{TAG}] Review-round migration complete on {d}.")
    finally:
        conn.close()


if __name__ == "__main__":
    run()
