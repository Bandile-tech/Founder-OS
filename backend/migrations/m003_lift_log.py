"""
Migration 003 — Refactor lift tracking out of DailyHealth into LiftLog.

Runs AFTER m002_academic_roadmap (alphabetical ordering enforced by startup hook).

Steps
-----
1. Idempotency check: if daily_health no longer has the three lift columns
   (main_lift, top_set_weight, top_set_reps), migration is already done — skip.
2. Copy any non-null lift data from daily_health into lift_logs.
3. Rebuild daily_health without the three lift columns via the safe
   SQLite table-swap pattern (SQLite < 3.35 does not support DROP COLUMN).
4. Seed the five default Lift rows — idempotent: only inserts if name
   is not already present.

Run directly:
    python migrations/m003_lift_log.py
Or imported and called as run(db) from the startup hook.
"""

import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text
from database import SessionLocal, engine


DEFAULT_LIFTS = [
    ("Bench Press",     1),
    ("Pull-ups",        2),
    ("Squat",           3),
    ("Incline DB Press", 4),
    ("Barbell Row",     5),
]


def _columns_exist(conn) -> bool:
    """Return True if the old lift columns are still on daily_health."""
    result = conn.execute(text("PRAGMA table_info(daily_health)")).fetchall()
    col_names = {row[1] for row in result}
    return "main_lift" in col_names


def run(db=None):
    own_db = db is None
    if own_db:
        db = SessionLocal()

    try:
        conn = db.bind.connect() if hasattr(db, "bind") else engine.connect()

        with conn.begin():
            if not _columns_exist(conn):
                # Already migrated — only seed lifts if needed.
                _seed_lifts(conn)
                return

            # 1. Copy lift data into lift_logs (only rows that have a lift logged)
            conn.execute(text("""
                INSERT INTO lift_logs (lift_id, lift_name, date, weight_kg, reps, created_at)
                SELECT
                    COALESCE(
                        (SELECT id FROM lifts WHERE name = dh.main_lift LIMIT 1),
                        -1
                    ),
                    dh.main_lift,
                    dh.date,
                    COALESCE(dh.top_set_weight, 0),
                    COALESCE(dh.top_set_reps, 0),
                    CURRENT_TIMESTAMP
                FROM daily_health dh
                WHERE dh.main_lift IS NOT NULL
                  AND dh.top_set_weight IS NOT NULL
            """))

            # 2. Build new daily_health without the three lift columns
            conn.execute(text("""
                CREATE TABLE daily_health_new (
                    id            INTEGER PRIMARY KEY,
                    date          DATE NOT NULL UNIQUE,
                    sleep_hours   REAL,
                    mobility_done BOOLEAN DEFAULT 0,
                    session_done  BOOLEAN DEFAULT 0,
                    notes         TEXT,
                    created_at    DATETIME,
                    updated_at    DATETIME
                )
            """))

            conn.execute(text("""
                INSERT INTO daily_health_new
                    (id, date, sleep_hours, mobility_done, session_done,
                     notes, created_at, updated_at)
                SELECT
                    id, date, sleep_hours, mobility_done, session_done,
                    notes, created_at, updated_at
                FROM daily_health
            """))

            conn.execute(text("DROP TABLE daily_health"))
            conn.execute(text("ALTER TABLE daily_health_new RENAME TO daily_health"))

            # 3. Fix up lift_logs rows that got lift_id = -1 (lift didn't exist yet)
            #    Those will be resolved after seeding below — re-run update.

        # Seed default lifts (idempotent)
        with conn.begin():
            _seed_lifts(conn)

        # Fix any lift_logs rows where lift_id was -1 (lift now exists after seed)
        with conn.begin():
            conn.execute(text("""
                UPDATE lift_logs
                SET lift_id = (
                    SELECT id FROM lifts WHERE lifts.name = lift_logs.lift_name LIMIT 1
                )
                WHERE lift_id = -1
            """))

        conn.close()
        print("[m003] Lift log migration complete.")

    finally:
        if own_db:
            db.close()


def _seed_lifts(conn):
    for name, sort_order in DEFAULT_LIFTS:
        existing = conn.execute(
            text("SELECT id FROM lifts WHERE name = :name"),
            {"name": name}
        ).fetchone()
        if not existing:
            conn.execute(
                text("INSERT INTO lifts (name, sort_order, is_active, created_at) "
                     "VALUES (:name, :sort, 1, :ts)"),
                {"name": name, "sort": sort_order, "ts": datetime.utcnow()}
            )


if __name__ == "__main__":
    run()
