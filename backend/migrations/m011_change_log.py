"""
Migration 011 — Change log, confirmations, suggestions, notes (Rebuild Stage 2).

New tables:
  change_log       — every assistant/parser/tool write, with before/after snapshots for undo
  pending_actions  — proposed deletes awaiting explicit confirmation
  suggestions      — low-confidence extractions accepted with one tap
  notes            — markdown notes (book / bible / journal / inbox / general)

SQLite + Postgres. Idempotent. No drops. RLS enabled on the new tables (Postgres).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from database import engine
from migrations._helpers import dialect, create_table_if_missing, create_index, enable_rls

TAG = "m011"

_TABLES = {
    "change_log": """
CREATE TABLE IF NOT EXISTS change_log (
    id          INTEGER PRIMARY KEY {autoincrement},
    created_at  DATETIME,
    batch_id    VARCHAR NOT NULL,
    source      VARCHAR NOT NULL DEFAULT 'assistant',
    table_name  VARCHAR NOT NULL,
    record_id   INTEGER,
    action      VARCHAR NOT NULL,
    before_json TEXT,
    after_json  TEXT,
    summary     VARCHAR,
    undone_at   DATETIME
)
""",
    "pending_actions": """
CREATE TABLE IF NOT EXISTS pending_actions (
    id           INTEGER PRIMARY KEY {autoincrement},
    created_at   DATETIME,
    thread_id    VARCHAR,
    kind         VARCHAR NOT NULL DEFAULT 'delete',
    table_name   VARCHAR NOT NULL,
    record_id    INTEGER NOT NULL,
    summary      VARCHAR,
    payload_json TEXT,
    status       VARCHAR NOT NULL DEFAULT 'pending',
    expires_at   DATETIME,
    resolved_at  DATETIME,
    batch_id     VARCHAR
)
""",
    "suggestions": """
CREATE TABLE IF NOT EXISTS suggestions (
    id           INTEGER PRIMARY KEY {autoincrement},
    created_at   DATETIME,
    kind         VARCHAR NOT NULL,
    payload_json TEXT    NOT NULL,
    confidence   REAL,
    reason       VARCHAR,
    source_text  TEXT,
    status       VARCHAR NOT NULL DEFAULT 'pending',
    source       VARCHAR,
    batch_id     VARCHAR,
    resolved_at  DATETIME
)
""",
    "notes": """
CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY {autoincrement},
    title      VARCHAR NOT NULL,
    kind       VARCHAR NOT NULL DEFAULT 'general',
    body       TEXT    NOT NULL DEFAULT '',
    tags       VARCHAR,
    created_at DATETIME,
    updated_at DATETIME
)
""",
}


def run(db=None):
    conn = engine.connect()
    try:
        d = dialect(conn, TAG)
        with conn.begin():
            for t, sql in _TABLES.items():
                create_table_if_missing(conn, d, t, sql, TAG)
            create_index(conn, "ix_change_log_batch_id", "change_log", "batch_id")
            create_index(conn, "ix_change_log_created_at", "change_log", "created_at")
            create_index(conn, "ix_notes_kind", "notes", "kind")
            enable_rls(conn, d, list(_TABLES), TAG)
        print(f"[{TAG}] Change-log migration complete on {d}.")
    finally:
        conn.close()


if __name__ == "__main__":
    run()
