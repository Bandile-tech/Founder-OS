"""
Migration 014 — Market intelligence v2 (Phase 5A).

Columns added:
  research_projects    cost_usd, llm_calls, objective_id
  research_findings    recommendation, times_seen, dismissed_reason, reviewed_at
  opportunity_pipeline next_action, due_date

New table:
  research_objectives  — standing research objectives that recur every cadence_days

Backfill (Python, row by row): every finding with a scores payload and no recommendation
gets one from the weighted scoring. ``overall_score`` is left as it was.

SQLite + Postgres. Idempotent. No drops. RLS enabled on the new table (Postgres).
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text

from database import engine
from migrations._helpers import (dialect, add_column_if_missing, create_table_if_missing,
                                 create_index, enable_rls, table_exists)

TAG = "m014"

_RESEARCH_OBJECTIVES = """
CREATE TABLE IF NOT EXISTS research_objectives (
    id                INTEGER PRIMARY KEY {autoincrement},
    objective         TEXT NOT NULL,
    cadence_days      INTEGER NOT NULL DEFAULT 14,
    enabled           BOOLEAN DEFAULT TRUE,
    max_opportunities INTEGER DEFAULT 3,
    last_run_at       DATETIME,
    last_project_id   INTEGER,
    created_at        DATETIME,
    updated_at        DATETIME
)
"""

_COLUMNS = [
    ("research_projects", "cost_usd", "REAL"),
    ("research_projects", "llm_calls", "INTEGER"),
    ("research_projects", "objective_id", "INTEGER"),
    ("research_findings", "recommendation", "VARCHAR"),
    ("research_findings", "times_seen", "INTEGER DEFAULT 1"),
    ("research_findings", "dismissed_reason", "TEXT"),
    ("research_findings", "reviewed_at", "TIMESTAMP"),
    ("opportunity_pipeline", "next_action", "TEXT"),
    ("opportunity_pipeline", "due_date", "DATE"),
]


def _backfill_recommendations(conn, d: str) -> int:
    if not table_exists(conn, d, "research_findings"):
        return 0
    from market_intel import scoring
    rows = conn.execute(text(
        "SELECT id, scores FROM research_findings WHERE recommendation IS NULL AND scores IS NOT NULL"
    )).fetchall()
    done = 0
    for fid, raw in rows:
        try:
            scores = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(scores, dict):
            continue
        conn.execute(text("UPDATE research_findings SET recommendation = :r WHERE id = :id"),
                     {"r": scoring.recommendation(scoring.overall(scores)), "id": fid})
        done += 1
    return done


def run(db=None):
    conn = engine.connect()
    try:
        d = dialect(conn, TAG)
        with conn.begin():
            create_table_if_missing(conn, d, "research_objectives", _RESEARCH_OBJECTIVES, TAG)
            for table, column, type_sql in _COLUMNS:
                add_column_if_missing(conn, d, table, column, type_sql, TAG)
            create_index(conn, "ix_research_objectives_enabled", "research_objectives", "enabled")
            enable_rls(conn, d, ["research_objectives"], TAG)
            filled = _backfill_recommendations(conn, d)
            if filled:
                print(f"[{TAG}] Backfilled recommendation on {filled} finding(s).")
        print(f"[{TAG}] Market intelligence v2 migration complete on {d}.")
    finally:
        conn.close()


if __name__ == "__main__":
    run()
