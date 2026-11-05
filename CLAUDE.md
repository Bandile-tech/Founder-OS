# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository. It describes the
system as it is after the intelligence-layer rebuild (branch `claude/rebuild-spec-review-vfrg4o`).
`docs/REBUILD_SPEC.md` is the spec that produced it; `docs/REBUILD_HANDOFF.md` has every endpoint
shape; `ROADMAP.md` has the phases.

## Product principles (these govern every decision)

1. **Founder OS must never need maintaining.** Information gets in through conversation or a
   single tap, never through forms or sliders.
2. **Conversation is the primary input.** Screens are for viewing and one-tap corrections.
3. **Not every sentence is a task.** Distinguish commitments from ideas and discussion.
4. **Act, then show what changed, with undo.** Ask first only before deleting anything.
5. **Keep running costs low.** One model gateway, a history budget, one briefing summary per
   day, embed only new or changed text, log every call.

## Running the project

```
cd backend
pip install -r requirements.txt      # Python 3.12 (the pins do not build on 3.13)
uvicorn main:app --reload
```
Visit `http://127.0.0.1:8000` — the backend serves `frontend/index.html` at `/` and the API on
the same origin. Frontend-only use (`open frontend/index.html`) still works for viewing local
state but every feature that matters needs the backend.

Environment (`backend/.env` locally, Render env vars in production):

| Var | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | unset | Without it everything non-AI works; AI endpoints return 503 |
| `FOUNDER_OS_PASSWORD` | unset | **Set in production.** Gates every route except /health, /ping, /auth/* |
| `FOUNDER_OS_ORIGINS` | local dev origins | Comma-separated CORS allow-list (the Netlify origin) |
| `ASSISTANT_MODEL` | `gpt-5.4-mini` | Assistant, parser, summaries, briefing |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | Vault / notes / memory retrieval |
| `ZMW_PER_USD` | `27` | Kwacha → dollar for the revenue scoreboard |
| `REALTIME_MODEL` | `gpt-realtime-2.1-mini` | Live voice (current mini realtime model, checked 2026-11-05) |
| `ASSISTANT_HISTORY_TURNS` / `ASSISTANT_SUMMARY_BATCH` | `20` / `10` | History budget |
| `INTENT_CONFIDENCE_MIN` | `0.7` | Below this an extracted item becomes a suggestion |

Tests (OpenAI is always mocked; the suite must pass with no key set):
```
cd backend
python -m pytest -q
```
`TestClient(app)` without a context manager never fires startup, so migrations are exercised
by `test_startup_handler_runs_with_no_key`, which calls `main.startup()` directly. After any
migration change, also run `python -c "import asyncio, main; asyncio.run(main.startup())"`.

The same suite runs on Postgres with `TEST_DATABASE_URL=postgresql://…` (throwaway database;
every test drops and recreates the schema). Browser smoke tests (login, brief item complete +
undo, syllabus tap-to-advance): `node tests/e2e/smoke.js` from the repo root. Deployment steps
and rollback: `docs/DEPLOY.md`.

## Architecture

### Frontend
One self-contained file, `frontend/index.html` (inline CSS and vanilla JS, no build step).
Luna palette: near-black base, cyan `#A7EBF2` for signal only, Bricolage Grotesque for display
numerals, Geist for body. Pages: Command Centre (home, built on `GET /briefing/today`),
Dashboard, Timetable, Health, Syllabus, Academic Roadmap (legacy), Agents, War Room, Annual
Targets, Design Guide, Settings. The home page is a 1582×866 pixel canvas laid out at the
coordinates of `docs/design/command-centre.png` (scaled down on narrower viewports, stacked
below 1024 px) using the cropped mock-up assets in `frontend/assets/` (`orb.png`, `scene.png`).
The global command dock is the talk bar (a glowing pill, docked into the home canvas): mic on
the left starts live voice (WebRTC), typed input in the middle, send on the right, and a
suggestions chip above it. Every backend call goes through a `fetch` wrapper that attaches
the session token; a 401 opens the login gate.
The orb is the assistant's presence (states idle / listening / thinking / speaking / acting
as CSS overlays on the orb image). `show_view` directives (`{"type":"ui"}` SSE events, or
`ui_directive` from the voice tool bridge) open a full-screen stage with a whitelisted view
(syllabus, brief, deadlines, note, changes, progress) laid out as KPI cards, list, next best
actions; the panel always ends above the dock. Live voice with no view open is the centred
full-screen orb with the transcript beneath it; a view opened during voice sits beside it.
All user text is escaped with `escHtml` before it reaches `innerHTML`; `mdHtml` escapes raw HTML
before markdown runs.

### Backend (FastAPI, `backend/`)

| File | Role |
|---|---|
| `main.py` | App, startup (migrations → schema check → seeds), legacy endpoints, `apply_parse_updates` |
| `llm.py` | The only OpenAI gateway: lazy client, model config, usage logging, cost estimates |
| `openai_client.py` | Prompts: parser schema, orchestrator loop (`get_orchestrator_response`) |
| `orchestrator.py` | `POST /orchestrator` SSE bridge: history budget, memory, persistence of tool calls |
| `orchestrator_tools.py` | The tool registry (23 tools, incl. `show_view` UI directives) and `run_tool` (never raises) |
| `conversation.py` | One default thread, verbatim window + rolling summary, replay of tool results |
| `memory_service.py` | Long-term memory (`remember` tool, recall) |
| `change_log.py` | Every write recorded with before/after snapshots; undo; batches live on `db.info` |
| `actions.py` | Pending deletes needing confirmation; suggestions accepted with one tap |
| `notes.py` | Notes (book / bible / journal / inbox / general), search, endpoints |
| `retrieval.py` | Shared `embeddings` table, cosine in Python, keyword fallback |
| `syllabus.py` | `syllabus_points` four-state tracker, CSV import, exam dates, weakest topics |
| `briefing.py` | `GET /briefing/today`: deterministic ranked items, progress, alerts, cached summary |
| `auth.py` | Password login, HMAC session tokens, route protection, CORS origins |
| `voice_realtime.py` | Client secret for WebRTC voice, server-side tool bridge, transcript save |
| `usage.py` | `GET /usage/summary` from `llm_usage` |
| `vault_sync.py` | Obsidian vault clone/index; semantic search when embedded |
| `models.py` / `schemas.py` / `database.py` | ORM, Pydantic, engine (SQLite locally, Postgres on Supabase) |
| `migrations/` | Hand-written, re-runnable, wired into startup (`m010`–`m013` are the rebuild) |
| `market_intel/` | Research agent (out of scope for the rebuild; still uses the lazy client) |

### How a message flows
`POST /orchestrator` → `orchestrator.orchestrator_stream` builds history (last N turns verbatim +
summary) and recalls memories → `get_orchestrator_response` streams the model, executes tool
calls through `run_tool` inside one change-log batch → every message of the turn (user,
assistant tool calls, tool results, final) is saved to the default thread → the final SSE
event carries `changes`, which the UI shows with Undo.

`route_brain_dump` → parser (`get_parse_response`) → `apply_parse_updates` (atomic, one
commit, everything logged). Commitments become tasks, ideas go to the Inbox note, discussion
writes nothing, low-confidence items become suggestions.

### Data
SQLite at `backend/founder_os.db` locally; Postgres on Supabase in production with RLS enabled
on every table. Never rely on `create_all` for a new column: add a migration following
`migrations/m010_assistant_core.py` and the helpers in `migrations/_helpers.py`, and enable RLS
on new tables. No destructive migrations.

## Critical components — treat with care
- `apply_parse_updates` (atomicity, change logging, intent handling) and the parser schema in
  `openai_client.get_parse_response`.
- `change_log` snapshot/undo and the batch-on-session mechanism (SSE generator steps run on
  separate threads, so context variables do not survive between them).
- The orchestrator prompt in `context/core/orchestrator_prompt.md` and the tool schemas: keep
  them byte-stable where possible so prompt caching applies; dynamic context goes in the second
  system message.
- `briefing.build_briefing` ranking and `syllabus.weakest_topics` scoring — the home screen and
  the assistant's study advice depend on them.
- `auth.AuthMiddleware` exemptions.

## Key domain concepts
- **Syllabus states**: `not_started` (red) → `learned` (yellow) → `revised` (blue) →
  `bulletproof` (green). One tap or `update_syllabus` advances; `last_revised` is automatic.
- **Exam dates** per `(subject_code, paper)` in `exam_dates`, falling back to `subjects.exam_date`.
- **Currency**: revenue rows carry `USD` or `ZMW`; conversions use `ZMW_PER_USD`.
- **Habit keys** (non-negotiables): `scripture_prayer`, `ironing`, `python_session`,
  `sprint_training`, `academics` — referenced by the parser.
- **KPI keys**: `maths_syllabus`, `further_maths`, `business`, `economics` (sprint KPIs and
  trading are no longer tracked by the assistant).
- **Trading** tables and `/trading/*` endpoints still exist but nothing in the UI or the
  assistant reads them.
