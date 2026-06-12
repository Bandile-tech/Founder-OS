"""
Migration 005 — Reading plan schema additions + book position backfill.

Part A — books (position backfill):
  Any Book row with position = 0 gets position = row_number ordered by
  created_at.  Idempotent: if all positions are already distinct and
  non-zero the backfill is skipped.

Part B — reading_plans (new columns):
  current_book             VARCHAR   default ''
  current_chapter          INTEGER   default 1
  daily_target_chapters    INTEGER   default 1
  start_date               DATE      nullable
  target_completion_date   DATE      nullable
  notes                    VARCHAR   default ''

Dialect handling: SQLite ADD COLUMN (safe with defaults).
Postgres: ADD COLUMN IF NOT EXISTS per column inside SAVEPOINTs.
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
    raise RuntimeError(f"[m005] Unsupported dialect '{name}'.")


# ── Part A: book position backfill ───────────────────────────

def _books_need_position_backfill(conn) -> bool:
    """True if any book has position = 0 (and there is more than one book)."""
    total = conn.execute(text("SELECT COUNT(*) FROM books")).scalar() or 0
    if total <= 1:
        return False
    zero_count = conn.execute(
        text("SELECT COUNT(*) FROM books WHERE position = 0")
    ).scalar() or 0
    return zero_count > 0


def _backfill_book_positions_sqlite(conn):
    rows = conn.execute(
        text("SELECT id FROM books ORDER BY created_at ASC, id ASC")
    ).fetchall()
    for pos, row in enumerate(rows):
        conn.execute(
            text("UPDATE books SET position = :pos WHERE id = :id"),
            {"pos": pos, "id": row[0]},
        )


def _backfill_book_positions_postgres(conn):
    conn.execute(text("""
        UPDATE books b
        SET position = sub.rn
        FROM (
            SELECT id, ROW_NUMBER() OVER (ORDER BY created_at ASC, id ASC) - 1 AS rn
            FROM books
        ) sub
        WHERE b.id = sub.id
    """))


# ── Part B: reading_plans new columns ────────────────────────

_RP_COLS_SQLITE = [
    "ALTER TABLE reading_plans ADD COLUMN current_book VARCHAR DEFAULT ''",
    "ALTER TABLE reading_plans ADD COLUMN current_chapter INTEGER DEFAULT 1",
    "ALTER TABLE reading_plans ADD COLUMN daily_target_chapters INTEGER DEFAULT 1",
    "ALTER TABLE reading_plans ADD COLUMN start_date DATE",
    "ALTER TABLE reading_plans ADD COLUMN target_completion_date DATE",
    "ALTER TABLE reading_plans ADD COLUMN notes VARCHAR DEFAULT ''",
]

_RP_COLS_POSTGRES = [
    "ALTER TABLE reading_plans ADD COLUMN IF NOT EXISTS current_book VARCHAR DEFAULT ''",
    "ALTER TABLE reading_plans ADD COLUMN IF NOT EXISTS current_chapter INTEGER DEFAULT 1",
    "ALTER TABLE reading_plans ADD COLUMN IF NOT EXISTS daily_target_chapters INTEGER DEFAULT 1",
    "ALTER TABLE reading_plans ADD COLUMN IF NOT EXISTS start_date DATE",
    "ALTER TABLE reading_plans ADD COLUMN IF NOT EXISTS target_completion_date DATE",
    "ALTER TABLE reading_plans ADD COLUMN IF NOT EXISTS notes VARCHAR DEFAULT ''",
]


def _rp_needs_migration(conn) -> bool:
    """True if reading_plans lacks current_book column."""
    dialect = _dialect(conn)
    if dialect == "postgresql":
        row = conn.execute(text("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name   = 'reading_plans'
              AND column_name  = 'current_book'
        """)).scalar()
        return not bool(row)
    result = conn.execute(text("PRAGMA table_info(reading_plans)")).fetchall()
    existing = {r[1] for r in result}
    return "current_book" not in existing


def _migrate_reading_plans(conn, dialect):
    if dialect == "sqlite":
        result = conn.execute(text("PRAGMA table_info(reading_plans)")).fetchall()
        existing = {r[1] for r in result}
        for stmt in _RP_COLS_SQLITE:
            col = stmt.split("ADD COLUMN ")[1].split()[0]
            if col not in existing:
                conn.execute(text(stmt))
                print(f"[m005] OK: {stmt[:70]}")
    else:
        for stmt in _RP_COLS_POSTGRES:
            try:
                with conn.begin_nested():
                    conn.execute(text(stmt))
                print(f"[m005] OK: {stmt[:70]}")
            except Exception as e:
                print(f"[m005] skipped: {stmt[:60]} — {e}")


# ── Entry point ───────────────────────────────────────────────

def run(db=None):
    own_db = db is None
    if own_db:
        db = SessionLocal()
    try:
        conn = engine.connect()
        dialect = _dialect(conn)

        # Part A: book position backfill
        with conn.begin():
            if _books_need_position_backfill(conn):
                print(f"[m005] Backfilling book positions on {dialect} …")
                if dialect == "sqlite":
                    _backfill_book_positions_sqlite(conn)
                else:
                    _backfill_book_positions_postgres(conn)
                print("[m005] Book position backfill done.")
            else:
                print("[m005] Book positions already OK.")

        # Part B: reading_plans schema additions
        with conn.begin():
            if _rp_needs_migration(conn):
                print(f"[m005] Adding new columns to reading_plans on {dialect} …")
                _migrate_reading_plans(conn, dialect)
                print("[m005] reading_plans migration done.")
            else:
                print("[m005] reading_plans already up-to-date.")

        conn.close()
        print(f"[m005] Migration complete on {dialect}.")
    finally:
        if own_db:
            db.close()


if __name__ == "__main__":
    run()
