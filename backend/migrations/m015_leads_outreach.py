"""
Migration 015 — Leads and outreach (Phase 5B).

New tables:
  leads              — prospects, with status, segment and qualification
  lead_imports       — one row per CSV import
  outreach_messages  — drafted, sent and received messages per lead

Columns added:
  todos.lead_id, todos.client_id  — follow-ups and client tasks are ordinary todos

SQLite + Postgres. Idempotent. No drops. RLS enabled on the new tables (Postgres).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from database import engine
from migrations._helpers import (dialect, add_column_if_missing, create_table_if_missing,
                                 create_index, enable_rls)

TAG = "m015"

_TABLES = {
    "leads": """
CREATE TABLE IF NOT EXISTS leads (
    id                  INTEGER PRIMARY KEY {autoincrement},
    name                VARCHAR NOT NULL,
    company             VARCHAR,
    role                VARCHAR,
    email               VARCHAR,
    phone               VARCHAR,
    whatsapp            VARCHAR,
    linkedin_url        VARCHAR,
    website             VARCHAR,
    location            VARCHAR,
    segment             VARCHAR,
    source              VARCHAR NOT NULL DEFAULT 'manual',
    status              VARCHAR NOT NULL DEFAULT 'new',
    qualification_score INTEGER,
    qualification_json  TEXT,
    opportunity_id      INTEGER,
    client_id           INTEGER,
    notes               TEXT,
    last_contact_at     DATETIME,
    next_follow_up_at   DATE,
    created_at          DATETIME,
    updated_at          DATETIME
)""",
    "lead_imports": """
CREATE TABLE IF NOT EXISTS lead_imports (
    id           INTEGER PRIMARY KEY {autoincrement},
    filename     VARCHAR,
    created      INTEGER DEFAULT 0,
    updated      INTEGER DEFAULT 0,
    skipped      INTEGER DEFAULT 0,
    total_rows   INTEGER DEFAULT 0,
    mapping_json TEXT,
    segment      VARCHAR,
    created_at   DATETIME
)""",
    "outreach_messages": """
CREATE TABLE IF NOT EXISTS outreach_messages (
    id                INTEGER PRIMARY KEY {autoincrement},
    lead_id           INTEGER NOT NULL,
    channel           VARCHAR NOT NULL,
    direction         VARCHAR NOT NULL DEFAULT 'out',
    subject           VARCHAR,
    body              TEXT NOT NULL,
    status            VARCHAR NOT NULL DEFAULT 'draft',
    version           INTEGER DEFAULT 1,
    supersedes_id     INTEGER,
    brief             TEXT,
    pending_action_id INTEGER,
    approved_at       DATETIME,
    sent_at           DATETIME,
    external_id       VARCHAR,
    error             TEXT,
    created_by        VARCHAR NOT NULL DEFAULT 'assistant',
    created_at        DATETIME,
    updated_at        DATETIME
)""",
}


def run(db=None):
    conn = engine.connect()
    try:
        d = dialect(conn, TAG)
        with conn.begin():
            for table, sql in _TABLES.items():
                create_table_if_missing(conn, d, table, sql, TAG)
            add_column_if_missing(conn, d, "todos", "lead_id", "INTEGER", TAG)
            add_column_if_missing(conn, d, "todos", "client_id", "INTEGER", TAG)
            create_index(conn, "ix_leads_status", "leads", "status")
            create_index(conn, "ix_leads_segment", "leads", "segment")
            create_index(conn, "ix_leads_email", "leads", "email")
            create_index(conn, "ix_outreach_lead_id", "outreach_messages", "lead_id")
            create_index(conn, "ix_outreach_status", "outreach_messages", "status")
            enable_rls(conn, d, list(_TABLES), TAG)
        print(f"[{TAG}] Leads/outreach migration complete on {d}.")
    finally:
        conn.close()


if __name__ == "__main__":
    run()
