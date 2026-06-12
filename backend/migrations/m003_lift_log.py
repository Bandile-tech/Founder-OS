"""
Migration 003 — Refactor lift tracking out of DailyHealth into LiftLog.

Runs AFTER m002_academic_roadmap (ordered by startup hook).

On SQLite: uses the safe table-swap pattern (SQLite < 3.35 does not
support DROP COLUMN natively).

On Postgres: uses ALTER TABLE ... DROP COLUMN IF EXISTS directly.

Idempotency: checks whether the old lift columns still exist before
doing any destructive work, using database-appropriate introspection.
Lift seeding is always idempotent (INSERT only if name not present).
"""

import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text
from database import SessionLocal, engine


DEFAULT_LIFTS = [
    ("Bench Press",      1),
    ("Pull-ups",         2),
    ("Squat",            3),
    ("Incline DB Press", 4),
    ("Barbell Row",      5),
]


# ── Dialect helpers ──────────────────────────────────────────

def _dialect(conn) -> str:
    """Return 'sqlite' or 'postgresql'.  Raises on anything else."""
    name = getattr(conn.dialect, "name", None) or engine.dialect.name
    if name in ("sqlite", "postgresql"):
        return name
    raise RuntimeError(
        f"[m003] Unsupported dialect '{name}'. "
        "Add an explicit migration path before deploying."
    )


def _lift_columns_exist(conn) -> bool:
    """Return True if the old lift columns are still on daily_health."""
    dialect = _dialect(conn)
    if dialect == "postgresql":
        row = conn.execute(text("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_name = 'daily_health'
              AND column_name = 'main_lift'
        """)).scalar()
        return bool(row)
    else:
        # SQLite
        result = conn.execute(text("PRAGMA table_info(daily_health)")).fetchall()
        col_names = {row[1] for row in result}
        return "main_lift" in col_names


def _drop_lift_columns(conn):
    """Remove the three old lift columns from daily_health."""
    dialect = _dialect(conn)
    if dialect == "postgresql":
        # Postgres supports DROP COLUMN IF EXISTS natively
        conn.execute(text(
            "ALTER TABLE daily_health DROP COLUMN IF EXISTS main_lift"
        ))
        conn.execute(text(
            "ALTER TABLE daily_health DROP COLUMN IF EXISTS top_set_weight"
        ))
        conn.execute(text(
            "ALTER TABLE daily_health DROP COLUMN IF EXISTS top_set_reps"
        ))
    else:
        # SQLite: must rebuild the table without those columns
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


# ── Main entry point ─────────────────────────────────────────

def run(db=None):
    own_db = db is None
    if own_db:
        db = SessionLocal()

    try:
        # Use the engine the session is bound to (respects test overrides),
        # falling back to the module-level engine when no session is supplied.
        _engine = db.get_bind() if db is not None else engine
        conn = _engine.connect()

        with conn.begin():
            if _lift_columns_exist(conn):
                # Copy lift data before dropping columns
                conn.execute(text("""
                    INSERT INTO lift_logs
                        (lift_id, lift_name, date, weight_kg, reps, created_at)
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
                _drop_lift_columns(conn)

        # Seed default lifts (idempotent)
        with conn.begin():
            _seed_lifts(conn)

        # Fix any lift_logs rows where lift_id was -1
        with conn.begin():
            conn.execute(text("""
                UPDATE lift_logs
                SET lift_id = (
                    SELECT id FROM lifts WHERE lifts.name = lift_logs.lift_name LIMIT 1
                )
                WHERE lift_id = -1
            """))

        dialect = _dialect(conn)
        conn.close()
        print(f"[m003] Lift log migration complete on {dialect}.")

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
                     "VALUES (:name, :sort, TRUE, :ts)"),
                {"name": name, "sort": sort_order, "ts": datetime.utcnow()}
            )


if __name__ == "__main__":
    run()
