"""
Migration 006 — Add completed_at column to todos.

SQLite: ADD COLUMN idempotent (checked via PRAGMA).
Postgres: ADD COLUMN IF NOT EXISTS.
Backfill: existing done rows get completed_at = due (treated as same-day completion).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text
from database import SessionLocal, engine


def _dialect(conn) -> str:
    name = getattr(conn.dialect, "name", None) or engine.dialect.name
    if name in ("sqlite", "postgresql"):
        return name
    raise RuntimeError(f"[m006] Unsupported dialect '{name}'.")


def _needs_migration(conn) -> bool:
    dialect = _dialect(conn)
    if dialect == "postgresql":
        row = conn.execute(text("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name   = 'todos'
              AND column_name  = 'completed_at'
        """)).scalar()
        return not bool(row)
    result = conn.execute(text("PRAGMA table_info(todos)")).fetchall()
    existing = {r[1] for r in result}
    return "completed_at" not in existing


def run(db=None):
    own_db = db is None
    if own_db:
        db = SessionLocal()
    try:
        conn = engine.connect()
        dialect = _dialect(conn)

        with conn.begin():
            if _needs_migration(conn):
                print(f"[m006] Adding completed_at to todos on {dialect} …")
                if dialect == "sqlite":
                    conn.execute(text("ALTER TABLE todos ADD COLUMN completed_at DATE"))
                else:
                    conn.execute(text(
                        "ALTER TABLE todos ADD COLUMN IF NOT EXISTS completed_at DATE"
                    ))
                # Backfill: done todos get completed_at = due so they don't disappear
                conn.execute(text(
                    "UPDATE todos SET completed_at = due WHERE done = 1 AND due IS NOT NULL"
                    if dialect == "sqlite" else
                    "UPDATE todos SET completed_at = due::date WHERE done = true AND due IS NOT NULL"
                ))
                print("[m006] completed_at added and backfilled.")
            else:
                print("[m006] todos.completed_at already present — skipping.")

        conn.close()
        print(f"[m006] Migration complete on {dialect}.")
    finally:
        if own_db:
            db.close()


if __name__ == "__main__":
    run()
