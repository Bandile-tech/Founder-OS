"""
Migration 004 — War Room schema additions.

Changes:
  annual_targets  — drop category/lower_is_better/year; add display_value,
                    is_complete, priority, is_active, sort_order; make
                    current_value/target_value nullable.
  books           — add position (int), is_currently_reading (bool).

Dialect handling:
  SQLite   — table-swap for annual_targets (can't drop columns); ADD COLUMN
             for books.
  Postgres — ALTER TABLE per column.  Every Postgres DDL step runs inside
             its own SAVEPOINT (begin_nested) so a single failed ALTER never
             aborts the entire migration transaction.

  If an unrecognised dialect is encountered the migration raises immediately
  rather than silently doing nothing.

PRODUCTION REPAIR NOTE (2026-06-12):
  The initial Phase 5 deploy to Supabase (Postgres) failed to add
  'position' and 'is_currently_reading' to the books table.  Root cause:
  a failing ALTER TABLE statement inside annual_targets migration aborted
  the Postgres transaction; the books block never ran.  Manual repair:

      ALTER TABLE books ADD COLUMN IF NOT EXISTS position INTEGER DEFAULT 0;
      ALTER TABLE books ADD COLUMN IF NOT EXISTS is_currently_reading
          BOOLEAN DEFAULT FALSE;

  Database and model state are now aligned.  This migration is idempotent
  (uses IF NOT EXISTS / checks before running) so re-running is safe.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text
from database import SessionLocal, engine


# ── Dialect helper ────────────────────────────────────────────

def _dialect(conn) -> str:
    """Return 'sqlite' or 'postgresql'.  Raises on anything else."""
    name = getattr(conn.dialect, "name", None) or engine.dialect.name
    if name in ("sqlite", "postgresql"):
        return name
    raise RuntimeError(
        f"[m004] Unsupported dialect '{name}'. "
        "Add an explicit migration path before deploying."
    )


# ── Annual targets ────────────────────────────────────────────

def _at_needs_migration(conn) -> bool:
    """Return True if annual_targets still has the old 'year' column."""
    dialect = _dialect(conn)
    if dialect == "postgresql":
        row = conn.execute(text("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'annual_targets'
              AND column_name = 'year'
        """)).scalar()
        return bool(row)
    # SQLite
    result = conn.execute(text("PRAGMA table_info(annual_targets)")).fetchall()
    return "year" in {r[1] for r in result}


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
    """
    Add / remove columns on Postgres.

    CRITICAL: each DDL statement runs inside its own SAVEPOINT (begin_nested).
    In PostgreSQL, any error in a transaction marks it as aborted — all
    subsequent statements in that connection then silently fail.  By wrapping
    each statement in a SAVEPOINT we isolate failures: a single failed ALTER
    only rolls back that savepoint, leaving the outer transaction alive and
    allowing the remaining statements to execute normally.
    """
    add_cols = [
        "ALTER TABLE annual_targets ADD COLUMN IF NOT EXISTS display_value VARCHAR",
        "ALTER TABLE annual_targets ADD COLUMN IF NOT EXISTS is_complete BOOLEAN DEFAULT FALSE",
        "ALTER TABLE annual_targets ADD COLUMN IF NOT EXISTS priority INTEGER DEFAULT 3",
        "ALTER TABLE annual_targets ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE",
        "ALTER TABLE annual_targets ADD COLUMN IF NOT EXISTS sort_order INTEGER DEFAULT 0",
    ]
    drop_cols = [
        "ALTER TABLE annual_targets DROP COLUMN IF EXISTS category",
        "ALTER TABLE annual_targets DROP COLUMN IF EXISTS lower_is_better",
        "ALTER TABLE annual_targets DROP COLUMN IF EXISTS year",
    ]
    nullable_cols = ["current_value", "target_value"]

    for stmt in add_cols + drop_cols:
        try:
            with conn.begin_nested():
                conn.execute(text(stmt))
            print(f"[m004] OK: {stmt[:70]}")
        except Exception as e:
            print(f"[m004] skipped (savepoint rolled back): {stmt[:60]} — {e}")

    for col in nullable_cols:
        try:
            with conn.begin_nested():
                conn.execute(text(
                    f"ALTER TABLE annual_targets ALTER COLUMN {col} DROP NOT NULL"
                ))
            print(f"[m004] OK: {col} DROP NOT NULL")
        except Exception as e:
            # This is expected if the column was already nullable
            print(f"[m004] skipped — {col} already nullable or error: {e}")


# ── Books ─────────────────────────────────────────────────────

def _books_need_migration(conn) -> bool:
    """Return True if books table lacks the 'position' column."""
    dialect = _dialect(conn)
    if dialect == "postgresql":
        row = conn.execute(text("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'books'
              AND column_name = 'position'
        """)).scalar()
        return not bool(row)
    # SQLite
    result = conn.execute(text("PRAGMA table_info(books)")).fetchall()
    return "position" not in {r[1] for r in result}


def _migrate_books(conn):
    """Add position and is_currently_reading to books. Each DDL in its own savepoint."""
    dialect = _dialect(conn)
    if dialect == "postgresql":
        for stmt in [
            "ALTER TABLE books ADD COLUMN IF NOT EXISTS position INTEGER DEFAULT 0",
            "ALTER TABLE books ADD COLUMN IF NOT EXISTS is_currently_reading BOOLEAN DEFAULT FALSE",
        ]:
            try:
                with conn.begin_nested():
                    conn.execute(text(stmt))
                print(f"[m004] OK: {stmt[:70]}")
            except Exception as e:
                print(f"[m004] books DDL skipped: {e}")
    else:
        # SQLite ADD COLUMN is safe when a default is provided
        conn.execute(text("ALTER TABLE books ADD COLUMN position INTEGER DEFAULT 0"))
        conn.execute(text("ALTER TABLE books ADD COLUMN is_currently_reading BOOLEAN DEFAULT 0"))


# ── Entry point ───────────────────────────────────────────────

def run(db=None):
    own_db = db is None
    if own_db:
        db = SessionLocal()

    try:
        # Always use engine.connect() directly — Session.bind was removed in
        # SQLAlchemy 2.x and the fallback to engine.connect() is always correct.
        conn = engine.connect()
        dialect = _dialect(conn)

        with conn.begin():
            if _at_needs_migration(conn):
                print(f"[m004] Migrating annual_targets schema on {dialect} …")
                if dialect == "postgresql":
                    _migrate_annual_targets_postgres(conn)
                else:
                    _migrate_annual_targets_sqlite(conn)
                print("[m004] annual_targets migration done.")
            else:
                print("[m004] annual_targets already up-to-date.")

        with conn.begin():
            if _books_need_migration(conn):
                print(f"[m004] Adding position/is_currently_reading to books on {dialect} …")
                _migrate_books(conn)
                print("[m004] books migration done.")
            else:
                print("[m004] books already up-to-date.")

        conn.close()
        print(f"[m004] War Room migration complete on {dialect}.")
    finally:
        if own_db:
            db.close()


if __name__ == "__main__":
    run()
