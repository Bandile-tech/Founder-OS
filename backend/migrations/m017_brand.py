"""
Migration 017 — Personal-brand analytics (Phase 5D).

New tables:
  social_accounts  — one row per connected platform account
  content_posts    — posts and videos, unique per (account_id, external_id)
  content_metrics  — append-only metric snapshots per post
  account_metrics  — append-only follower and view snapshots per account
  content_ideas    — the idea backlog and the content calendar
  brand_insights   — cached weekly insight text, unique per (week_start, platform)

No backfill. SQLite + Postgres. Idempotent. No drops. RLS enabled on the new tables.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from database import engine
from migrations._helpers import dialect, create_table_if_missing, create_index, enable_rls

TAG = "m017"

_TABLES = {
    "social_accounts": """
CREATE TABLE IF NOT EXISTS social_accounts (
    id             INTEGER PRIMARY KEY {autoincrement},
    platform       VARCHAR NOT NULL,
    handle         VARCHAR,
    external_id    VARCHAR,
    access         VARCHAR NOT NULL DEFAULT 'api_key',
    config_json    TEXT,
    status         VARCHAR NOT NULL DEFAULT 'connected',
    last_synced_at DATETIME,
    last_error     TEXT,
    created_at     DATETIME,
    updated_at     DATETIME
)""",
    "content_posts": """
CREATE TABLE IF NOT EXISTS content_posts (
    id          INTEGER PRIMARY KEY {autoincrement},
    account_id  INTEGER NOT NULL,
    external_id VARCHAR NOT NULL,
    url         VARCHAR,
    title       VARCHAR,
    kind        VARCHAR,
    posted_at   DATETIME,
    topic_tags  VARCHAR,
    idea_id     INTEGER,
    created_at  DATETIME,
    updated_at  DATETIME
)""",
    "content_metrics": """
CREATE TABLE IF NOT EXISTS content_metrics (
    id           INTEGER PRIMARY KEY {autoincrement},
    post_id      INTEGER NOT NULL,
    captured_at  DATETIME NOT NULL,
    views        INTEGER,
    likes        INTEGER,
    comments     INTEGER,
    shares       INTEGER,
    saves        INTEGER,
    impressions  INTEGER,
    watch_time_s INTEGER,
    source       VARCHAR,
    raw_json     TEXT
)""",
    "account_metrics": """
CREATE TABLE IF NOT EXISTS account_metrics (
    id          INTEGER PRIMARY KEY {autoincrement},
    account_id  INTEGER NOT NULL,
    captured_at DATETIME NOT NULL,
    followers   INTEGER,
    following   INTEGER,
    posts_count INTEGER,
    total_views INTEGER,
    source      VARCHAR,
    raw_json    TEXT
)""",
    "content_ideas": """
CREATE TABLE IF NOT EXISTS content_ideas (
    id             INTEGER PRIMARY KEY {autoincrement},
    title          VARCHAR NOT NULL,
    angle          TEXT,
    platform       VARCHAR,
    format         VARCHAR,
    status         VARCHAR NOT NULL DEFAULT 'idea',
    scheduled_for  DATE,
    posted_post_id INTEGER,
    source         VARCHAR DEFAULT 'conversation',
    score          INTEGER,
    created_at     DATETIME,
    updated_at     DATETIME
)""",
    "brand_insights": """
CREATE TABLE IF NOT EXISTS brand_insights (
    id         INTEGER PRIMARY KEY {autoincrement},
    week_start DATE NOT NULL,
    platform   VARCHAR NOT NULL,
    text       TEXT NOT NULL,
    model      VARCHAR,
    created_at DATETIME
)""",
}


def run(db=None):
    conn = engine.connect()
    try:
        d = dialect(conn, TAG)
        with conn.begin():
            for table, sql in _TABLES.items():
                create_table_if_missing(conn, d, table, sql, TAG)
            create_index(conn, "ux_content_posts_account_external", "content_posts",
                         "account_id, external_id", unique=True)
            create_index(conn, "ix_content_metrics_post_captured", "content_metrics", "post_id, captured_at")
            create_index(conn, "ix_content_ideas_status_scheduled", "content_ideas", "status, scheduled_for")
            create_index(conn, "ux_brand_insights_week_platform", "brand_insights",
                         "week_start, platform", unique=True)
            enable_rls(conn, d, list(_TABLES), TAG)
        print(f"[{TAG}] Brand analytics migration complete on {d}.")
    finally:
        conn.close()


if __name__ == "__main__":
    run()
