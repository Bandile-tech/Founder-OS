"""
Migration 027 — the personal finance ledger (Phase 7, Stage 2).

New tables:
  buckets              — the pots Bandile's own money sits in. No balance column: a balance
                         is always the sum of the bucket's transactions
  ledger_transactions  — one movement of money into or out of one bucket, in whole minor
                         units (ngwee). Income split across buckets is one row per bucket,
                         sharing entry_id
  allocation_rules     — how income of a type is split, in basis points

Nothing is seeded: the buckets and the percentages are Bandile's, and he sets them in
conversation. No money that was recorded before this migration is copied into the ledger.

SQLite + Postgres. Idempotent. No drops, no data changes. RLS enabled on all three tables
(Postgres).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from database import engine
from migrations._helpers import dialect, create_table_if_missing, create_index, enable_rls

TAG = "m027"

_TABLES = {
    "buckets": """
CREATE TABLE IF NOT EXISTS buckets (
    id             INTEGER PRIMARY KEY {autoincrement},
    name           VARCHAR NOT NULL,
    key            VARCHAR NOT NULL UNIQUE,
    is_default     BOOLEAN DEFAULT FALSE,
    is_remainder   BOOLEAN DEFAULT FALSE,
    is_unallocated BOOLEAN DEFAULT FALSE,
    created_at     DATETIME,
    updated_at     DATETIME
)""",
    "ledger_transactions": """
CREATE TABLE IF NOT EXISTS ledger_transactions (
    id           INTEGER PRIMARY KEY {autoincrement},
    entry_id     VARCHAR NOT NULL,
    created_at   DATETIME,
    occurred_at  DATE NOT NULL,
    direction    VARCHAR NOT NULL,
    amount_minor BIGINT NOT NULL,
    currency     VARCHAR NOT NULL DEFAULT 'ZMW',
    bucket_id    INTEGER NOT NULL REFERENCES buckets(id),
    category     VARCHAR,
    note         VARCHAR,
    income_type  VARCHAR,
    payment_id   INTEGER,
    revenue_id   INTEGER,
    batch_id     VARCHAR
)""",
    "allocation_rules": """
CREATE TABLE IF NOT EXISTS allocation_rules (
    id          INTEGER PRIMARY KEY {autoincrement},
    income_type VARCHAR NOT NULL,
    bucket_id   INTEGER NOT NULL REFERENCES buckets(id),
    pct         INTEGER NOT NULL,
    created_at  DATETIME
)""",
}


def run(db=None):
    conn = engine.connect()
    try:
        d = dialect(conn, TAG)
        with conn.begin():
            for table, sql in _TABLES.items():
                create_table_if_missing(conn, d, table, sql, TAG)
            create_index(conn, "ix_ledger_transactions_entry_id", "ledger_transactions", "entry_id")
            create_index(conn, "ix_ledger_transactions_bucket_id", "ledger_transactions", "bucket_id")
            create_index(conn, "ix_ledger_transactions_occurred_at", "ledger_transactions", "occurred_at")
            create_index(conn, "ix_ledger_transactions_payment_id", "ledger_transactions", "payment_id")
            create_index(conn, "ix_ledger_transactions_revenue_id", "ledger_transactions", "revenue_id")
            create_index(conn, "ix_ledger_transactions_batch_id", "ledger_transactions", "batch_id")
            create_index(conn, "ix_allocation_rules_income_type", "allocation_rules", "income_type")
            enable_rls(conn, d, list(_TABLES), TAG)
        print(f"[{TAG}] Finance ledger tables ready on {d}.")
    finally:
        conn.close()


if __name__ == "__main__":
    run()
