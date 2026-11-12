"""
Migration 016 — CRM (Phase 5C).

Columns added to clients: lead_id, currency, tags, health

New tables:
  contacts, deals, projects, payments, recurring_revenue, communications
  (projects carries every column the portfolio needs as well)

Backfills (Python, row by row, re-runnable):
  * clients.currency  — rows that predate the column get DEFAULT_CURRENCY (ZMW unless the
    variable says USD), the same value new rows get from the ORM. The column is added
    without a SQL default so those rows are the ones with NULL.
  * payments          — every revenue row with a client_id gets one paid payment
    (title 'Imported') in that revenue row's own currency, whatever the client's is,
    matched by revenue_id so a second run adds nothing.

SQLite + Postgres. Idempotent. No drops. RLS enabled on the new tables (Postgres).
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text

from database import engine
from migrations._helpers import (dialect, add_column_if_missing, create_table_if_missing,
                                 create_index, enable_rls, table_exists)

TAG = "m016"

_TABLES = {
    "contacts": """
CREATE TABLE IF NOT EXISTS contacts (
    id         INTEGER PRIMARY KEY {autoincrement},
    client_id  INTEGER NOT NULL,
    name       VARCHAR NOT NULL,
    role       VARCHAR,
    email      VARCHAR,
    phone      VARCHAR,
    whatsapp   VARCHAR,
    is_primary BOOLEAN DEFAULT FALSE,
    notes      TEXT,
    created_at DATETIME,
    updated_at DATETIME
)""",
    "deals": """
CREATE TABLE IF NOT EXISTS deals (
    id             INTEGER PRIMARY KEY {autoincrement},
    client_id      INTEGER NOT NULL,
    title          VARCHAR NOT NULL,
    stage          VARCHAR NOT NULL DEFAULT 'lead',
    amount         REAL,
    currency       VARCHAR DEFAULT 'ZMW',
    probability    INTEGER,
    expected_close DATE,
    won_at         DATETIME,
    lost_reason    TEXT,
    notes          TEXT,
    project_id     INTEGER,
    created_at     DATETIME,
    updated_at     DATETIME
)""",
    "projects": """
CREATE TABLE IF NOT EXISTS projects (
    id                INTEGER PRIMARY KEY {autoincrement},
    title             VARCHAR NOT NULL,
    kind              VARCHAR NOT NULL DEFAULT 'client',
    client_id         INTEGER,
    deal_id           INTEGER,
    status            VARCHAR NOT NULL DEFAULT 'active',
    priority          INTEGER DEFAULT 3,
    expected_value    REAL,
    currency          VARCHAR DEFAULT 'ZMW',
    effort_hours_week REAL,
    start_date        DATE,
    due_date          DATE,
    delivered_at      DATETIME,
    next_action       TEXT,
    blocked_reason    TEXT,
    last_activity_at  DATETIME,
    health            VARCHAR DEFAULT 'green',
    notes             TEXT,
    created_at        DATETIME,
    updated_at        DATETIME
)""",
    "payments": """
CREATE TABLE IF NOT EXISTS payments (
    id           INTEGER PRIMARY KEY {autoincrement},
    client_id    INTEGER NOT NULL,
    deal_id      INTEGER,
    project_id   INTEGER,
    title        VARCHAR,
    amount       REAL NOT NULL,
    currency     VARCHAR NOT NULL DEFAULT 'ZMW',
    status       VARCHAR NOT NULL DEFAULT 'due',
    due_date     DATE,
    paid_at      DATE,
    revenue_id   INTEGER,
    recurring_id INTEGER,
    period_start DATE,
    notes        TEXT,
    created_at   DATETIME,
    updated_at   DATETIME
)""",
    "recurring_revenue": """
CREATE TABLE IF NOT EXISTS recurring_revenue (
    id           INTEGER PRIMARY KEY {autoincrement},
    client_id    INTEGER NOT NULL,
    title        VARCHAR NOT NULL,
    amount       REAL NOT NULL,
    currency     VARCHAR NOT NULL DEFAULT 'ZMW',
    "interval"   VARCHAR NOT NULL DEFAULT 'monthly',
    day_of_month INTEGER DEFAULT 1,
    next_due     DATE NOT NULL,
    active       BOOLEAN DEFAULT TRUE,
    ended_at     DATE,
    created_at   DATETIME,
    updated_at   DATETIME
)""",
    "communications": """
CREATE TABLE IF NOT EXISTS communications (
    id                  INTEGER PRIMARY KEY {autoincrement},
    client_id           INTEGER,
    contact_id          INTEGER,
    lead_id             INTEGER,
    channel             VARCHAR NOT NULL,
    direction           VARCHAR NOT NULL,
    summary             TEXT NOT NULL,
    occurred_at         DATETIME NOT NULL,
    outreach_message_id INTEGER,
    source              VARCHAR DEFAULT 'conversation',
    created_at          DATETIME
)""",
}

_CLIENT_COLUMNS = [
    ("lead_id", "INTEGER"),
    ("currency", "VARCHAR"),              # no SQL default: NULL marks the rows to backfill
    ("tags", "VARCHAR"),
    ("health", "VARCHAR DEFAULT 'green'"),
]

_INDEXES = [
    ("ix_contacts_client", "contacts", "client_id"),
    ("ix_deals_client", "deals", "client_id"),
    ("ix_deals_stage", "deals", "stage"),
    ("ix_projects_status", "projects", "status"),
    ("ix_payments_client", "payments", "client_id"),
    ("ix_payments_status", "payments", "status"),
    ("ix_payments_due", "payments", "due_date"),
    ("ix_recurring_next_due", "recurring_revenue", "next_due"),
    ("ix_communications_client", "communications", "client_id"),
    ("ix_communications_occurred", "communications", "occurred_at"),
]


def _backfill_client_currency(conn, d: str) -> int:
    """Clients with no currency predate the column. They get the default currency
    (DEFAULT_CURRENCY, else ZMW): Bandile works in Kwacha, so nothing is assumed to be dollars."""
    if not table_exists(conn, d, "clients"):
        return 0
    from models import default_currency
    cur = default_currency()
    ids = [r[0] for r in conn.execute(text("SELECT id FROM clients WHERE currency IS NULL")).fetchall()]
    for cid in ids:
        conn.execute(text("UPDATE clients SET currency = :cur WHERE id = :id"), {"cur": cur, "id": cid})
    return len(ids)


def _backfill_payments(conn, d: str) -> int:
    """One paid payment per existing revenue row that names a client; idempotent by revenue_id.
    The payment is in the revenue row's own currency (a revenue row with none predates
    revenue.currency and was in dollars, as m012 recorded), never the client's."""
    if not (table_exists(conn, d, "revenue") and table_exists(conn, d, "payments")):
        return 0
    linked = {r[0] for r in conn.execute(
        text("SELECT revenue_id FROM payments WHERE revenue_id IS NOT NULL")).fetchall()}
    rows = conn.execute(text(
        "SELECT id, client_id, amount, currency, date FROM revenue WHERE client_id IS NOT NULL ORDER BY id"
    )).fetchall()
    now = datetime.utcnow()
    done = 0
    for rid, client_id, amount, currency, day in rows:
        if rid in linked:
            continue
        conn.execute(text(
            "INSERT INTO payments (client_id, title, amount, currency, status, paid_at, revenue_id, "
            "created_at, updated_at) VALUES (:c, 'Imported', :a, :cur, 'paid', :d, :r, :now, :now)"
        ), {"c": client_id, "a": amount, "cur": (currency or "USD").upper(), "d": day, "r": rid, "now": now})
        done += 1
    return done


def run(db=None):
    conn = engine.connect()
    try:
        d = dialect(conn, TAG)
        with conn.begin():
            for table, sql in _TABLES.items():
                create_table_if_missing(conn, d, table, sql, TAG)
            for column, type_sql in _CLIENT_COLUMNS:
                add_column_if_missing(conn, d, "clients", column, type_sql, TAG)
            for name, table, columns in _INDEXES:
                create_index(conn, name, table, columns)
            # Partial unique index: manual payments (recurring_id NULL) never collide.
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_payments_recurring_period "
                "ON payments (recurring_id, period_start) WHERE recurring_id IS NOT NULL"
            ))
            enable_rls(conn, d, list(_TABLES), TAG)
            currencies = _backfill_client_currency(conn, d)
            payments = _backfill_payments(conn, d)
            if currencies:
                print(f"[{TAG}] Set the default currency on {currencies} existing client(s).")
            if payments:
                print(f"[{TAG}] Backfilled {payments} paid payment(s) from revenue.")
        print(f"[{TAG}] CRM migration complete on {d}.")
    finally:
        conn.close()


if __name__ == "__main__":
    run()
