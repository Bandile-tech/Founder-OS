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
| 3 | `FOUNDER_OS_SECRET` | 32+ random chars (`openssl rand -hex 32`) | keeps sessions valid if the password changes, and means nothing signed is derived from the password |
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

## Checking an upgrade on PostgreSQL before a merge

`backend/tests/postgres/README.md` has the steps: build a database from main's schema with
production-like rows, run the branch's startup chain on it, and check the backfills, the
unchanged rows and row-level security. The same folder says how to run the whole suite on
Postgres.

## Phase 7 — what to set, and the one-time setup

Nothing here is needed for the app to start. Each block switches one thing on.

**What to set on Render, in one place.** Each is explained in its block below. The first two
were the ones missed or easy to miss in the live check (20 November 2026).

| Var | Needed for | If it is missing |
|---|---|---|
| `JOBS_TOKEN` | The schedule. 32 or more random characters, the same value on Render and as the repository secret | `POST /jobs/tick` answers 503: the workflow fails and nothing is sent while the app is closed |
| `VAULT_REPO` | The live vault. The vault repository as `owner/name` | A push from any repository that knows the secret is accepted, and the daily export is skipped |
| `GITHUB_WEBHOOK_SECRET` | The live vault. 32 or more random characters, the same value in the GitHub webhook | `POST /vault/webhook` answers 503 |
| `VAULT_WRITE_TOKEN` | The daily export into `Founder OS/` | The export is skipped |
| `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY` | Notifications | No device can subscribe |
| `ANTHROPIC_API_KEY`, `MONTHLY_LLM_CAP_USD`, `QUIET_HOURS`, `VAULT_MIN_SCORE` | Optional | Defaults apply |

**Migrations.** m026 to m030 only add tables and columns. On the Render log after the deploy,
look for `[m026] Model router ready on postgresql: 10 model(s) and 12 route(s) seeded.`,
`[m027] Finance ledger tables ready`, `[m028] Vault export table ready`, `[m029] Web Push
migration complete`, `[m030] Vault changes table ready` and `Schema OK`. m026 fills `llm_usage.provider` with `openai` for rows
that have none, once. m029 moves the notification settings to the three daily wakes, once,
and only where a value is still the one it shipped with.

**Models (optional).** `ANTHROPIC_API_KEY` to use Claude or to fall back to it.
`MONTHLY_LLM_CAP_USD` for a monthly cap. `FALLBACK_MAP` only to change the seeded pairs.

**The schedule.** `JOBS_TOKEN` has to be set in two places with the same value (32 or more
random characters, for example `openssl rand -hex 32`):
1. On Render, as the environment variable `JOBS_TOKEN`. Without it, or with fewer than 16
   characters, `POST /jobs/tick` answers 503 whatever the workflow sends.
2. In the repository: Settings → Secrets and variables → Actions → add `JOBS_TOKEN`, and
   `FOUNDER_OS_URL` (the Render URL, no trailing slash).

The workflow `Founder OS jobs tick` then runs at 05:45, 12:30 and 19:45 Lusaka time.
Run it once by hand (Actions → Founder OS jobs tick → Run workflow) and check that the home
screen shows "Last scheduled run". A run that fails with 503 means the Render side is missing;
401 means the two values differ.

**Notifications on your phone.**
1. On any machine with the backend's Python environment: `python tools/vapid_keys.py`. Set the
   two lines it prints on Render as `VAPID_PUBLIC_KEY` and `VAPID_PRIVATE_KEY`. Optionally set
   `VAPID_SUBJECT` to `mailto:` your address, and `QUIET_HOURS` (for example `22:00-05:30`).
2. On the phone, open the app. On iPhone: Share → Add to Home Screen, then open it from the
   Home Screen (notifications do not work from a Safari tab).
3. Settings → Notifications → This device → On, and allow notifications when asked.
4. Tap Send test.

**The live vault.**
1. In Obsidian: install the community plugin "Git" (Obsidian Git). In its settings set
   "Auto commit-and-sync interval" to 10 minutes and turn on "Pull on startup". The vault then
   commits and pushes on its own every ten minutes.

   Obsidian Git pushes with the Git installed on that machine, so Git there needs a working
   GitHub sign-in. Use Git Credential Manager (it ships with Git for Windows: the first push
   opens a browser sign-in and the credential is kept by Windows). Do not put a token in the
   remote URL: the remote should read `https://github.com/<owner>/<vault>.git` and nothing
   more (`git remote -v` in the vault folder shows it). To check, run `git push` once in the
   vault folder from a terminal: it must finish without asking for anything. If the sign-in
   is broken, Obsidian keeps committing but nothing is pushed, GitHub has nothing to send,
   and Founder OS never hears of the edits; this is what failed the first live vault test.
2. On Render set `GITHUB_WEBHOOK_SECRET` to 32 or more random characters, and `VAULT_REPO` to
   the vault repository as `owner/name` (for example `Bandile-tech/bandile-vault`). `VAULT_REPO`
   is what makes the webhook ignore pushes from any other repository, and the daily export
   needs it too.
3. On GitHub, in the vault repository: Settings → Webhooks → Add webhook. Payload URL:
   `https://<your Render host>/vault/webhook`. Content type: `application/json`. Secret: the
   same value as `GITHUB_WEBHOOK_SECRET`. Events: "Just the push event". Save; GitHub sends a
   ping, which should show a green tick (answer `{"ok": true, "pong": true}`).
4. For the daily export into `Founder OS/`: GitHub → Settings → Developer settings →
   Personal access tokens → Fine-grained tokens → Generate. Repository access: "Only select
   repositories", the vault repository alone. Permissions: Contents, "Read and write". Nothing
   else. Set it on Render as `VAULT_WRITE_TOKEN`.
5. Settings → Integrations → Obsidian vault should read "Vault synced … · updates on every
   push". The Sync button is the manual fallback.
6. To check the whole chain: edit a note, wait for Obsidian's next push (or run "Git: Commit
   and sync" from its command palette), then ask the assistant "what did I add to my vault
   today". If the note is not listed, look in this order: the vault repository on GitHub (is
   the commit there? if not, it is the sign-in in step 1); the webhook's Recent Deliveries on
   GitHub (a red delivery shows the status Founder OS answered); Settings → Integrations for
   the last sync and its error.

`VAULT_MIN_SCORE` (optional, default `0.35`) is the weakest meaning match a vault search still
counts. Lower it if the assistant says "nothing relevant" about notes that are there; raise it
if unrelated notes come back.

**Rollback.** Redeploying the previous commit is safe: the new tables and columns are left in
place and the old code does not read them. The notification times m029 changed stay changed;
set them back in Settings if wanted.
