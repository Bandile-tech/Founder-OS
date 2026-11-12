"""
Migration 022 — Notifications (Phase 6D).

New tables:
  notifications          — the outbox: queued, sent, failed, suppressed or cancelled
  notification_settings  — exactly one row (id=1), created here with the defaults

SQLite + Postgres. Idempotent. No drops. RLS enabled on both tables (Postgres).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text

from database import engine
from migrations._helpers import (dialect, add_column_if_missing, create_table_if_missing,
                                 create_index, enable_rls)

TAG = "m022"

_TABLES = {
    "notifications": """
CREATE TABLE IF NOT EXISTS notifications (
    id                INTEGER PRIMARY KEY {autoincrement},
    created_at        DATETIME,
    channel           VARCHAR NOT NULL DEFAULT 'telegram',
    kind              VARCHAR NOT NULL,
    title             VARCHAR,
    body              TEXT NOT NULL,
    dedupe_key        VARCHAR UNIQUE,
    send_at           DATETIME,
    status            VARCHAR NOT NULL DEFAULT 'queued',
    sent_at           DATETIME,
    error             TEXT,
    external_id       VARCHAR,
    agent_run_id      INTEGER,
    source            VARCHAR,
    suppressed_reason VARCHAR
)""",
    "notification_settings": """
CREATE TABLE IF NOT EXISTS notification_settings (
    id                 INTEGER PRIMARY KEY {autoincrement},
    enabled            BOOLEAN DEFAULT TRUE,
    morning_time       VARCHAR DEFAULT '06:00',
    evening_time       VARCHAR,
    quiet_start        VARCHAR DEFAULT '22:00',
    quiet_end          VARCHAR DEFAULT '05:00',
    alert_min_severity VARCHAR DEFAULT 'warn',
    daily_cap          INTEGER DEFAULT 12,
    channels_json      TEXT,
    deadline_days_json TEXT,
    muted_until        DATETIME,
    updated_at         DATETIME
)""",
}


def run(db=None):
    conn = engine.connect()
    try:
        d = dialect(conn, TAG)
        with conn.begin():
            for table, sql in _TABLES.items():
                create_table_if_missing(conn, d, table, sql, TAG)
            add_column_if_missing(conn, d, "notification_settings", "muted_until", "TIMESTAMP", TAG)
            create_index(conn, "ix_notifications_status_send_at", "notifications", "status, send_at")
            exists = conn.execute(text("SELECT 1 FROM notification_settings WHERE id = 1")).first()
            if exists is None:
                conn.execute(text(
                    "INSERT INTO notification_settings (id, enabled, morning_time, evening_time, quiet_start, "
                    "quiet_end, alert_min_severity, daily_cap, channels_json, deadline_days_json, updated_at) "
                    "VALUES (1, TRUE, '06:00', NULL, '22:00', '05:00', 'warn', 12, "
                    "'{\"telegram\": true}', '[30, 14, 7, 3, 1]', CURRENT_TIMESTAMP)"))
                print(f"[{TAG}] Created the notification settings row.")
            enable_rls(conn, d, list(_TABLES), TAG)
        print(f"[{TAG}] Notifications migration complete on {d}.")
    finally:
        conn.close()


if __name__ == "__main__":
    run()
