"""
Migration 013 — Syllabus model and daily briefing (Rebuild Stage 4).

New tables:
  syllabus_points     — one row per subject/paper/topic with the four-state status,
                        unique on (subject_code, paper, topic_code)
  briefing_summaries  — cached LLM summary per day (at most one call per day)

SQLite + Postgres. Idempotent. No drops. RLS enabled on the new tables (Postgres).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from database import engine
from migrations._helpers import dialect, create_table_if_missing, create_index, enable_rls

TAG = "m013"

_SYLLABUS_POINTS = """
CREATE TABLE IF NOT EXISTS syllabus_points (
    id               INTEGER PRIMARY KEY {autoincrement},
    subject_code     VARCHAR NOT NULL,
    subject_name     VARCHAR NOT NULL,
    level            VARCHAR,
    paper            VARCHAR,
    topic_code       VARCHAR NOT NULL,
    title            VARCHAR NOT NULL,
    status           VARCHAR NOT NULL DEFAULT 'not_started',
    past_paper_score REAL,
    last_revised     DATE,
    weakness_notes   TEXT,
    updated_at       DATETIME
)
"""

_BRIEFING_SUMMARIES = """
CREATE TABLE IF NOT EXISTS briefing_summaries (
    id            INTEGER PRIMARY KEY {autoincrement},
    day           DATE NOT NULL UNIQUE,
    summary       TEXT NOT NULL DEFAULT '',
    greeting_line VARCHAR,
    model         VARCHAR,
    created_at    DATETIME
)
"""


def run(db=None):
    conn = engine.connect()
    try:
        d = dialect(conn, TAG)
        with conn.begin():
            created = []
            if create_table_if_missing(conn, d, "syllabus_points", _SYLLABUS_POINTS, TAG):
                created.append("syllabus_points")
            if create_table_if_missing(conn, d, "briefing_summaries", _BRIEFING_SUMMARIES, TAG):
                created.append("briefing_summaries")
            create_index(conn, "ix_syllabus_points_subject", "syllabus_points", "subject_code")
            # Postgres treats NULLs as distinct in unique indexes, so paper is coalesced.
            if d == "postgresql":
                from sqlalchemy import text
                conn.execute(text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_syllabus_points_key "
                    "ON syllabus_points (subject_code, COALESCE(paper, ''), topic_code)"
                ))
            else:
                create_index(conn, "ux_syllabus_points_key", "syllabus_points",
                             "subject_code, COALESCE(paper, ''), topic_code", unique=True)
            enable_rls(conn, d, created, TAG)
        print(f"[{TAG}] Syllabus/briefing migration complete on {d}.")
    finally:
        conn.close()


if __name__ == "__main__":
    run()
