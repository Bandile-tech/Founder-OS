"""
Migration 008 — Enable Row-Level Security on all public tables.

SQLite  (local dev): skipped — RLS is a Postgres-only concept.
Postgres (Supabase): ALTER TABLE ... ENABLE ROW LEVEL SECURITY for every
                     table in the project.

Effect: no RLS policies are created. RLS on + no policies = PostgREST/anon
key access fully denied. The backend's direct SQLAlchemy connection (as the
postgres superuser) bypasses RLS entirely and continues working unchanged.

Idempotent — safe to run multiple times. Re-enabling already-enabled RLS
is a no-op in Postgres.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text
from database import engine

TABLES = [
    "weekly_targets",
    "daily_logs",
    "log_impacts",
    "weekly_target_snapshots",
    "ai_memory",
    "habits",
    "annual_targets",
    "kpi_snapshots",
    "roadmap_tasks",
    "todos",
    "chat_messages",
    "bible_entries",
    "daily_bible_log",
    "books",
    "social_scores",
    "clients",
    "revenue",
    "subjects",
    "topics",
    "subtopics",
    "daily_health",
    "lifts",
    "lift_logs",
    "weekly_health",
    "prop_firm_accounts",
    "backtest_trades",
    "live_trades",
    "documents",
    "document_chunks",
    "non_negotiables",
    "reading_plans",
    "reading_plan_entries",
    "cold_archive_chunks",
]


def run():
    dialect = engine.dialect.name

    if dialect == "sqlite":
        print("[m008] SQLite detected — RLS is Postgres-only. Skipping.")
        return

    if dialect != "postgresql":
        print(f"[m008] Unknown dialect '{dialect}' — skipping.")
        return

    print(f"[m008] Enabling RLS on {len(TABLES)} tables (Postgres)…")
    with engine.connect() as conn:
        with conn.begin():
            for table in TABLES:
                sql = f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY"
                conn.execute(text(sql))
                print(f"[m008]   RLS enabled: {table}")

    print(f"[m008] Done — RLS enabled on all {len(TABLES)} tables.")
    print("[m008] No policies created — PostgREST/anon access is now fully blocked.")
    print("[m008] Backend direct connection (SQLAlchemy postgres user) is unaffected.")


if __name__ == "__main__":
    run()
