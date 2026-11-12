"""
Migration 021 — Integrations (Phase 6C).

New tables:
  integrations     — one row per provider; tokens in secret_json (never serialised)
  calendar_events  — a cache of the calendar, unique per (provider, external_id)
  email_messages   — snippets of email from known people only; bodies are never stored

Column added:
  outreach_messages.replied_at  — set when a reply to a sent outreach email arrives

SQLite + Postgres. Idempotent. No drops. RLS enabled on the new tables (Postgres).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from database import engine
from migrations._helpers import (dialect, add_column_if_missing, create_table_if_missing,
                                 create_index, enable_rls)

TAG = "m021"

_TABLES = {
    "integrations": """
CREATE TABLE IF NOT EXISTS integrations (
    id            INTEGER PRIMARY KEY {autoincrement},
    provider      VARCHAR NOT NULL UNIQUE,
    status        VARCHAR NOT NULL DEFAULT 'disconnected',
    account_label VARCHAR,
    config_json   TEXT,
    secret_json   TEXT,
    scopes        VARCHAR,
    connected_at  DATETIME,
    last_sync_at  DATETIME,
    last_error    TEXT,
    created_at    DATETIME,
    updated_at    DATETIME
)""",
    "calendar_events": """
CREATE TABLE IF NOT EXISTS calendar_events (
    id                INTEGER PRIMARY KEY {autoincrement},
    provider          VARCHAR DEFAULT 'google',
    calendar_id       VARCHAR,
    external_id       VARCHAR NOT NULL,
    title             VARCHAR,
    description       TEXT,
    location          VARCHAR,
    start_at          DATETIME,
    end_at            DATETIME,
    all_day           BOOLEAN DEFAULT FALSE,
    start_date        DATE,
    status            VARCHAR DEFAULT 'confirmed',
    html_link         VARCHAR,
    created_by_us     BOOLEAN DEFAULT FALSE,
    remote_updated_at DATETIME,
    synced_at         DATETIME,
    deleted_at        DATETIME
)""",
    "email_messages": """
CREATE TABLE IF NOT EXISTS email_messages (
    id                  INTEGER PRIMARY KEY {autoincrement},
    message_id          VARCHAR NOT NULL UNIQUE,
    thread_key          VARCHAR,
    direction           VARCHAR NOT NULL,
    from_addr           VARCHAR,
    to_addr             VARCHAR,
    subject             VARCHAR,
    snippet             TEXT,
    received_at         DATETIME,
    lead_id             INTEGER,
    client_id           INTEGER,
    contact_id          INTEGER,
    outreach_message_id INTEGER,
    processed_at        DATETIME,
    created_at          DATETIME
)""",
}


def run(db=None):
    conn = engine.connect()
    try:
        d = dialect(conn, TAG)
        with conn.begin():
            for table, sql in _TABLES.items():
                create_table_if_missing(conn, d, table, sql, TAG)
            add_column_if_missing(conn, d, "outreach_messages", "replied_at", "TIMESTAMP", TAG)
            create_index(conn, "ux_calendar_events_provider_external", "calendar_events",
                         "provider, external_id", unique=True)
            create_index(conn, "ix_calendar_events_start_date", "calendar_events", "start_date")
            create_index(conn, "ix_email_messages_received", "email_messages", "received_at")
            enable_rls(conn, d, list(_TABLES), TAG)
        print(f"[{TAG}] Integrations migration complete on {d}.")
    finally:
        conn.close()


if __name__ == "__main__":
    run()
