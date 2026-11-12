"""
Migration 018 — Project portfolio (Phase 5E).

New table:
  project_updates — progress notes, milestones, blockers and status changes per project

(``projects`` itself was created by m016 with every column the portfolio needs.)

SQLite + Postgres. Idempotent. No drops. RLS enabled on the new table (Postgres).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from database import engine
from migrations._helpers import dialect, create_table_if_missing, create_index, enable_rls

TAG = "m018"

_PROJECT_UPDATES = """
CREATE TABLE IF NOT EXISTS project_updates (
    id          INTEGER PRIMARY KEY {autoincrement},
    project_id  INTEGER NOT NULL,
    kind        VARCHAR NOT NULL DEFAULT 'note',
    text        TEXT NOT NULL,
    occurred_at DATETIME NOT NULL,
    source      VARCHAR DEFAULT 'conversation',
    created_at  DATETIME
)
"""


def run(db=None):
    conn = engine.connect()
    try:
        d = dialect(conn, TAG)
        with conn.begin():
            create_table_if_missing(conn, d, "project_updates", _PROJECT_UPDATES, TAG)
            create_index(conn, "ix_project_updates_project", "project_updates", "project_id")
            enable_rls(conn, d, ["project_updates"], TAG)
        print(f"[{TAG}] Portfolio migration complete on {d}.")
    finally:
        conn.close()


if __name__ == "__main__":
    run()
