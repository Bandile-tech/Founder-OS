"""
Migration 001 — Make DailyLog.weekly_target_id nullable.

SQLite does not support ALTER COLUMN, so this migration uses the
table-swap pattern. On Postgres (and any other non-SQLite engine) the
ORM model already declares the column nullable, so create_all() builds
the schema correctly and this migration is a no-op.

Run directly:
    python migrations/001_dailylog_nullable_target.py
Or import apply() from tests / other scripts.
"""

import os
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "backend" / "founder_os.db"
# When run from inside the backend/ directory the db is one level up from migrations/
_FALLBACK = Path(__file__).parent / "founder_os.db"


def _get_conn(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or (DB_PATH if DB_PATH.exists() else _FALLBACK)
    return sqlite3.connect(str(path))


def apply(db_path: Path | None = None) -> None:
    # On non-SQLite databases the ORM already creates the correct schema
    # via create_all(); this migration is SQLite-only.
    db_url = os.getenv("DATABASE_URL", "sqlite://")
    if not db_url.startswith("sqlite") and "sqlite" not in db_url:
        print("Migration 001: non-SQLite database detected — skipping (schema correct via ORM).")
        return
    conn = _get_conn(db_path)
    cur = conn.cursor()

    # Check current schema — skip if already nullable (no NOT NULL constraint)
    cur.execute("PRAGMA table_info(daily_logs)")
    cols = {row[1]: row for row in cur.fetchall()}  # name → row
    wt_col = cols.get("weekly_target_id")
    if wt_col is None:
        print("daily_logs.weekly_target_id column not found — skipping.")
        conn.close()
        return
    # PRAGMA table_info returns (cid, name, type, notnull, dflt_value, pk)
    notnull = wt_col[3]
    if notnull == 0:
        print("Migration 001: already applied (column is already nullable).")
        conn.close()
        return

    print("Migration 001: making daily_logs.weekly_target_id nullable …")

    cur.executescript("""
        PRAGMA foreign_keys = OFF;

        ALTER TABLE daily_logs RENAME TO _daily_logs_old;

        CREATE TABLE daily_logs (
            id               INTEGER PRIMARY KEY,
            date             DATE,
            entry            VARCHAR,
            weekly_target_id INTEGER REFERENCES weekly_targets(id),
            impact_score     INTEGER DEFAULT 0
        );

        INSERT INTO daily_logs (id, date, entry, weekly_target_id, impact_score)
        SELECT                  id, date, entry, weekly_target_id, impact_score
        FROM _daily_logs_old;

        DROP TABLE _daily_logs_old;

        PRAGMA foreign_keys = ON;
    """)

    conn.commit()
    conn.close()
    print("Migration 001: done (SQLite path).")


def rollback(db_path: Path | None = None) -> None:
    """Re-adds NOT NULL (will fail if any rows have NULL weekly_target_id)."""
    conn = _get_conn(db_path)
    cur = conn.cursor()
    cur.executescript("""
        PRAGMA foreign_keys = OFF;

        ALTER TABLE daily_logs RENAME TO _daily_logs_nullable;

        CREATE TABLE daily_logs (
            id               INTEGER PRIMARY KEY,
            date             DATE,
            entry            VARCHAR,
            weekly_target_id INTEGER NOT NULL REFERENCES weekly_targets(id),
            impact_score     INTEGER DEFAULT 0
        );

        INSERT INTO daily_logs (id, date, entry, weekly_target_id, impact_score)
        SELECT                  id, date, entry, weekly_target_id, impact_score
        FROM _daily_logs_nullable;

        DROP TABLE _daily_logs_nullable;

        PRAGMA foreign_keys = ON;
    """)
    conn.commit()
    conn.close()
    print("Migration 001: rolled back.")


if __name__ == "__main__":
    apply()
