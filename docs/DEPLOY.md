# Deploying Founder OS (Render + Supabase)

One host. Render runs the FastAPI backend (Python 3.12) against Supabase Postgres, and the same
service serves the frontend at `/app/` (`/` redirects there). The frontend is `frontend/index.html`
plus `frontend/assets/`, `frontend/icons/` and `frontend/manifest.json`; the page calls the API
on its own origin, so there is no second host, no CORS set-up and nothing else to deploy.
There is no Netlify site any more (`netlify.toml` was removed).

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
| 4 | `FOUNDER_OS_ORIGINS` | leave unset — the app and the API share one origin. Set it only if some other origin must call the API | CORS |
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
4. Nothing else to deploy: the frontend ships with the backend and is live at `$API/app/`.

## 4. Checks after deploying

Run these against the live URL (`$API`), with `$TOKEN` from the login call:

```bash
curl -s $API/health                     # {"ok":true,"ai_configured":true,...}
curl -s -o /dev/null -w "%{http_code}\n" $API/todos          # 401 — the API is locked
curl -s -o /dev/null -w "%{http_code} %{redirect_url}\n" $API/   # 307 …/app/
curl -s -o /dev/null -w "%{http_code}\n" $API/app/assets/orb.png # 200 — no token needed
curl -s $API/app/manifest.json | head -c 200                     # the web app manifest, no token
curl -s -o /dev/null -w "%{http_code}\n" $API/app/icons/apple-touch-icon.png  # 200
curl -s -X POST $API/auth/login -H 'Content-Type: application/json' -d '{"password":"…"}'
curl -s -H "Authorization: Bearer $TOKEN" $API/briefing/today | head -c 400
curl -s -H "Authorization: Bearer $TOKEN" $API/syllabus | head -c 200
curl -s -H "Authorization: Bearer $TOKEN" $API/usage/summary
```

Then in the browser, at `$API/app/`: log in, confirm the Command Centre
renders the brief with its scene and orb images, send one typed
message to the assistant ("what's due today?"), tick a brief item and press Undo, open the
Syllabus page and import the tracker CSV, and start a Live session and say one sentence.
Check `GET /usage/summary` afterwards: every call should be logged.

On a phone, open `$API/app/` and use **Add to Home Screen** (Safari) or **Install app** (Chrome):
it installs as "Founder OS" with the triangle icon and opens standalone, without browser chrome.

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

Browser smoke tests (login, brief item complete + undo, syllabus advance) and the layout
checks with a contact sheet of every page at 1280×720, 1536×864, 1920×1080 and 390×844
(`tests/e2e/out/contact-sheet.jpg`):

```bash
node tests/e2e/smoke.js
node tests/e2e/screens.js
```

Both need the `playwright` npm package; set `PW_CHANNEL=msedge` (or `chrome`) to use an
installed browser instead of Playwright's Chromium.
