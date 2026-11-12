"""
Migration 024 — a failed briefing summary is remembered.

Columns added:
  briefing_summaries.failed_at  — when the last summary call for that day failed (or came
                                  back empty). While it is under 30 minutes old no new call
                                  is made, on a plain load or a refresh. Cleared on success.
  briefing_summaries.last_error — the failure, redacted and truncated, for diagnosis.

A day whose only call failed has a row with an empty ``summary``.

SQLite + Postgres. Idempotent. No drops, no data changes.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from database import engine
from migrations._helpers import dialect, add_column_if_missing

TAG = "m024"


def run(db=None):
    conn = engine.connect()
    try:
        d = dialect(conn, TAG)
        with conn.begin():
            add_column_if_missing(conn, d, "briefing_summaries", "failed_at", "TIMESTAMP", TAG)
            add_column_if_missing(conn, d, "briefing_summaries", "last_error", "VARCHAR", TAG)
        print(f"[{TAG}] Briefing summary failure columns ready on {d}.")
    finally:
        conn.close()


if __name__ == "__main__":
    run()
