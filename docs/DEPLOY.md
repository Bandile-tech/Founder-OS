# Deploying the rebuild (Render + Supabase + Netlify)

The backend runs on Render (FastAPI, Python 3.12) against Supabase Postgres. The frontend is
one file; it can be served by the backend at `/` (same origin, simplest) or by Netlify.

## 1. Before merging

- [ ] Suite green locally: `cd backend && python -m pytest -q` (no `OPENAI_API_KEY` set).
- [ ] Startup chain green on SQLite: `python -c "import asyncio, main; asyncio.run(main.startup())"`.
- [ ] Optional but recommended: the same against a throwaway Postgres (see "Verifying against
      Postgres" below). The migrations were verified on PostgreSQL 16 against both a fresh
      database and one built from main's pre-rebuild schema.
- [ ] Take a Supabase backup (Database → Backups) or `pg_dump` of production. The migrations add
      only; nothing is dropped. The backup is the rollback.

## 2. Environment variables on Render — set in this order

Set these **before** the first deploy of the branch, so the first boot comes up locked and
configured.

| # | Var | Value | Why first |
|---|---|---|---|
| 1 | `DATABASE_URL` | Supabase Postgres URL (already set) | migrations run at boot |
| 2 | `FOUNDER_OS_PASSWORD` | a long passphrase | without it the API boots open |
| 3 | `FOUNDER_OS_SECRET` | 32+ random chars (`openssl rand -hex 32`) | keeps sessions valid if the password changes |
| 4 | `FOUNDER_OS_ORIGINS` | `https://<your-netlify-site>.netlify.app` (omit if the backend serves the frontend) | CORS |
| 5 | `OPENAI_API_KEY` | the key | assistant, parser, embeddings, voice |
| 6 | `ASSISTANT_MODEL` | `gpt-5.4-mini` (default) | cost |
| 7 | `ZMW_PER_USD` | current rate, e.g. `27` | revenue scoreboard |
| 8 | `REALTIME_MODEL` | `gpt-realtime-2.1-mini` (default) | live voice |
| 9 | `VAULT_REPO_URL` | already set, with the token | vault sync |
| — | optional | `ASSISTANT_HISTORY_TURNS`, `ASSISTANT_SUMMARY_BATCH`, `INTENT_CONFIDENCE_MIN`, `EMBEDDING_MODEL`, `REALTIME_VOICE`, `LOGIN_MAX_FAILURES`, `LOGIN_LOCK_SECONDS`, `LLM_PRICING_JSON` | defaults are fine |

Render's start command stays `uvicorn main:app --host 0.0.0.0 --port $PORT` from `backend/`.
Python version: 3.12 (`PYTHON_VERSION=3.12.x`); the pinned requirements do not build on 3.13.

## 3. Merge and deploy

1. Open a pull request from `claude/rebuild-spec-review-vfrg4o` into `main`; merge it.
2. Render deploys `main`. Watch the deploy log for, in order:
   `[m010] …`, `[m011] …`, `[m012] … Backfilled due_date on N todos`, `[m012] Copied N books…`,
   `[m013] …`, `[startup] Schema OK: all ORM columns present in live database.`
   The RLS lines (`RLS enabled: …`) should appear for the ten new tables.
3. If the log shows `Schema drift detected`, the boot refused to serve: see Rollback.
4. If the frontend is on Netlify, deploy `frontend/index.html` (it points at the Render URL
   unless served from localhost).

## 4. Checks after deploying

Run these against the live URL (`$API`), with `$TOKEN` from the login call:

```bash
curl -s $API/health                     # {"ok":true,"ai_configured":true,...}
curl -s -o /dev/null -w "%{http_code}\n" $API/todos          # 401 — the API is locked
curl -s -X POST $API/auth/login -H 'Content-Type: application/json' -d '{"password":"…"}'
curl -s -H "Authorization: Bearer $TOKEN" $API/briefing/today | head -c 400
curl -s -H "Authorization: Bearer $TOKEN" $API/syllabus | head -c 200
curl -s -H "Authorization: Bearer $TOKEN" $API/usage/summary
```

Then in the browser: log in, confirm the Command Centre renders the brief, send one typed
message to the assistant ("what's due today?"), tick a brief item and press Undo, open the
Syllabus page and import the tracker CSV, and start a Live session and say one sentence.
Check `GET /usage/summary` afterwards: every call should be logged.

Also confirm in Supabase (Table editor → any new table → RLS) that row-level security shows as
enabled on `change_log`, `notes`, `syllabus_points`, `embeddings`, `llm_usage` and the rest.

## 5. Rollback on Render

The migrations are additive, so the old code runs against the migrated database.

1. Render → the service → **Manual Deploy → Deploy a previous commit** (or **Rollback** on the
   deploy that last worked). Pick the last `main` deploy before the merge.
2. The old build boots against the migrated schema without complaint: it ignores the new
   columns and tables. Nothing needs to be reverted in the database.
3. If you must undo the data changes too (you shouldn't need to): restore the Supabase backup
   taken in step 1. The only data the migrations wrote are `chat_messages.thread_id`,
   `revenue.currency`, `todos.due_date` and the copied notes; dropping those columns/tables by
   hand is possible but the backup is cleaner.
4. Revert the merge commit on `main` so the next deploy does not re-apply the branch.

## 6. Verifying against Postgres locally

```bash
# any Postgres 16; e.g. a throwaway database "founder_check"
export DATABASE_URL=postgresql://user:pass@127.0.0.1:5432/founder_check
cd backend && python -c "import asyncio, main; asyncio.run(main.startup())"
# whole test suite on Postgres (drops/recreates the schema per test — use a throwaway db)
TEST_DATABASE_URL=postgresql://user:pass@127.0.0.1:5432/founder_tests python -m pytest -q
```

Browser smoke tests (login, brief item complete + undo, syllabus advance):

```bash
node tests/e2e/smoke.js
```
