# Founder OS — Rebuild Spec: Intelligence Layer

## Read this first

- Python 3.12. Backend is FastAPI + SQLAlchemy (SQLite locally and in tests, Postgres on Supabase in production). Frontend is a single vanilla JS file served by the backend on Render.
- **CLAUDE.md and AGENTS.md are out of date.** Trust the code, not those files.
- Work on a new branch, `rebuild/intelligence`, from `main`. Never push to `main`.
- **Before writing any code, post a plan** (stages, files touched, migrations, tests) and wait for my approval.
- Commit at the end of every stage with the full test suite passing. Do not start a stage while tests are failing.
- `backend/main.py` (~3,000 lines) and `frontend/index.html` (~6,100 lines) are large. Read them by line range, not whole.
- Tests must never call OpenAI; mock it. The app and the test suite must run with no `OPENAI_API_KEY` set.
- The repo is public. Never commit keys, personal data or real progress data. Use synthetic fixtures.

## Product principles (these govern every decision)

1. **Founder OS must never need maintaining.** Information gets in through conversation or a single tap, never through forms or sliders.
2. **Conversation is the primary input.** Screens are for viewing and one-tap corrections.
3. **Not every sentence is a task.** Distinguish commitments from ideas and discussion.
4. **Act, then show what changed, with undo.** Ask first only before deleting anything.
5. **Keep running costs low.** See "Running-cost rules" below.

## Goal for this session

By the end of this session, I can talk or type to Founder OS and it reliably updates my tasks, syllabus and notes, remembers context between conversations, and builds a daily briefing when I open it.

The existing dashboard command box and voice button already route to the orchestrator, so this goal is testable without new UI.

## Out of scope — do not touch

These are handled separately after this branch merges:

- Login and authentication
- Input escaping in the Business tab, cold-start and timeout handling in the frontend
- Removing tabs from the UI, the syllabus tracker UI, the briefing view, the notes UI, the undo UI
- CLAUDE.md, AGENTS.md, ROADMAP.md
- Deleting dead code or old tables
- Business/CRM, prospecting, personal brand, agent management, calendar, push notifications

If a task seems to require one of these, stop and say so rather than building it.

---

## Stage 1 — Make the assistant reliable

1. **Lazy OpenAI client** (`openai_client.py:11`). Create the client on first use. With no key, the app boots, every non-AI endpoint works, and AI endpoints return a clear 503 instead of crashing.
2. **Per-tool error handling** (`openai_client.py:504`). A failing tool returns a structured error to the model. The reply still completes and is saved.
3. **Fix the weekly-review crash** (`orchestrator_tools.py:231`). `RoadmapTask` has no `title` field.
4. **Persist tool calls and tool results** in conversation history so later turns can see what a tool returned.
5. **Server-side conversation.** One persistent default thread stored server-side, so history follows me across devices. Keep accepting the existing browser session ID for compatibility.
6. **History budget.** Send the last N turns verbatim (env var, default 20) plus a rolling summary of older turns, stored and updated as the window moves. Never resend the whole session.
7. **Long-term memory.** Wire `ai_memory` into the orchestrator: a `remember` tool for durable facts, decisions and preferences, and retrieval of relevant memories into context each turn (keyword now, embeddings from Stage 3).
8. **Graceful AI failures** in `/chat` and `/parse` (`main.py:1469`, `main.py:1584`).
9. **Model config.** `ASSISTANT_MODEL` env var, default `gpt-5.4-mini`. Remove the hardcoded `gpt-5.4` default.

**Done when:** tests prove a failing tool doesn't kill the reply, the history budget holds, tool results persist, and the app boots with no key.

## Stage 2 — Change log, undo and intent

1. **`change_log` table**: id, created_at, batch_id, source (assistant / parser / ui), table_name, record_id, action (create / update / delete), before_json, after_json, summary, undone_at. Every write made by the assistant, parser or tools is recorded.
2. **Undo**: `POST /changes/{id}/undo` and `POST /changes/batch/{batch_id}/undo`. One assistant turn is one batch. Undo restores the before-state (a create is removed). Idempotent.
3. **Assistant responses include `changes`**: a list of `{id, summary, table, action}` in both the final stream event and the saved message, so a UI can show "Updated X — Undo".
4. **Deletes need confirmation.** The assistant proposes a delete as a pending action and executes it only after an explicit yes.
5. **Intent classification.** The parser labels every extracted item as `commitment`, `idea` or `discussion`, with a confidence score.
   - Commitments write tasks.
   - Ideas append to an `Inbox` note.
   - Discussion writes nothing.
   - Low-confidence items are returned as `suggestions` and accepted with one tap (`POST /suggestions/{id}/accept`). They are not written until accepted.

## Stage 3 — Brain-dump rework and new tools

1. **Atomic writes.** `apply_parse_updates` (`main.py:1144`) runs in one transaction; any failure rolls back everything. A failed parse writes nothing (`openai_client.py:258`).
2. **Apply `todos_complete`.** Match against open todos by fuzzy title. Ambiguous matches become suggestions.
3. **Currency.** The parser captures currency (K / ZMW, $ / USD). `revenue` gets a `currency` column. The $10K scoreboard converts using a `ZMW_PER_USD` env var. Fix the prompt example that turns K500 into 500.
4. **Remove trading** from the parser, the tools and the alert rules (trade logs, backtests, trading gate). Leave the tables in place; delete no data.
5. **`todos.due` as a real date** (migration), so overdue items are computed server-side.
6. **Notes.** New `notes` table: id, title, kind (book / bible / journal / inbox / general), body (markdown), tags, created_at, updated_at.
   - Reading progress and reflections ("finished chapter 6 of Deep Work", "read Romans 8") append a dated entry to the right note, creating it if missing.
   - Copy existing book and Bible-reading data into notes once. Don't delete the originals.
7. **New tools**: `complete_task`, `update_syllabus` (Stage 4), `notes_search`, `notes_read`, `notes_append`, `notes_create`, `get_deadlines` (upcoming and overdue), `set_exam_date`.
8. **Meaning-based vault search.**
   - Embed vault chunks with `text-embedding-3-small` at sync time, only for new or changed chunks.
   - Store vectors as JSON in a column and compute cosine similarity in Python. The vault is ~715 chunks; no pgvector.
   - Fall back to keyword search when there's no key.
   - Notes and long-term memory use the same retrieval.
9. **Proactive context.** Remove the rule that vault search runs only when explicitly asked (`context/core/orchestrator_prompt.md`). The assistant searches vault, notes and memory whenever they're relevant.

## Stage 4 — Syllabus model and daily briefing

1. **`syllabus_points` table**: id, subject_code, subject_name, level (AS / A2), paper, topic_code, title, status, past_paper_score, last_revised, weakness_notes, updated_at. Unique on (subject_code, paper, topic_code).
2. **Four states**, mapped from my tracker sheet:
   - `not_started` (red)
   - `learned` (yellow — learned but weak)
   - `revised` (blue — revised but not 100%)
   - `bulletproof` (green)
3. **CSV importer**: `POST /syllabus/import` (multipart: file, subject_code, subject_name, level, optional `papers` list such as `P1,S1`).
   - Header row: `Topic, Learned, Revised, Bulletproof, Past Paper Score %, Last Revised, Weakness Notes`, with an optional `Paper` column.
   - The header row can repeat; each repeat starts a new block. Without a `Paper` column, blocks map to `papers` in order.
   - Ignore blank rows and everything from a row starting `Legend` onwards.
   - A topic cell like `1.3 Coordinate geometry` splits into code and title.
   - Status = the highest of Learned / Revised / Bulletproof that is TRUE.
   - Upsert; re-importing never duplicates. Return created / updated / skipped counts.
4. **Endpoints**: `GET /syllabus` (grouped by subject and paper, with progress per group), `POST /syllabus/{id}/advance` (one tap moves to the next state, wrapping round), `PATCH /syllabus/{id}`.
5. **`update_syllabus` tool.** "Revised S1 probability today, still shaky on conditional probability" sets the status, sets `last_revised` automatically, and appends the weakness note. Match topics by name or code, fuzzily.
6. **Replace percentage mastery.** `surface_weakest_subtopic` becomes `surface_weakest_topics`, using status, staleness of `last_revised`, weakness notes and exam proximity. Update the matching alert rule. The old academic tables stay but the assistant stops reading them.
7. **Exam dates.** The stored dates are stale. `set_exam_date` lets conversation set them ("Further Maths P1 is on 12 May").
8. **Daily briefing**: `GET /briefing/today`.
   - **Deterministic core, no LLM needed**: overdue and carried-over tasks, today's tasks, non-negotiables status, deadlines in the next 14 days, exam countdowns, syllabus priorities (red and yellow topics weighted by exam proximity and staleness), recent weakness notes, critical annual targets, alerts.
   - Every item has a title, category, reason (why it matters), suggested action and target id. The list is ranked.
   - **Optional summary**: at most one LLM call per day, cached for that day, producing 3–5 sentences. Regenerate only on request.
   - Works fully with no OpenAI key.
9. **Alerts.** The backend is the single source. Drop the trading rule, point the habits rule at `non_negotiables`, and use the syllabus-based academic rule. Alerts appear inside the briefing.

**After Stage 4: stop, post the hand-off report, and wait for my go-ahead before Stage 5.**

## Stage 5 — Real-time voice (only after I approve)

1. `POST /voice/realtime/session` issues a short-lived client secret. The browser never sees the API key.
2. Frontend: WebRTC connection, voice activity detection, interruption, live transcript, and visible states (listening / thinking / speaking / acting). A start/stop button. The session ends automatically after 2 minutes of silence to cap cost.
3. The realtime session exposes the same tool registry. Tool calls run server-side through one endpoint that reuses the orchestrator tool functions, change log and confirmation rules.
4. The transcript saves into the same server-side conversation.
5. `REALTIME_MODEL` env var, defaulting to the current mini realtime model. Check the exact model ID in OpenAI's docs.
6. Keep the existing record → transcribe → speak loop as a fallback.

---

## Migrations and data safety

- Hand-written, re-runnable migrations following the `m006_todo_completed_at.py` pattern, wired into startup. Never rely on table creation for new columns: the startup schema check will crash-loop Render.
- Enable row-level security on every new table, following m009.
- Everything must work on SQLite and Postgres. Flag any SQL that might behave differently on Postgres.
- No destructive migrations. Drop no tables or columns.

## Running-cost rules

- Defaults: `gpt-5.4-mini` for the assistant, parser and briefing summary; `text-embedding-3-small` for embeddings.
- Put the system prompt and tool definitions first and keep them stable, so prompt caching applies.
- Enforce the history budget from Stage 1.
- At most one briefing summary call per day.
- Embed only new or changed chunks.
- Log every OpenAI call to an `llm_usage` table (model, input, cached input, output, estimated cost) and expose `GET /usage/summary` by day and month.

## Tests and acceptance

All existing tests pass. Fix the date-dependent war-room test (`test_warroom.py:95`). New tests, with OpenAI mocked:

1. "I finished the Business essay" completes the matching todo, logs the change, and undo restores it.
2. "Revised S1 probability today, still shaky on conditional probability" sets that topic to revised, sets today's date, and appends the weakness note.
3. "Finished chapter 6 of Deep Work" appends to that book's note, creating it if missing.
4. "Maybe I should start a newsletter someday" creates no task and appends to the Inbox note.
5. A tool that raises an exception doesn't stop the reply from completing and saving.
6. A delete request deletes nothing until confirmed.
7. A synthetic CSV fixture mirroring the sheet (repeated header blocks, blank rows, legend rows) imports correctly, and re-importing doesn't duplicate.
8. The briefing returns ranked items with no OpenAI key.
9. "Got K500 from a client" stores 500 ZMW.
10. The app boots with no `OPENAI_API_KEY`.

## Hand-off report (after Stage 4, and again after Stage 5 if run)

- What was built per stage, and anything not finished.
- Every new or changed endpoint with request and response shapes. The UI will be built against these.
- Migrations added, and anything risky on Postgres.
- Env vars to set on Render.
- Known issues.
