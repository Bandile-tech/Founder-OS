"""
Migration 023 — review round on the Phase 5/6 branch.

Column added:
  change_log.undoable  — FALSE on a change that records something which has already left
                         the system (a sent email or WhatsApp message). Undo keeps those
                         rows. NULL and TRUE both mean undoable, so existing rows are.

SQLite + Postgres. Idempotent. No drops, no data changes.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from database import engine
from migrations._helpers import dialect, add_column_if_missing

TAG = "m023"


def run(db=None):
    conn = engine.connect()
    try:
        d = dialect(conn, TAG)
        with conn.begin():
            add_column_if_missing(conn, d, "change_log", "undoable", "BOOLEAN DEFAULT TRUE", TAG)
        print(f"[{TAG}] Review-round migration complete on {d}.")
    finally:
        conn.close()


if __name__ == "__main__":
    run()
