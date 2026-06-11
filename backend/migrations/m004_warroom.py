"""
Migration 004 — War Room schema additions.

Changes:
  annual_targets  — drop category/lower_is_better/year; add display_value,
                    is_complete, priority, is_active, sort_order; make
                    current_value/target_value nullable.
  books           — add position (int), is_currently_reading (bool).

On Postgres: ORM create_all() already built the correct schema; this migration
is a no-op (ADD COLUMN IF EXISTS is used for safety).

On SQLite: uses table-swap for annual_targets (can't drop columns or ALTER
NOT NULL), and ADD COLUMN for books (safe since defaults are provided).

Idempotency: guarded by checking whether the migration has already run.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text
from database import SessionLocal, engine


def _dialect(conn) -> str:
    name = getattr(conn, "dialect", None)
    if name:
        return name.name
    try:
        return engine.dialect.name
    except Exception:
        return "sqlite"


# ── Annual targets ────────────────────────────────────────────

def _at_needs_migration(conn) -> bool:
    """Return True if annual_targets still has the old schema (has 'year' column)."""
    dialect = _dialect(conn)
    if dialect == "postgresql":
        row = conn.execute(text("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_name = 'annual_targets' AND column_name = 'year'
        """)).scalar()
        return bool(row)
    else:
        result = conn.execute(text("PRAGMA table_info(annual_targets)")).fetchall()
        col_names = {r[1] for r in result}
        return "year" in col_names


def _migrate_annual_targets_sqlite(conn):
    """Full table-swap for annual_targets on SQLite."""
    conn.execute(text("""
        CREATE TABLE annual_targets_new (
            id            INTEGER PRIMARY KEY,
            name          VARCHAR NOT NULL,
            current_value REAL,
            target_value  REAL,
            unit          VARCHAR,
            display_value VARCHAR,
            is_complete   BOOLEAN DEFAULT 0,
            priority      INTEGER DEFAULT 3,
            is_active     BOOLEAN DEFAULT 1,
            sort_order    INTEGER DEFAULT 0,
            created_at    DATETIME,
            updated_at    DATETIME
        )
    """))
    # Copy rows that had numeric targets (current_value/target_value were NOT NULL before)
    conn.execute(text("""
        INSERT INTO annual_targets_new
            (id, name, current_value, target_value, unit,
             display_value, is_complete, priority, is_active, sort_order,
             created_at, updated_at)
        SELECT
            id, name, current_value, target_value, unit,
            NULL, 0, 3, 1, 0,
            created_at, updated_at
        FROM annual_targets
    """))
    conn.execute(text("DROP TABLE annual_targets"))
    conn.execute(text("ALTER TABLE annual_targets_new RENAME TO annual_targets"))


def _migrate_annual_targets_postgres(conn):
    """Add missing columns on Postgres (columns created by ORM may already exist)."""
    for stmt in [
        "ALTER TABLE annual_targets ADD COLUMN IF NOT EXISTS display_value VARCHAR",
        "ALTER TABLE annual_targets ADD COLUMN IF NOT EXISTS is_complete BOOLEAN DEFAULT FALSE",
        "ALTER TABLE annual_targets ADD COLUMN IF NOT EXISTS priority INTEGER DEFAULT 3",
        "ALTER TABLE annual_targets ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE",
        "ALTER TABLE annual_targets ADD COLUMN IF NOT EXISTS sort_order INTEGER DEFAULT 0",
        "ALTER TABLE annual_targets DROP COLUMN IF EXISTS category",
        "ALTER TABLE annual_targets DROP COLUMN IF EXISTS lower_is_better",
        "ALTER TABLE annual_targets DROP COLUMN IF EXISTS year",
        "ALTER TABLE annual_targets ALTER COLUMN current_value DROP NOT NULL",
        "ALTER TABLE annual_targets ALTER COLUMN target_value DROP NOT NULL",
    ]:
        try:
            conn.execute(text(stmt))
        except Exception as e:
            print(f"[m004] Postgres annual_targets step skipped ({e})")


# ── Books ─────────────────────────────────────────────────────

def _books_need_migration(conn) -> bool:
    """Return True if books table lacks the 'position' column."""
    dialect = _dialect(conn)
    if dialect == "postgresql":
        row = conn.execute(text("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_name = 'books' AND column_name = 'position'
        """)).scalar()
        return not bool(row)
    else:
        result = conn.execute(text("PRAGMA table_info(books)")).fetchall()
        col_names = {r[1] for r in result}
        return "position" not in col_names


def _migrate_books(conn):
    dialect = _dialect(conn)
    if dialect == "postgresql":
        conn.execute(text(
            "ALTER TABLE books ADD COLUMN IF NOT EXISTS position INTEGER DEFAULT 0"
        ))
        conn.execute(text(
            "ALTER TABLE books ADD COLUMN IF NOT EXISTS is_currently_reading BOOLEAN DEFAULT FALSE"
        ))
    else:
        # SQLite ADD COLUMN is safe when a default is provided
        conn.execute(text(
            "ALTER TABLE books ADD COLUMN position INTEGER DEFAULT 0"
        ))
        conn.execute(text(
            "ALTER TABLE books ADD COLUMN is_currently_reading BOOLEAN DEFAULT 0"
        ))


# ── Entry point ───────────────────────────────────────────────

def run(db=None):
    own_db = db is None
    if own_db:
        db = SessionLocal()

    try:
        conn = db.bind.connect() if hasattr(db, "bind") else engine.connect()
        dialect = _dialect(conn)

        with conn.begin():
            if _at_needs_migration(conn):
                print("[m004] Migrating annual_targets schema …")
                if dialect == "postgresql":
                    _migrate_annual_targets_postgres(conn)
                else:
                    _migrate_annual_targets_sqlite(conn)
                print("[m004] annual_targets migration done.")
            else:
                print("[m004] annual_targets already up-to-date.")

        with conn.begin():
            if _books_need_migration(conn):
                print("[m004] Adding position/is_currently_reading to books …")
                _migrate_books(conn)
                print("[m004] books migration done.")
            else:
                print("[m004] books already up-to-date.")

        conn.close()
        print("[m004] War Room migration complete.")
    finally:
        if own_db:
            db.close()


if __name__ == "__main__":
    run()
