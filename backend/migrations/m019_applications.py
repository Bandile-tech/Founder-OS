"""
Migration 019 — Applications (Phase 6A).

New tables:
  applications              — one row per application, with its deadline and where the
                              deadline came from (user | document | unknown)
  application_requirements  — the checklist: missing → in_progress → ready → submitted
  application_gaps          — profile gaps from a review: open | addressed | accepted_risk |
                              superseded

SQLite + Postgres. Idempotent. No drops. RLS enabled on the new tables (Postgres).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from database import engine
from migrations._helpers import dialect, create_table_if_missing, create_index, enable_rls

TAG = "m019"

_TABLES = {
    "applications": """
CREATE TABLE IF NOT EXISTS applications (
    id                      INTEGER PRIMARY KEY {autoincrement},
    name                    VARCHAR NOT NULL,
    institution             VARCHAR,
    programme               VARCHAR,
    kind                    VARCHAR NOT NULL DEFAULT 'university',
    deadline                DATE,
    deadline_source         VARCHAR,
    decision_date           DATE,
    status                  VARCHAR NOT NULL DEFAULT 'researching',
    priority                INTEGER DEFAULT 2,
    portal_url              VARCHAR,
    notes                   TEXT,
    requirements_updated_at DATETIME,
    gaps_reviewed_at        DATETIME,
    created_at              DATETIME,
    updated_at              DATETIME
)""",
    "application_requirements": """
CREATE TABLE IF NOT EXISTS application_requirements (
    id             INTEGER PRIMARY KEY {autoincrement},
    application_id INTEGER NOT NULL,
    title          VARCHAR NOT NULL,
    kind           VARCHAR NOT NULL DEFAULT 'other',
    status         VARCHAR NOT NULL DEFAULT 'missing',
    due_date       DATE,
    due_source     VARCHAR,
    details        TEXT,
    todo_id        INTEGER,
    note_id        INTEGER,
    sort_order     INTEGER DEFAULT 0,
    created_at     DATETIME,
    updated_at     DATETIME
)""",
    "application_gaps": """
CREATE TABLE IF NOT EXISTS application_gaps (
    id               INTEGER PRIMARY KEY {autoincrement},
    application_id   INTEGER NOT NULL,
    gap              VARCHAR NOT NULL,
    severity         VARCHAR NOT NULL,
    evidence         TEXT,
    suggested_action TEXT,
    status           VARCHAR NOT NULL DEFAULT 'open',
    review_batch     VARCHAR,
    created_at       DATETIME,
    updated_at       DATETIME
)""",
}


def run(db=None):
    conn = engine.connect()
    try:
        d = dialect(conn, TAG)
        with conn.begin():
            for table, sql in _TABLES.items():
                create_table_if_missing(conn, d, table, sql, TAG)
            create_index(conn, "ix_application_requirements_app", "application_requirements", "application_id")
            create_index(conn, "ix_application_gaps_app_status", "application_gaps", "application_id, status")
            enable_rls(conn, d, list(_TABLES), TAG)
        print(f"[{TAG}] Applications migration complete on {d}.")
    finally:
        conn.close()


if __name__ == "__main__":
    run()
