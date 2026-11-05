"""
Migration 010 — Assistant core (Rebuild Stage 1).

chat_messages:
  + thread_id        VARCHAR  — server-side conversation thread; every existing row is
                                merged into the 'default' thread
  + tool_calls_json  TEXT     — assistant turns that called tools
  + tool_call_id     VARCHAR  — tool-result rows (role = 'tool')
  + changes_json     TEXT     — change-log entries attached to an assistant reply
ai_memory:
  + kind             VARCHAR  — fact | decision | preference
  + tags             VARCHAR
New tables:
  conversation_summaries — rolling summary of turns that fell out of the history window
  llm_usage              — one row per OpenAI call (model, tokens, estimated cost)

SQLite + Postgres. Idempotent. No drops. RLS enabled on the new tables (Postgres).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text
from database import engine
from migrations._helpers import (
    dialect, add_column_if_missing, create_table_if_missing, create_index, enable_rls,
)

TAG = "m010"
DEFAULT_THREAD_ID = "default"

_CONVERSATION_SUMMARIES = """
CREATE TABLE IF NOT EXISTS conversation_summaries (
    id                         INTEGER PRIMARY KEY {autoincrement},
    thread_id                  VARCHAR NOT NULL UNIQUE,
    summary                    TEXT    NOT NULL DEFAULT '',
    covers_through_message_id  INTEGER,
    message_count              INTEGER DEFAULT 0,
    updated_at                 DATETIME
)
"""

_LLM_USAGE = """
CREATE TABLE IF NOT EXISTS llm_usage (
    id                   INTEGER PRIMARY KEY {autoincrement},
    created_at           DATETIME,
    model                VARCHAR NOT NULL,
    purpose              VARCHAR,
    input_tokens         INTEGER DEFAULT 0,
    cached_input_tokens  INTEGER DEFAULT 0,
    output_tokens        INTEGER DEFAULT 0,
    estimated_cost_usd   REAL    DEFAULT 0,
    duration_ms          INTEGER
)
"""


def run(db=None):
    conn = engine.connect()
    try:
        d = dialect(conn, TAG)
        with conn.begin():
            added_thread = add_column_if_missing(conn, d, "chat_messages", "thread_id", "VARCHAR", TAG)
            add_column_if_missing(conn, d, "chat_messages", "tool_calls_json", "TEXT", TAG)
            add_column_if_missing(conn, d, "chat_messages", "tool_call_id", "VARCHAR", TAG)
            add_column_if_missing(conn, d, "chat_messages", "changes_json", "TEXT", TAG)
            # Merge every pre-existing browser session into the single default thread.
            conn.execute(text(
                "UPDATE chat_messages SET thread_id = :t WHERE thread_id IS NULL"
            ), {"t": DEFAULT_THREAD_ID})
            create_index(conn, "ix_chat_messages_thread_id", "chat_messages", "thread_id")
            if added_thread:
                print(f"[{TAG}] Existing chat rows merged into thread '{DEFAULT_THREAD_ID}'.")

            add_column_if_missing(conn, d, "ai_memory", "kind", "VARCHAR", TAG)
            add_column_if_missing(conn, d, "ai_memory", "tags", "VARCHAR", TAG)

            create_table_if_missing(conn, d, "conversation_summaries", _CONVERSATION_SUMMARIES, TAG)
            create_table_if_missing(conn, d, "llm_usage", _LLM_USAGE, TAG)
            create_index(conn, "ix_llm_usage_created_at", "llm_usage", "created_at")
            enable_rls(conn, d, ["conversation_summaries", "llm_usage"], TAG)
        print(f"[{TAG}] Assistant core migration complete on {d}.")
    finally:
        conn.close()


if __name__ == "__main__":
    run()
