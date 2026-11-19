"""
Migration 028 — the live vault (Phase 7, Stage 3).

New table:
  vault_exports — per note, what was last written to the vault folder ``Founder OS/``:
                  the path, the hash of the text and the file's blob sha on GitHub. It lets
                  the daily export skip notes that have not changed and remove the file of
                  a note that was renamed or deleted.

When the vault was last synced is kept on the ``integrations`` row for ``vault`` (created
on first use), not in a table of its own.

SQLite + Postgres. Idempotent. No drops, no data changes. RLS enabled (Postgres).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from database import engine
from migrations._helpers import dialect, create_table_if_missing, enable_rls

TAG = "m028"

_TABLES = {
    "vault_exports": """
CREATE TABLE IF NOT EXISTS vault_exports (
    id           INTEGER PRIMARY KEY {autoincrement},
    note_id      INTEGER NOT NULL UNIQUE,
    path         VARCHAR NOT NULL,
    content_hash VARCHAR NOT NULL,
    sha          VARCHAR,
    exported_at  DATETIME
)""",
}


def run(db=None):
    conn = engine.connect()
    try:
        d = dialect(conn, TAG)
        with conn.begin():
            for table, sql in _TABLES.items():
                create_table_if_missing(conn, d, table, sql, TAG)
            enable_rls(conn, d, list(_TABLES), TAG)
        print(f"[{TAG}] Vault export table ready on {d}.")
    finally:
        conn.close()


if __name__ == "__main__":
    run()
