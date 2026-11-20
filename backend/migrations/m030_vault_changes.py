"""
Migration 030 — what changed in the vault (Phase 7 live check).

New table:
  vault_changes — one row each time a sync finds a vault file added, modified or removed:
                  the path, the title, which of the three, when Founder OS saw it and which
                  sync saw it. ``vault_recent`` reads it to answer "what did I add to my
                  vault today". Rows older than ninety days are dropped by the next sync.

SQLite + Postgres. Idempotent. No drops, no data changes. RLS enabled (Postgres).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from database import engine
from migrations._helpers import dialect, create_table_if_missing, create_index, enable_rls

TAG = "m030"

_TABLES = {
    "vault_changes": """
CREATE TABLE IF NOT EXISTS vault_changes (
    id         INTEGER PRIMARY KEY {autoincrement},
    file_path  VARCHAR NOT NULL,
    file_title VARCHAR NOT NULL,
    change     VARCHAR NOT NULL,
    changed_at DATETIME NOT NULL,
    source     VARCHAR
)""",
}


def run(db=None):
    conn = engine.connect()
    try:
        d = dialect(conn, TAG)
        with conn.begin():
            for table, sql in _TABLES.items():
                create_table_if_missing(conn, d, table, sql, TAG)
            create_index(conn, "ix_vault_changes_changed_at", "vault_changes", "changed_at")
            enable_rls(conn, d, list(_TABLES), TAG)
        print(f"[{TAG}] Vault changes table ready on {d}.")
    finally:
        conn.close()


if __name__ == "__main__":
    run()
