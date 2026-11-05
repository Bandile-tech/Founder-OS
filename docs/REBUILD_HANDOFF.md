# Founder OS — Rebuild hand-off (Stages 1–4)

Branch: `claude/rebuild-spec-review-vfrg4o`. One commit per stage. Suite: 391 tests, all
passing with **no `OPENAI_API_KEY` set**, under Python 3.12 with the pinned requirements.

The spec's "done when" for each stage holds. Stage 5 (real-time voice) follows in a
separate commit and has its own addendum at the end.

---

## 1. What was built, per stage

### Stage 1 — reliable assistant
- **`llm.py`** is the single gateway to OpenAI. Lazy client: the app boots, migrations
  run, every non-AI endpoint works with no key. AI endpoints return **503** with a clear
  message. All calls log to `llm_usage`.
- **`ASSISTANT_MODEL`** (default `gpt-5.4-mini`) drives the assistant, parser, summaries
  and briefing. `ORCHESTRATOR_MODEL` still works as a legacy fallback. The hardcoded
  `gpt-5.4` default is gone.
- **Per-tool error handling.** A tool that raises returns `{"error": ...}` to the model;
  the stream finishes, the reply is saved, and the SSE `tool_result` event carries
  `"error": true`.
- **Weekly-review crash fixed** (`RoadmapTask.title` → `task_id`).
- **Server-side conversation.** One thread (`default`) for every device. The browser's
  `session_id` is still accepted and stored on each row, never used to partition history.
  m010 merged all existing rows into the default thread.
- **Tool calls and results persist** (`chat_messages.tool_calls_json`, role `tool` rows with
  `tool_call_id`) and are replayed to the model; tool results are truncated to 1,500 chars on
  replay.
- **History budget.** Last `ASSISTANT_HISTORY_TURNS` (default 20) user turns verbatim, plus
  a rolling summary of older turns stored in `conversation_summaries`. The summary is
  refreshed only once `ASSISTANT_SUMMARY_BATCH` (default 10) messages have fallen out of
  the window, so it is not an LLM call every turn.
- **Long-term memory.** `remember` tool (fact / decision / preference) on `ai_memory`; the
  top 5 relevant memories are injected each turn as a **second** system message, so the main
  prompt and tool definitions stay byte-stable for prompt caching.
- Graceful failures in `/chat`, `/parse`, `/input`, `/proactive-brief`, `/weekly-ai-review`
  and the voice endpoints. A parser that returns invalid JSON raises instead of silently
  returning an empty payload.

### Stage 2 — change log, undo, confirmation, intent
- `change_log` records every write by the assistant, parser or tools with before/after
  snapshots. One assistant turn = one batch (the batch lives on the SQLAlchemy session,
  because Starlette drives SSE generators one step per worker thread and context variables do
  not survive between steps).
- Undo endpoints restore the before-state; a create is removed; a delete is re-inserted with
  its original id. Idempotent. Undoing a KPI snapshot reloads the in-memory KPI state.
- The orchestrator's final SSE event and the saved assistant message carry `changes`.
- **Deletes need confirmation**: `propose_delete` → pending action, deletes nothing;
  `confirm_action` only after an explicit yes; `cancel_action`. One-tap endpoints too.
- **Intent classification** in the parser (`intent`, `confidence` per `todos_add` item).
  Commitments → tasks; ideas → the `Inbox` note; discussion → nothing. Below
  `INTENT_CONFIDENCE_MIN` (default 0.7) → `suggestions`, written only on accept.
- `apply_parse_updates` is now **atomic**: one commit, full rollback on any failure, in-memory
  KPI state updated only after the commit.

### Stage 3 — brain-dump rework, notes, retrieval
- `todos_complete` is applied with fuzzy matching; ambiguity becomes `todo_complete`
  suggestions (one per candidate), no match is reported as `todos_complete_unmatched`.
- **Currency.** `revenue.currency` (USD / ZMW). The parser keeps the stated currency; "Got
  K500" stores 500 ZMW. `/revenue` and the $10K scoreboard convert with `ZMW_PER_USD`.
- **Trading removed** from the parser, the tools and the alert rules. `/trading/*` endpoints
  and tables untouched; no data deleted.
- **`todos.due_date`** (real DATE) added and backfilled in Python from the free-text `due`.
  Both columns are written going forward. Overdue is computed server-side.
- **Notes** (`notes` table + endpoints + tools). Reading progress appends dated entries to the
  book's note (created if missing); Bible readings append to the `Bible` note. Books and the
  Bible log were copied once into notes (idempotent; originals untouched).
- **Meaning-based retrieval.** One shared `embeddings` table (`text-embedding-3-small`, JSON
  vectors, cosine in Python, keyed by content hash so unchanged text is never re-embedded;
  vault re-index just moves pointers). Vault, notes and memory share it. Keyword fallback
  with no key. Vault sync embeds new/changed chunks after indexing.
- New tools: `complete_task`, `notes_search`, `notes_read`, `notes_append`, `notes_create`,
  `get_deadlines`, `set_exam_date`. Prompt: vault / notes / memory are searched whenever
  relevant, not only on command.

### Stage 4 — syllabus and briefing
- `syllabus_points` with four states; CSV importer; grouped GET; one-tap advance; PATCH.
- `update_syllabus` tool; `surface_weakest_topics` (status, staleness, notes, exam proximity).
  The assistant no longer reads `subjects/topics/subtopics` (tables kept; `/subjects/*`
  endpoints untouched for the old UI).
- `exam_dates` keyed on (subject_code, paper) with fallback to `subjects.exam_date`.
- `GET /briefing/today`: deterministic ranked items, progress, alert count, greeting line;
  optional LLM summary cached once per day.
- Alerts: `backtest_stalled` removed; `habit_drift_7d` now checks active `non_negotiables`;
  `zero_mastery_high_weight` replaced by `syllabus_weak_before_exam`. Alerts are inside the
  briefing (`alerts`, `alert_count`, and `category: "alert"` items).

### Not finished / deliberately left
- The market-intelligence agent still hardcodes `gpt-4o-mini` (agent management is out of
  scope). It uses the lazy client, so it no longer breaks boot.
- Legacy `/input` keyword router and `/chat` kept as-is apart from graceful failures.
- Embeddings for notes/memory are written when a key exists at write time; run
  `retrieval.index_notes(db)` / `index_memories(db)` once after setting the key to backfill
  (a `POST /vault/sync` embeds the vault). No endpoint for that backfill yet.

---

## 2. Endpoints — new or changed

All responses are JSON. Dates are ISO strings. `changes` entries have the shape
`{id, summary, table, action, record_id, batch_id, undone}`.

### Assistant
- `POST /orchestrator` `{text, session_id, context?}` → SSE `data:` events:
  - `{"type":"reasoning","content"}`
  - `{"type":"tool_call","tool","args"}`
  - `{"type":"tool_result","tool","summary","error": bool}`
  - `{"type":"error","content"}` (only when the model call fails; still followed by `final`)
  - `{"type":"final","content","changes":[...]}`
- `POST /chat`, `POST /parse`, `POST /input` unchanged shapes; 503 when AI unconfigured,
  502 on upstream failure (`detail` explains). `/parse` and `/input` responses now include
  `updates.changes`, `updates.batch_id`, `updates.suggestions`, `updates.ideas_inboxed`,
  `updates.discussion_skipped`, `updates.todos_completed`, `updates.todos_complete_unmatched`,
  `updates.reading_notes_appended`; `revenue_logged[]` items include `currency`.
- `GET /health` → `{ok, ai_configured, assistant_model}`.

### Changes / undo
- `GET /changes?limit=50&batch_id=` → `[{...change, source, created_at}]`
- `POST /changes/{id}/undo` → `{...change, already_undone: bool}`
- `POST /changes/batch/{batch_id}/undo` → `{batch_id, undone, already_undone, changes:[...]}`

### Pending actions (deletes)
- `GET /actions/pending` → `[{id, kind, table, record_id, summary, status, created_at, expires_at, requires_confirmation}]`
- `POST /actions/{id}/confirm` → `{...pending, executed: bool, change?|reason?}`
- `POST /actions/{id}/cancel` → `{...pending}`

### Suggestions
- `GET /suggestions?status=pending|accepted|dismissed|all` →
  `[{id, kind, payload, confidence, reason, status, created_at, source_text, text}]`
  kinds: `todo_add` (payload `{text, priority, category, due, roadmap_id}`), `inbox_add`
  (`{text}`), `todo_complete` (`{todo_id, text, fragment, score}`)
- `POST /suggestions/{id}/accept` → `{...suggestion, applied: bool, updates, changes}`
- `POST /suggestions/{id}/dismiss` → `{...suggestion}`

### Notes
- `GET /notes?kind=&q=&limit=` → `[{id, title, kind, tags[], created_at, updated_at, preview, score?}]`
- `GET /notes/{id}` → `{id, title, kind, tags[], body, created_at, updated_at}`
- `POST /notes` `{title, kind?, body?, tags?[]}` → 201 note
- `PATCH /notes/{id}` `{title?, kind?, body?, tags?[]}` → note
- `POST /notes/{id}/append` `{text, date?}` → note

### Revenue / scoreboard
- `GET /revenue` → `{total (raw sum, legacy), total_usd, by_currency{USD,ZMW}, zmw_per_usd, records[{..., currency}]}`
- `POST /revenue` accepts `currency` (default USD) → `{id, amount, currency}`
- `GET /scoreboard/ten-k` → adds `zmw_per_usd`; `ai_revenue_usd` is converted.
- `GET /todos` rows now include `due_date`; `POST /todos` sets it from `due`.

### Syllabus
- `GET /syllabus` →
  ```
  {"subjects":[{"subject_code","subject_name","level","exam_date",
                "papers":[{"paper","exam_date","progress":{total,pct,bulletproof_pct,counts{...}},
                           "points":[{id, subject_code, subject_name, level, paper, topic_code, title,
                                      status, colour, past_paper_score, last_revised, weakness_notes, updated_at}]}],
                "progress":{...}}],
   "progress":{...}}
  ```
- `POST /syllabus/import` multipart: `file`, `subject_code`, `subject_name`, `level?`,
  `papers?` ("P1,S1") → `{created, updated, skipped, total_rows, papers[]}`
- `POST /syllabus/{id}/advance` → point (status moves to the next state, wrapping)
- `PATCH /syllabus/{id}` `{status?, weakness_notes?, past_paper_score?, last_revised?, title?}` → point
- `GET /syllabus/weakest?limit=5` → `[{...point, score, reason, exam_days_away}]`

### Briefing
- `GET /briefing/today?refresh=0|1` →
  ```
  {"date","weekday","badge":"EXECUTION DAY|BUILD DAY|REVIEW DAY",
   "greeting":"Good morning|afternoon|evening",
   "greeting_line": "You're 26% through your November targets. Two items need your attention.",
   "summary": "3–5 sentences" | null, "summary_source":"llm|deterministic", "summary_cached": bool,
   "items":[{"id","title","category":"task|deadline|non_negotiable|exam|syllabus|target|alert",
             "due_label":"Today|Tomorrow|Due 7 Nov|2d overdue|19 days|CRITICAL",
             "due_date", "reason", "suggested_action", "target_type":"todo|habit|exam|syllabus_point|annual_target|alert",
             "target_id", "completable": bool, "score", ...}],
   "attention_count", "alert_count", "alerts":[{rule_id, severity, message}],
   "progress":{"academics": int|null, "business": int, "targets": int|null, "habits": int},
   "counts":{overdue, today, upcoming, undated, items, non_negotiables_done, non_negotiables_total, exams_upcoming},
   "exams":[{subject_code, subject, paper, exam_date, days_away}],
   "non_negotiables":[{key, label, done, habit_id}],
   "weakness_notes":[{syllabus_point_id, subject, paper, topic, note, date, status}]}
  ```
  A checkbox on a `completable` item calls the assistant's `complete_task` with
  `todo_id = target_id` (or `POST /todos/{id}/toggle`).

### Usage
- `GET /usage/summary?days=90` → `{days, total{calls,input_tokens,cached_input_tokens,output_tokens,estimated_cost_usd}, by_day[], by_month[], by_model[]}`

### Tool registry (for the UI's reference)
`route_brain_dump, get_dashboard_state, detect_off_track, synthesize_weekly_review,
surface_weakest_topics, query_war_room, add_todos, query_cold_archive,
run_market_research_agent, promote_research_opportunity, remember, propose_delete,
confirm_action, cancel_action, complete_task, notes_search, notes_read, notes_append,
notes_create, get_deadlines, set_exam_date, update_syllabus`

---

## 3. Migrations

Run automatically at startup, in order, before the schema check. All re-runnable; none drops
anything. RLS enabled on every new table on Postgres (m008 convention).

| Migration | Adds |
|---|---|
| m010 | `chat_messages.thread_id/tool_calls_json/tool_call_id/changes_json` (+ merge into `default`), `ai_memory.kind/tags`, `conversation_summaries`, `llm_usage` |
| m011 | `change_log`, `pending_actions`, `suggestions`, `notes` |
| m012 | `revenue.currency` (backfilled USD), `todos.due_date` (Python backfill), `embeddings`, `exam_dates`; one-off copy of books + Bible log into notes |
| m013 | `syllabus_points`, `briefing_summaries` |

**Postgres notes**
- All ADD COLUMN use `IF NOT EXISTS`; all tables `CREATE TABLE IF NOT EXISTS`.
- `todos.due_date` backfill is row-by-row in Python; no SQL cast, so non-ISO strings cannot
  fail the migration (they stay NULL).
- `syllabus_points` unique index uses `COALESCE(paper, '')` because Postgres treats NULLs as
  distinct.
- Fuzzy matching uses `lower()`/Python, not `ILIKE`, so SQLite and Postgres behave the same.
- Boolean literals are handled via the ORM, not raw SQL.
- Existing Postgres rows: `revenue.currency` is set to `USD` for every existing row. If any
  historic rows were actually Kwacha logged as a bare number, fix them by hand.

---

## 4. Env vars to set on Render

| Var | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | unset | Without it everything non-AI works; AI endpoints 503 |
| `ASSISTANT_MODEL` | `gpt-5.4-mini` | Assistant, parser, summaries, briefing summary |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | Vault / notes / memory embeddings |
| `ASSISTANT_HISTORY_TURNS` | `20` | Verbatim history window |
| `ASSISTANT_SUMMARY_BATCH` | `10` | Messages that must fall out of the window before the summary refreshes |
| `INTENT_CONFIDENCE_MIN` | `0.7` | Below this an extracted item becomes a suggestion |
| `ZMW_PER_USD` | `27.0` | Kwacha→dollar rate for the $10K scoreboard. **Set this.** |
| `LLM_PRICING_JSON` | built-in table | Override per-model prices for `llm_usage` cost estimates |
| `ORCHESTRATOR_MODEL` | — | Legacy; still honoured if `ASSISTANT_MODEL` is unset |

---

## 5. Known issues

- **Pricing table is an estimate.** `llm.py` ships default prices for gpt-5.4-mini / gpt-5.4 /
  gpt-4o-mini / embeddings; check against OpenAI's current pricing and override with
  `LLM_PRICING_JSON` if they differ. Costs are "estimated", never billed figures.
- **Streaming usage** relies on `stream_options={"include_usage": true}`; if the SDK/model
  doesn't return it, the call is logged with zero tokens (still counted as a call).
- **Test harness never fires startup.** `TestClient(app)` without a context manager does not
  run `on_event("startup")`, so the suite exercises migrations only through
  `test_startup_handler_runs_with_no_key`, which calls `main.startup()` directly against
  the local SQLite file. I also ran the startup chain by hand after each stage.
- **Requirements pins vs Python 3.13.** The pinned `pydantic==2.7.1` / `psycopg2-binary`
  will not build on 3.13; use 3.12 (as the spec says).
- `get_dashboard_state` no longer reports the trading gate; the old UI's dashboard reads the
  `/trading/*` endpoints directly, which are unchanged.
- The default thread means the frontend's `founderOS_session` id is informational only.

---

## 6. Stage 5 addendum — real-time voice

Committed separately so Stage 4 is never at risk.

### What was built
- **`POST /voice/realtime/session`** mints a short-lived client secret through OpenAI's
  `/v1/realtime/client_secrets` (TTL 600 s). The browser never sees the API key. The session
  config sent upstream carries the orchestrator prompt plus a voice addendum, the **same tool
  registry** (22 tools, converted to the realtime API's flat tool shape), server VAD with
  `interrupt_response`, input transcription, and the output voice.
  Response: `{client_secret:{value, expires_at}, model, voice, voice_session_id, calls_url,
  silence_timeout_seconds, thread_id}`. 503 with no key; 502 with OpenAI's own message if the
  session is rejected (e.g. an unknown model id).
- **`POST /voice/realtime/tool`** `{name, arguments, call_id?, voice_session_id?}` runs one
  function call through `orchestrator_tools.run_tool` inside a change batch — same change
  log, same delete-confirmation rules — and persists the assistant tool-call row and the
  tool-result row into the default thread. Returns `{call_id, name, result, summary, error,
  changes, output}`; `output` is the JSON string to hand back to the model.
- **`POST /voice/realtime/transcript`** `{role, text, voice_session_id?, changes?}` saves a
  transcript line into the default thread (so typed and spoken history are one history).
- **Frontend**: a `Live` button next to the mic. WebRTC peer connection, mic track with
  echo cancellation, remote audio element, data channel `oai-events`. Visible states:
  connecting / listening / thinking / speaking / acting, a running transcript under the
  command box, barge-in handled by server VAD (`speech_started` drops the partial line and
  returns to listening), tool calls bridged server-side then `function_call_output` +
  `response.create`, auto-stop after **2 minutes** of silence, and a Stop button. The old
  record → transcribe → speak loop on the mic button is untouched as the fallback.

### Env vars
| Var | Default | Purpose |
|---|---|---|
| `REALTIME_MODEL` | `gpt-realtime-mini` | Realtime model id — **see the caveat below** |
| `REALTIME_VOICE` | `marin` | Output voice |
| `REALTIME_TRANSCRIBE_MODEL` | `gpt-4o-mini-transcribe` | Input transcription model |
| `OPENAI_REALTIME_BASE` | `https://api.openai.com/v1/realtime` | Override for proxies |

### Caveat — not verified against OpenAI's docs
This build environment's network policy blocks `developers.openai.com` and
`platform.openai.com`, so the spec's instruction to "check the exact model ID in OpenAI's
docs" could not be carried out from here. Web search results (secondary sources) indicate:
- `gpt-realtime-mini` exists but is reported deprecated with shutdown on 20 Jan 2027;
- Azure lists `gpt-realtime-2.1-mini` as the current mini realtime model.
The default stays on `gpt-realtime-mini`; set `REALTIME_MODEL=gpt-realtime-2.1-mini` on
Render once you have confirmed it on OpenAI's model page. If the id is wrong, the session
endpoint returns 502 with OpenAI's message and the UI shows it in a toast — nothing else
breaks.

The same applies to the GA request/event shapes used (nested `session.audio.input.*`,
`/v1/realtime/calls` SDP exchange, `response.function_call_arguments.done`,
`conversation.item.input_audio_transcription.completed`,
`response.output_audio_transcript.*`). They follow OpenAI's GA Realtime API as documented
up to mid-2026; the frontend also accepts the older `response.audio_transcript.*` names.
Expect to do one live smoke test with a real key before relying on it.

### Not done
- No browser-level automated test of the WebRTC flow (no network, no mic in CI). The suite
  covers the three endpoints and asserts the frontend wiring.
- No "choose a voice by ear" UI; `REALTIME_VOICE` is an env var.
- Tool results from voice are not truncated before being sent back to the model (same
  as the typed path's live turn; only replay is truncated).

---

## 7. Stage 6 addendum — UI, security, clean-up

One commit per item (6.1 … 6.7), all on the same branch.

### 6.1 Login
- `FOUNDER_OS_PASSWORD` gates every route except `/health`, `/ping`, `/auth/*`, CORS
  preflights and the static frontend. Unset → the API is open (local dev) and startup logs a
  warning. **Set it on Render.**
- `POST /auth/login {password}` → `{auth_required, token, expires_at}`; `GET /auth/status` →
  `{auth_required, authenticated}`. Tokens are HMAC-signed and expire after
  `FOUNDER_OS_SESSION_DAYS` (30). Secret = `FOUNDER_OS_SECRET` or a hash of the password, so
  changing the password invalidates every session.
- Clients send `Authorization: Bearer <token>` (or `X-Founder-Token`). The frontend wraps
  `fetch` to add it and reopens the login gate on 401. Sign out lives in Settings.
- CORS: `FOUNDER_OS_ORIGINS` (comma-separated), default local dev origins only. **Set the
  Netlify origin** if the frontend is served from there.

### 6.2 Home screen (Command Centre)
Built on `GET /briefing/today` (shape in section 2). Checkboxes call the new
`POST /todos/{id}/complete` → `{id, done, completed_at, change}` (logged, undoable). Header
bell shows `alert_count` and lists `alerts` plus overdue tasks; the AI pill reads `/health`.
Only the Academics tile exists; it opens the syllabus page.

### 6.3 Changes and suggestions in the dock
The final SSE event's `changes` render with Undo / Undo all (`POST /changes/{id}/undo`,
`POST /changes/batch/{batch_id}/undo`). Pending `GET /suggestions` render with Accept
(`POST /suggestions/{id}/accept`) and Dismiss.

### 6.4 Syllabus tracker
`GET /syllabus` grouped rows, four colours, tap → `POST /syllabus/{id}/advance`. Import CSV
modal → `POST /syllabus/import` (multipart: file, subject_code, subject_name, level, papers).

### 6.5 Removed from the UI
Trading Desk, Aether Command (Business tab), Reading Queue, Bible Plan, the $10K September
hero band and countdown strip, hardcoded exam/regionals dates, browser notifications, the
hidden agent-alerts panel and client-side urgent flags, the header drift badge. **No backend
tables or endpoints were removed**; `/trading/*`, `/books`, `/reading-plans`, `/clients`,
`/revenue`, `/scoreboard/ten-k` still answer.

### 6.6 Escaping and cold starts
Every user-text interpolation into `innerHTML` goes through `escHtml`; `mdHtml` escapes raw
HTML before markdown and strips non-http link schemes. The 10-second timeout is gone: sync
and ping wait up to 90 s with a "waking backend" header state after 2.5 s.

### 6.7 Docs
`CLAUDE.md` rewritten; `ROADMAP.md` added with six phases (1–4 core built).

### Env vars added in Stage 6
| Var | Default | Purpose |
|---|---|---|
| `FOUNDER_OS_PASSWORD` | unset (open) | Login password — set in production |
| `FOUNDER_OS_SECRET` | derived from password | Token signing secret (set to keep sessions across password changes) |
| `FOUNDER_OS_SESSION_DAYS` | `30` | Token lifetime |
| `FOUNDER_OS_ORIGINS` | local dev origins | CORS allow-list |

### Known issues after Stage 6
- The command dock is slightly wider than a 390 px phone viewport (the Live button is
  clipped); it was like that before the Live button and needs a responsive pass.
- The old Dashboard page is kept as a second page; it still renders KPIs, todos and the
  radar from local state. Decide whether it stays once the Command Centre covers it.
- The Academic Roadmap (legacy subject/topic/subtopic) page is still in the nav; the
  assistant no longer reads those tables.
- No automated browser tests; the UI was checked with headless Chromium screenshots and
  click-throughs (home, suggestions accept → undo, syllabus tap-to-advance) during the build.
- Login has no rate limiting.

---

## 8. Stage 7 addendum — verification, review, gaps, smoke tests, runbook

### 7.1 Postgres verification (PostgreSQL 16 in the build container)
- **Fresh database**: the startup chain ran three times (create_all → m002…m013 → schema
  check), idempotent; every new endpoint was exercised against it (import, re-import,
  advance, parse with intent/currency/notes, complete, undo, batch undo, suggestion accept,
  propose/confirm delete, deadlines, exam date, update_syllabus, briefing).
- **Production mirror**: a database built with `main`'s pre-rebuild code and seeded with
  legacy rows (two browser sessions of chat, todos with ISO / dd-mm-yyyy / junk / null due
  text, revenue, a book, a Bible log entry, a legacy memory row). The branch's startup
  migrated it correctly: chat rows merged into `default` with session ids preserved,
  `due_date` backfilled for the two parseable rows and left NULL for the rest, revenue set
  to USD, the book and the Bible log copied into notes, the ten new tables created with
  sequences and `timestamp` columns. Second run: no-op.
- **Full suite on Postgres** (`TEST_DATABASE_URL`): first run 417 passed, 1 error — a lock
  timeout caused by a test leaking an open session (invisible on SQLite). Fixed the test and
  made the fixture terminate stale connections; see the final run result at the end.
- **Bugs found and fixed**:
  1. **RLS was not enabled on any of the ten new tables on Postgres.** The migrations only
     enabled it on tables they created, but `create_all` at import creates them first. Now
     every migration enables RLS on all its tables on every run (verified: 10/10).
  2. The migration DDL used SQLite types (`INTEGER PRIMARY KEY AUTOINCREMENT`, `DATETIME`);
     on Postgres it is now rewritten to `SERIAL` / `TIMESTAMP`, so a table created by the
     migration rather than `create_all` still gets a sequence.

### 7.2 Branch review (fresh-reviewer pass over the diff vs main)
Checked: token signing (HMAC-SHA256 over the payload, `compare_digest` on both the password
and the signature, expiry enforced, secret derived from the password unless
`FOUNDER_OS_SECRET` is set so a password change invalidates sessions), route protection
(everything except `/health`, `/ping`, `/auth/*`, OPTIONS, `/`, `/index.html`, `/app/`),
undo, and the migrations for data loss (all statements are additive; backfills only fill
NULLs; the notes copy only inserts).
Fixed:
- Undo is now **all-or-nothing per batch** and returns 409 (instead of a 500 or a half-undone
  batch) when a record is gone or, on Postgres, still referenced by a foreign key.
- The static-frontend exemption matched any path starting with `/app` (e.g. `/apples`);
  it now matches `/app` and `/app/…` only.
Not reviewed in this pass: the market-intelligence agent, vault git handling, the health and
timetable pages' JS, and the legacy `/input` keyword router.

### 7.3 Known gaps
- Login rate limiting: 5 failures per 5 minutes per address (X-Forwarded-For aware) locks the
  address for 15 minutes; 429 with `Retry-After`. In-memory, resets on restart.
  Env: `LOGIN_MAX_FAILURES`, `LOGIN_WINDOW_SECONDS`, `LOGIN_LOCK_SECONDS`.
- Dock no longer clips at phone width (badge hidden, controls shrink, bar fits 390 px).
- Dashboard and the legacy Academic Roadmap are out of the nav (pages still exist in the
  file; nothing links to them).

### 7.4 Smoke tests
`node tests/e2e/smoke.js` boots the backend on a throwaway SQLite database with a password,
seeds a task and the syllabus fixture through the API, then drives headless Chromium:
login gate (401 → gate → wrong password → in), brief checkbox complete → change log → undo
→ reopened, syllabus tap-to-advance → backend state. It found and fixed one bug: a 401
body was being parsed as sync data (`fetchWithTimeout` now rejects non-OK responses).

### 7.5 Realtime (docs reached with curl)
OpenAI's models index lists `gpt-realtime-2.1-mini` as the current mini realtime model
(and `gpt-realtime-2.1`); `gpt-realtime-mini` still has a page. **Default changed to
`gpt-realtime-2.1-mini`.** The WebRTC guide confirms the Stage 5 shapes: client secret via
`POST /v1/realtime/client_secrets` with `{"session": {"type": "realtime", "model", "audio":
{"output": {"voice"}}}}`, response `value`; browser posts the SDP offer to
`/v1/realtime/calls` with `Authorization: Bearer <ephemeral>` and
`Content-Type: application/sdp` (no query string — removed ours); data channel
`oai-events`; `function_call_output` + `response.create`; `input_audio_buffer.speech_*`;
`response.output_audio_transcript.*`; `turn_detection.interrupt_response`. The frontend also
treats `session.started` as ready. Not confirmed (API reference page returned 403):
`expires_after` on the client-secret request — if OpenAI rejects it, the 502 says so and the
field can be dropped.

### 7.6 Deploy runbook
`docs/DEPLOY.md`: env vars in order, merge steps, post-deploy checks, Render rollback.

### Postgres suite — final run
After the fixture fix the full suite was re-run on PostgreSQL 16 (`TEST_DATABASE_URL`):
**421 passed, 0 failed, 0 errors** (2 min 34 s). The same 421 pass on SQLite.

---

## 9. Stage 8 addendum — command centre visual pass, brief, stage and voice

### 8.1 Visual pass (vs `docs/design/command-centre.png`)
Two-column hero: greeting block and the brief/progress cards on the left, the assistant's orb
on the right. Deeper glass cards with cyan edge glow, larger type scale, Academics /
Business / Targets gradient bars, header with logo mark, short date and live time. The orb
is pure CSS (sphere with dot grid, glowing eyes, halo platform, speech bubble, waveform) and
reflects `idle | listening | thinking | speaking | acting` from both typed turns and voice.
Checked side by side with the mock-up at 1440 px and 390 px (headless Chromium).

### 8.2 Today's Brief
At most five ranked items; "Show all" expands. All non-negotiables collapse into one
`Non-negotiables x/y` row (ranked where the first one was, pinned into the top five while
any are open) that expands on tap; each can be ticked (`POST /habits/toggle`).

### 8.4 `show_view` — the orb opens views
Tool `show_view(view, params)` with a fixed whitelist:

| view | params | data |
|---|---|---|
| `syllabus` | `subject` (name/code → code), `paper` (normalised) | `GET /syllabus`, filtered client-side |
| `brief` | — | current briefing items |
| `deadlines` | `days` (1–90) | **new** `GET /deadlines?days=` |
| `note` | `title` → resolved to `note_id` server-side | `GET /notes/{id}` |
| `changes` | `limit` (1–100) | `GET /changes` with Undo |
| `progress` | — | briefing progress + per-subject syllabus |
| `close` (aliases home) | — | closes the stage |

The orchestrator stream emits `{"type":"ui","directive":{"view","params"}}` after the tool
result; the voice tool bridge returns `ui_directive` in its response. The model never sends
HTML. Frontend: `#stage` full-screen overlay; the orb moves into the stage, the view renders
on the right, the dock stays usable on top; Esc / Close / a `close` directive return home.
Tested with OpenAI mocked (`tests/test_stage8_show_view.py`, 10 tests) and in the smoke run.

### 8.5 Live voice full-screen
Starting a live session opens the stage in voice mode (orb centred, transcript beneath, bubble
shows the last spoken line). A view opened by voice appears beside the orb. Esc or Close
clears the view first, then ends the session; stopping the session closes the stage.

### Known gaps after Stage 8
- The orb's listening/speaking states in a live session come from server VAD events; with no
  WebRTC available in CI they were exercised by feeding the same events to the handler.
- The stage views are read-mostly: the syllabus view supports tap-to-advance and the brief
  view supports ticking tasks; notes are read-only there.
- The mock-up's extra tiles (Business, Projects, Content, Applications, Agents) remain out by
  design (phases 5–6).
