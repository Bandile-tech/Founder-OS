"""
Migration 009 — Market Intelligence Agent tables.

Creates:
  research_projects      — one row per research run (objective, status, timestamps)
  research_findings      — structured opportunities surfaced by the agent
  opportunity_pipeline   — explicit promotions only (discovered → ... → rejected)
  research_memory_notes  — one lesson per row (slug, one-line summary, body)

SQLite / Postgres: CREATE TABLE IF NOT EXISTS on both dialects.
Postgres only:     ENABLE ROW LEVEL SECURITY on the four new tables (m008
                   convention — RLS on + no policies = anon access denied; the
                   backend's direct connection bypasses RLS).
Idempotent — safe to run multiple times.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text
from database import SessionLocal, engine


def _dialect(conn) -> str:
    name = getattr(conn.dialect, "name", None) or engine.dialect.name
    if name in ("sqlite", "postgresql"):
        return name
    raise RuntimeError(f"[m009] Unsupported dialect '{name}'.")


def _table_exists(conn, dialect: str, table: str) -> bool:
    if dialect == "postgresql":
        row = conn.execute(text("""
            SELECT COUNT(*) FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name   = :t
        """), {"t": table}).scalar()
        return bool(row)
    rows = conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name=:t"),
        {"t": table},
    ).fetchall()
    return bool(rows)


_TABLES_SQL = {
    "research_projects": """
CREATE TABLE IF NOT EXISTS research_projects (
    id           INTEGER PRIMARY KEY {autoincrement},
    title        VARCHAR  NOT NULL,
    objective    TEXT     NOT NULL,
    status       VARCHAR  NOT NULL DEFAULT 'running',
    error        TEXT,
    created_at   DATETIME,
    completed_at DATETIME
)
""",
    "research_findings": """
CREATE TABLE IF NOT EXISTS research_findings (
    id                  INTEGER PRIMARY KEY {autoincrement},
    project_id          INTEGER NOT NULL REFERENCES research_projects (id) ON DELETE CASCADE,
    problem             TEXT    NOT NULL,
    industry            VARCHAR,
    customer_segment    VARCHAR,
    persona             VARCHAR,
    discovery           TEXT,
    market_analysis     TEXT,
    founder_fit         TEXT,
    evidence            TEXT,
    scores              TEXT,
    overall_score       REAL,
    first_customer_path TEXT,
    status              VARCHAR NOT NULL DEFAULT 'surfaced',
    notes               TEXT,
    created_at          DATETIME
)
""",
    "opportunity_pipeline": """
CREATE TABLE IF NOT EXISTS opportunity_pipeline (
    id          INTEGER PRIMARY KEY {autoincrement},
    finding_id  INTEGER NOT NULL UNIQUE REFERENCES research_findings (id) ON DELETE CASCADE,
    stage       VARCHAR NOT NULL DEFAULT 'discovered',
    notes       TEXT,
    promoted_at DATETIME,
    updated_at  DATETIME
)
""",
    "research_memory_notes": """
CREATE TABLE IF NOT EXISTS research_memory_notes (
    id               INTEGER PRIMARY KEY {autoincrement},
    slug             VARCHAR NOT NULL UNIQUE,
    summary          VARCHAR NOT NULL,
    content          TEXT    NOT NULL,
    times_reinforced INTEGER DEFAULT 1,
    created_at       DATETIME,
    updated_at       DATETIME
)
""",
}

_INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS ix_research_findings_project_id "
    "ON research_findings (project_id)",
    "CREATE INDEX IF NOT EXISTS ix_research_findings_status "
    "ON research_findings (status)",
    "CREATE INDEX IF NOT EXISTS ix_opportunity_pipeline_stage "
    "ON opportunity_pipeline (stage)",
]


def run(db=None):
    own_db = db is None
    if own_db:
        db = SessionLocal()
    try:
        conn = engine.connect()
        dialect = _dialect(conn)
        autoincrement = "AUTOINCREMENT" if dialect == "sqlite" else ""

        with conn.begin():
            for table, sql in _TABLES_SQL.items():
                if _table_exists(conn, dialect, table):
                    print(f"[m009] {table} already present — skipping.")
                    continue
                print(f"[m009] Creating {table} on {dialect} …")
                conn.execute(text(sql.format(autoincrement=autoincrement)))
            for idx_sql in _INDEX_SQL:
                conn.execute(text(idx_sql))

            if dialect == "postgresql":
                for table in _TABLES_SQL:
                    conn.execute(text(
                        f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY"
                    ))
                    print(f"[m009]   RLS enabled: {table}")

        conn.close()
        print(f"[m009] Market intelligence migration complete on {dialect}.")
    finally:
        if own_db:
            db.close()


if __name__ == "__main__":
    run()
