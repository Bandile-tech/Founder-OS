# Session C — Obsidian Cold Archive Integration

## Overview

Pulls Bandile's Obsidian vault from GitHub, indexes markdown into the database, and exposes it as a new orchestrator tool `query_cold_archive`. The vault is cold storage — never queried automatically.

## Architecture

```
GitHub (bandile-vault) ──git pull──► /tmp/bandile-vault (local clone)
                                           │
                                    vault_sync.py
                                    index_vault()
                                           │
                                    cold_archive_chunks table (SQLite/Postgres)
                                           │
                                    search_vault()
                                           │
                               orchestrator tool: query_cold_archive
                               (only called on explicit user command)
```

## Files Changed / Created

| File | Change |
|------|--------|
| `backend/models.py` | Added `ColdArchiveChunk` model |
| `backend/migrations/m007_cold_archive.py` | Idempotent table migration |
| `backend/vault_sync.py` | `sync_vault`, `index_vault`, `search_vault` |
| `backend/main.py` | `/vault/sync`, `/vault/status`, `/vault/search` endpoints; startup sync; APScheduler job |
| `backend/orchestrator_tools.py` | Tool 8 — `query_cold_archive` |
| `context/core/orchestrator_prompt.md` | QUERY_COLD_ARCHIVE strict rules appended |
| `frontend/index.html` | Vault card in War Room page |
| `tests/test_vault.py` | 7 new tests |

## Environment Variables

| Var | Default | Description |
|-----|---------|-------------|
| `VAULT_REPO_URL` | `https://github.com/Bandile-tech/bandile-vault.git` | Git remote URL |
| `VAULT_LOCAL_PATH` | `/tmp/bandile-vault` | Local clone path |
| `VAULT_SYNC_INTERVAL_HOURS` | `6` | Scheduled sync interval |

## Database Schema

```sql
CREATE TABLE cold_archive_chunks (
    id          INTEGER PRIMARY KEY,
    file_path   VARCHAR NOT NULL,   -- e.g. "journal/2026-06.md"
    file_title  VARCHAR NOT NULL,   -- filename without extension
    content     TEXT    NOT NULL,   -- chunk text (≈600 chars)
    chunk_index INTEGER NOT NULL,
    word_count  INTEGER,
    last_synced DATETIME,
    created_at  DATETIME
);
```

## Chunk Strategy

- Chunk size: 600 characters
- Overlap: 10% (same as `_chunk_text` in `main.py`)
- Upsert: delete all chunks for a file path, re-insert on every sync
- Skips: `.git/`, `.obsidian/` directories

## Search Strategy

- Case-insensitive keyword search (SQLite `LIKE`, Postgres `ILIKE`)
- Title matches returned first (secondary sort by chunk_index)
- Default limit: 8 results

## Orchestrator Tool — STRICT GUARD

The tool description and orchestrator prompt both enforce:
- Only fires when user explicitly says: "look in my vault", "check my notes", "search my second brain", "do I have anything about X in my archive", "find in my obsidian"
- Never called autonomously during conversation, brain dumps, or query responses
- The vault is cold storage — passive, never proactively read

## Scheduled Sync

APScheduler (already in `requirements.txt`) runs `sync_vault` every `VAULT_SYNC_INTERVAL_HOURS` hours. The startup sync is non-blocking — if git clone fails (no network, private repo), the error is logged and the server continues.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/vault/sync` | Manual trigger — clones/pulls and indexes |
| `GET` | `/vault/status` | `{last_synced, chunk_count, file_count, repo_url}` |
| `GET` | `/vault/search?q=` | Keyword search, returns top 8 chunks |

## Tests (tests/test_vault.py)

1. `test_index_vault_creates_chunks` — temp dir with 2 .md files → chunks in DB
2. `test_index_vault_skips_obsidian_folder` — .md in `.obsidian/` → not indexed
3. `test_search_vault_returns_matches` — seed chunks, search, verify returned
4. `test_search_vault_title_match_first` — title match returned before body match
5. `test_search_vault_case_insensitive` — "Proverbs" seeded, "proverbs" found
6. `test_query_cold_archive_tool` — tool returns formatted results
7. `test_vault_sync_graceful_on_bad_repo` — invalid URL → no exception, error in dict

All tests mock `subprocess.run` — no real network calls.
