"""
Migration 025 — money that waits for a milestone.

Columns added:
  payments.due_trigger            — the milestone a due payment waits for ("go-live").
                                    While it is set the payment has no due date.
  recurring_revenue.start_trigger — the milestone a recurring amount waits for. While it
                                    is set the row is inactive and generates nothing.

``crm.mark_milestone`` clears both when the milestone happens.

SQLite + Postgres. Idempotent. No drops, no data changes.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from database import engine
from migrations._helpers import dialect, add_column_if_missing

TAG = "m025"


def run(db=None):
    conn = engine.connect()
    try:
        d = dialect(conn, TAG)
        with conn.begin():
            add_column_if_missing(conn, d, "payments", "due_trigger", "VARCHAR", TAG)
            add_column_if_missing(conn, d, "recurring_revenue", "start_trigger", "VARCHAR", TAG)
        print(f"[{TAG}] Payment and recurring milestone columns ready on {d}.")
    finally:
        conn.close()


if __name__ == "__main__":
    run()
