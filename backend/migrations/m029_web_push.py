"""
Migration 029 — proactive delivery over Web Push (Phase 7, Stage 4).

New table:
  push_subscriptions — one row per device that turned notifications on: the endpoint at
                       the browser's push service and the two keys a message is encrypted with

Column added:
  notifications.target — the view a tap on the notification opens ("home", "applications",
                         "business:clients", …)

The notification settings row (id 1) is moved to the three daily wakes, once, and only
where it still holds the value it shipped with (a value Bandile set himself is left alone):
  morning_time   06:00            → 05:45   (the morning wake; at 06:00 it would wait until 12:30)
  evening_time   not set          → 19:45   (the evening check, on the evening wake)
  channels       telegram         → telegram and push
The reminder days (30, 14, 7, 3 and 1 days out) are not touched.

SQLite + Postgres. Idempotent. No drops. RLS enabled on the new table (Postgres).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text

from database import engine
from migrations._helpers import (dialect, add_column_if_missing, create_table_if_missing, enable_rls,
                                 table_exists, claim_once)

TAG = "m029"

_TABLES = {
    "push_subscriptions": """
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id              INTEGER PRIMARY KEY {autoincrement},
    endpoint        VARCHAR NOT NULL UNIQUE,
    p256dh          VARCHAR NOT NULL,
    auth            VARCHAR NOT NULL,
    user_agent      VARCHAR,
    created_at      DATETIME,
    last_success_at DATETIME,
    last_error      VARCHAR,
    failures        INTEGER DEFAULT 0
)""",
}

# (column, the value it shipped with, the new value). NULL is written as None.
_SETTINGS = [
    ("morning_time", "06:00", "05:45"),
    ("evening_time", None, "19:45"),
    ("channels_json", '{"telegram": true}', '{"telegram": true, "push": true}'),
]


def run(db=None):
    conn = engine.connect()
    try:
        d = dialect(conn, TAG)
        with conn.begin():
            for table, sql in _TABLES.items():
                create_table_if_missing(conn, d, table, sql, TAG)
            add_column_if_missing(conn, d, "notifications", "target", "VARCHAR", TAG)
            if table_exists(conn, d, "notification_settings") and claim_once(conn, d, "m029:notification_defaults", TAG):
                for column, old, new in _SETTINGS:
                    where = f"{column} IS NULL" if old is None else f"{column} = :old"
                    params = {"new": new} if old is None else {"new": new, "old": old}
                    changed = conn.execute(text(
                        f"UPDATE notification_settings SET {column} = :new WHERE id = 1 AND {where}"), params)
                    if changed.rowcount:
                        print(f"[{TAG}] notification settings: {column} is now {new}")
            enable_rls(conn, d, list(_TABLES), TAG)
        print(f"[{TAG}] Web Push migration complete on {d}.")
    finally:
        conn.close()


if __name__ == "__main__":
    run()
