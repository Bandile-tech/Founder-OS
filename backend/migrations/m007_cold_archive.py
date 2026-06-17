"""
Migration 007 — Create cold_archive_chunks table.

SQLite:  CREATE TABLE IF NOT EXISTS.
Postgres: CREATE TABLE IF NOT EXISTS.
Idempotent — safe to run multiple times.
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
    raise RuntimeError(f"[m007] Unsupported dialect '{name}'.")


def _table_exists(conn, dialect: str) -> bool:
    if dialect == "postgresql":
        row = conn.execute(text("""
            SELECT COUNT(*) FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name   = 'cold_archive_chunks'
        """)).scalar()
        return bool(row)
    rows = conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name='cold_archive_chunks'")
    ).fetchall()
    return bool(rows)


_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS cold_archive_chunks (
    id          INTEGER PRIMARY KEY {autoincrement},
    file_path   VARCHAR  NOT NULL,
    file_title  VARCHAR  NOT NULL,
    content     TEXT     NOT NULL,
    chunk_index INTEGER  NOT NULL,
    word_count  INTEGER,
    last_synced DATETIME,
    created_at  DATETIME
)
"""

_CREATE_SQL_SQLITE = _CREATE_SQL.format(autoincrement="AUTOINCREMENT")
_CREATE_SQL_PG = _CREATE_SQL.format(autoincrement="")

_INDEX_SQL_FILE_PATH = (
    "CREATE INDEX IF NOT EXISTS ix_cold_archive_chunks_file_path "
    "ON cold_archive_chunks (file_path)"
)


def run(db=None):
    own_db = db is None
    if own_db:
        db = SessionLocal()
    try:
        conn = engine.connect()
        dialect = _dialect(conn)

        with conn.begin():
            if not _table_exists(conn, dialect):
                print(f"[m007] Creating cold_archive_chunks on {dialect} …")
                sql = _CREATE_SQL_SQLITE if dialect == "sqlite" else _CREATE_SQL_PG
                conn.execute(text(sql))
                conn.execute(text(_INDEX_SQL_FILE_PATH))
                print("[m007] cold_archive_chunks created.")
            else:
                print("[m007] cold_archive_chunks already present — skipping.")

        conn.close()
        print(f"[m007] Migration complete on {dialect}.")
    finally:
        if own_db:
            db.close()


if __name__ == "__main__":
    run()
