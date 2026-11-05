# Founder OS — Conventions

Every pattern the intelligence-layer rebuild (Stages 1–6, branch
`claude/rebuild-spec-review-vfrg4o`) established, with the file that defines it and a
short example. **A new module must follow every rule in section 14.** When a rule here
and the code disagree, the code wins — fix the document.

Read with: `CLAUDE.md` (architecture), `docs/REBUILD_HANDOFF.md` (endpoint shapes),
`ROADMAP.md` (phases). Specs for the next phases live in `docs/phases/`.

---

## 0. Ground rules that never change

| Rule | Where it comes from |
|---|---|
| Python 3.12. The pins in `backend/requirements.txt` do not build on 3.13. `runtime.txt` still says 3.11.0 for Render — do not raise it without testing the pins. | `CLAUDE.md`, `docs/REBUILD_HANDOFF.md` §5 |
| The app and the whole test suite run with **no `OPENAI_API_KEY`**. | `llm.py`, `tests/conftest.py` |
| Tests never reach OpenAI or the network. | every `tests/test_stage*.py` |
| The repo is public. No keys, no personal data, no real progress data in fixtures. | `docs/REBUILD_SPEC.md` |
| Never rely on `create_all` for a new column. Hand-written, re-runnable migration + RLS. | `migrations/_helpers.py`, §5 below |
| No destructive migrations. No table or column is ever dropped. | `docs/REBUILD_SPEC.md` |
| One OpenAI gateway (`llm.py`). Every call is logged with an estimated cost. | `llm.py` |
| Every write by the assistant, parser, tools or UI goes through `change_log`. | `change_log.py` |
| Deletes are proposed, never executed, until an explicit yes. | `actions.py` |
| Not every sentence is a task: commitment / idea / discussion with a confidence. | `main._apply_parse_updates_inner` |
| The frontend is one file, `frontend/index.html`, vanilla JS, no build step. All user text goes through `escHtml` before `innerHTML`. | `CLAUDE.md` |
| Commit per stage with the suite green. One commit message per completed unit of work, imperative mood, no model names. | git log |

---

## 1. The OpenAI gateway — `backend/llm.py`

### 1.1 Rules

- **Only `llm.py` constructs an OpenAI client.** `llm.get_client()` is lazy; with no key
  it raises `llm.AIUnavailable`. Endpoints translate that to HTTP 503 via
  `main._ai_http_error(exc)` (`main.py` ~line 1660).
- **Every call goes through a logged helper**: `llm.chat_completion`, `llm.chat_text`,
  `llm.embed_texts`. Each logs one `llm_usage` row (`model, purpose, input_tokens,
  cached_input_tokens, output_tokens, estimated_cost_usd, duration_ms`).
  If you must call the SDK directly (streaming, realtime, audio), you call
  `llm.get_client()` **and** `llm.log_usage(...)` yourself. Reference:
  `openai_client._stream_completion` (streaming) and
  `voice_realtime.create_realtime_session` (a call with no token counts still logs a row
  with zeros so the call count is right).
- **Model comes from config, never a literal.** `llm.assistant_model()` (env
  `ASSISTANT_MODEL`, default `gpt-5.4-mini`), `llm.embedding_model()`. Passing
  `model=` explicitly is allowed only for a cheaper or different-class model and must
  still be an env-driven value. (`market_intel/agent.py` still hardcodes
  `MODEL = "gpt-4o-mini"` — the Phase 5A spec removes that.)
- **`purpose` is a short snake_case tag** and is how `GET /usage/summary` and the cost
  tables in the specs are read. Existing purposes: `assistant`, `parse`, `chat`,
  `conversation_summary`, `briefing_summary`, `proactive_brief`, `embed_vault`,
  `embed_note`, `embed_memory`, `embed_query`, `realtime_session`. New modules use
  `<module>_<role>` (e.g. `market_intel_planner`, `lead_qualify`).
- **Pricing is an estimate**, from `_DEFAULT_PRICING` in `llm.py`, overridable with
  `LLM_PRICING_JSON`. An unknown model is priced as the assistant default so cost is
  never silently zero. Add a price row when you introduce a model id.
- **Prompt caching**: the stable system prompt and the tool definitions go first and
  must stay byte-identical across turns; anything dynamic (memories, summary, today's
  date, live state) goes in a **second** system message. See `conversation.context_message`
  and `openai_client.get_orchestrator_response`.

### 1.2 Example — a logged, non-streaming JSON call

```python
import json, llm

def qualify(db, lead: dict) -> dict:
    messages = [
        {"role": "system", "content": QUALIFY_PROMPT},          # stable, first
        {"role": "user", "content": json.dumps(lead)},
    ]
    raw = llm.chat_text(messages, db=db, purpose="lead_qualify",
                        max_tokens=400, response_format={"type": "json_object"})
    try:
        return json.loads(raw)
    except ValueError:
        return {}        # a bad model answer degrades, never crashes a reply
```

`db=db` lets the usage row ride on the request session; `db=None` opens its own.

### 1.3 Example — the 503 path in an endpoint

```python
@router.post("/leads/{lead_id}/qualify")
def qualify_endpoint(lead_id: int, db: Session = Depends(get_db)):
    try:
        result = leads.qualify(db, lead_id)
    except Exception as exc:                 # noqa: BLE001
        import main
        raise main._ai_http_error(exc)       # AIUnavailable → 503, anything else → 502
    return result
```

### 1.4 Cost discipline (from the running-cost rules)

- History budget: last `ASSISTANT_HISTORY_TURNS` (20) user turns verbatim + rolling
  summary refreshed only every `ASSISTANT_SUMMARY_BATCH` (10) messages (`conversation.py`).
- At most one briefing summary call per day (`briefing._summary`, cached in
  `briefing_summaries`).
- Embed only new or changed text (`retrieval.index_items` is keyed by content hash).
- Any new recurring LLM call must have a cap (per day, per run, or per item) and the cap
  must be named in its spec and enforced in code, not in the prompt.

---

## 2. Change log and undo — `backend/change_log.py`

### 2.1 Rules

- **Record every write** with one of `record_create(db, obj, summary)`,
  `record_update(db, obj, before, summary)`, `record_delete(db, obj, summary)`.
  `before` is `change_log.snapshot(obj)` taken **before** mutating. `record_update` is a
  no-op when nothing changed. `record_delete` records then deletes; you commit.
- **One assistant turn is one batch.** The orchestrator calls `begin_batch(db,
  "assistant")` before the model loop and `end_batch(db)` in `finally`
  (`orchestrator.orchestrator_stream`). Everything written inside — tools, parser,
  suggestions — shares that `batch_id`.
- **Everywhere else, wrap writes in `with change_log.batch_scope(db, "<source>"):`**.
  It opens a batch only if none is active, so a UI endpoint and a tool can share code.
  Sources in use: `assistant`, `parser`, `ui`. New sources: `agent:<key>`, `telegram`,
  `job:<name>` (Phase 6).
- **The batch lives on `db.info`, not a context variable.** Starlette runs each step of
  an SSE generator on a different worker thread; context set in one step is gone in the
  next. Anything that must survive a turn rides on the session (`db.info[...]`).
- **Undo is idempotent and generic.** `_undo_row` finds the model by table name through
  the SQLAlchemy registry (`_model_for`) and coerces JSON back to column types. Any table
  with an integer `id` primary key is undoable with no extra code. Side effects outside
  the database need a hook in `_after_undo_hook` (today: reload KPI state).
- **Return `changes` to the caller.** Tools return the change entry in their result;
  endpoints return `change` or `changes`; the orchestrator's final SSE event carries
  `change_log.current_batch_changes(db)`. The UI renders them with Undo
  (`renderChangesStrip` in `index.html`).
- **Summaries are one human line, ≤ 80 chars of user text**, e.g.
  `f"Completed task: {todo.text[:80]}"`. They are what the user reads next to Undo.

### 2.2 Example — a logged update inside an endpoint

```python
@router.patch("/deals/{deal_id}")
def patch_deal(deal_id: int, payload: DealPatch, db: Session = Depends(get_db)):
    deal = db.query(models.Deal).filter(models.Deal.id == deal_id).first()
    if deal is None:
        raise HTTPException(404, "Deal not found")
    with change_log.batch_scope(db, "ui"):
        before = change_log.snapshot(deal)
        for k, v in payload.model_dump(exclude_none=True).items():
            setattr(deal, k, v)
        deal.updated_at = datetime.utcnow()
        change = change_log.record_update(db, deal, before, f"Deal '{deal.title}' → {deal.stage}")
        db.commit()
    return {**serialize(deal), "change": change}
```

### 2.3 Undo endpoints (already exist; never duplicate them)

`GET /changes?limit=&batch_id=`, `POST /changes/{id}/undo`,
`POST /changes/batch/{batch_id}/undo`.

---

## 3. Intent, suggestions and the parser — `main.apply_parse_updates`, `actions.py`

### 3.1 Rules

- The parser (`openai_client.get_parse_response`) returns one JSON payload; **everything
  it extracts is applied by `main.apply_parse_updates(db, parsed, today)` in one
  transaction** (one commit, full rollback on any failure; in-memory KPI state updated
  only after commit). `/parse`, `/input`, the `route_brain_dump` tool and suggestion
  acceptance all call it. Do not add a second write path for parsed text.
- Every `todos_add` item carries `intent` (`commitment | idea | discussion`) and
  `confidence` (0–1). Commitments become todos; ideas append to the `Inbox` note
  (`notes.inbox_add`); discussion writes nothing; anything below
  `INTENT_CONFIDENCE_MIN` (0.7) becomes a **suggestion** and is not written.
- **Suggestions** are rows in `suggestions` created with
  `actions.add_suggestion(db, kind, payload, confidence=, reason=, source_text=, source=)`
  and applied by `actions.accept_suggestion`, which replays the payload through
  `apply_parse_updates`. Kinds today: `todo_add`, `inbox_add`, `todo_complete`.
  **Adding a kind = add a branch in `accept_suggestion` that builds a `parsed` dict**,
  plus a key in `_apply_parse_updates_inner` if the parser does not already apply it.
- Ambiguity becomes suggestions, never a guess: `main.resolve_todo_completion` completes
  only when the top fuzzy match is ≥ 0.75 and ≥ 0.2 ahead of the runner-up; otherwise up
  to 3 `todo_complete` suggestions. Reuse this threshold shape for any fuzzy match that
  writes (leads, clients, projects, topics).
- **Changing the parser schema is a critical change.** Add keys only, keep every example
  in the prompt consistent with the schema, and add an acceptance test for each new key.
  Prefer an orchestrator tool over a parser key when the information is only ever stated
  in direct conversation with the assistant.

### 3.2 Example — a new suggestion kind

```python
# actions.accept_suggestion
elif s.kind == "project_pause":
    parsed["project_updates"] = [{"project_id": payload["project_id"], "status": "paused"}]
```
and in `_apply_parse_updates_inner`:
```python
for u in parsed.get("project_updates") or []:
    proj = db.query(models.Project).filter(models.Project.id == int(u["project_id"])).first()
    if proj and u.get("status") and u["status"] != proj.status:
        before = change_log.snapshot(proj)
        proj.status = u["status"]
        change_log.record_update(db, proj, before, f"Project '{proj.title}' → {proj.status}")
        result.setdefault("projects_updated", []).append(proj.id)
```

---

## 4. Pending actions — deletes and anything outward-facing — `backend/actions.py`

### 4.1 Rules

- Deletes are never executed by the assistant. `propose_delete(db, table, record_id,
  reason)` writes a `pending_actions` row (status `pending`, TTL 24 h,
  `payload_json` = snapshot) and **deletes nothing**. `confirm_action` executes through
  `change_log.record_delete` so it is undoable; `cancel_action` closes it. Duplicate
  proposals for the same row reuse the open one.
- `DELETABLE` in `actions.py` is the allow-list of tables the assistant may propose
  deleting from, with the column used for the human label. **A new table is not
  deletable until added there and in the `propose_delete` tool schema enum.**
- The prompt rule (`context/core/orchestrator_prompt.md`, "DELETES NEED CONFIRMATION")
  makes the model ask in plain words and call `confirm_action` only after an explicit yes
  in the **next** message.
- **Generalise, don't fork.** Phase 5/6 actions that leave the system (sending a message,
  creating a remote calendar event that cannot be undone, paying) use the same table with
  a new `kind` (`send_outreach`, `notify`, …) and a dispatcher in `confirm_action` keyed
  on `kind`. Today `confirm_action` assumes `kind == "delete"`; the first module that
  needs another kind refactors it into `_EXECUTORS = {"delete": _execute_delete, ...}`.
- Endpoints `GET /actions/pending`, `POST /actions/{id}/confirm`,
  `POST /actions/{id}/cancel` are the one-tap path; the UI shows pending actions in the
  dock.

### 4.2 Example — proposing a non-delete action

```python
p = models.PendingAction(
    created_at=datetime.utcnow(), kind="send_outreach", table_name="outreach_messages",
    record_id=msg.id, summary=f"Send {msg.channel} to {lead.name}: {msg.subject or msg.body[:60]}",
    payload_json=json.dumps({"message_id": msg.id}), status="pending",
    expires_at=datetime.utcnow() + timedelta(hours=actions.PENDING_TTL_HOURS),
    batch_id=change_log.current_batch_id(db),
)
```

---

## 5. Migrations and RLS — `backend/migrations/`

### 5.1 Rules

- One file per module change, numbered `mNNN_<topic>.py`, next free number first.
  Imports `dialect, add_column_if_missing, create_table_if_missing, create_index,
  enable_rls` from `migrations/_helpers.py`. Template: `m013_syllabus_briefing.py`
  (tables), `m010_assistant_core.py` (columns + data backfill), `m012_…` (Python-side
  backfill of a typed column from free text).
- `def run(db=None)` opens `engine.connect()`, works inside `with conn.begin():`,
  prints `[mNNN] …` lines, closes. Idempotent: every step checks existence first.
- **Register it in `main.startup()`** after the previous migration and before
  `_validate_schema(engine)`. The schema check raises if any ORM column is missing
  from the live DB — a missing migration crash-loops Render.
- **ORM first, SQL second.** Add the model in `models.py` (snake_case table, `Column`
  style, `datetime.utcnow` defaults) and write the DDL to match it exactly.
  Note: `models.Base.metadata.create_all(bind=engine)` runs at import of `main.py`, so
  on a fresh database the ORM creates every table **before** migrations run and
  `create_table_if_missing` is a fallback. What really executes on an existing production
  database is `ADD COLUMN`, index creation, data backfills and RLS.
- **`ADD COLUMN` must use types Postgres accepts**: `VARCHAR`, `TEXT`, `INTEGER`,
  `REAL` or `DOUBLE PRECISION`, `DATE`, `TIMESTAMP`, `BOOLEAN DEFAULT FALSE`.
  `DATETIME` is a SQLite spelling that Postgres rejects — only ever use it inside the
  `CREATE TABLE` fallback strings (where the ORM has already made the table), never in
  `add_column_if_missing`. A `NOT NULL` column added to an existing table needs a
  `DEFAULT`.
- **RLS on every new table**: `enable_rls(conn, d, created, TAG)` with the list of
  tables actually created. On Postgres this is `ENABLE ROW LEVEL SECURITY` with no
  policies, which blocks PostgREST/anon access; the backend's direct connection bypasses
  RLS. If the ORM created the table before the migration ran (fresh deploy), `created`
  is empty — so **also** call `enable_rls` unconditionally for the module's tables when
  `d == "postgresql"` (see the pattern in §5.2). Re-enabling is a no-op.
- Backfills run **in Python row by row**, never as a SQL cast (`m012._backfill_due_dates`).
- After writing a migration run both:
  ```
  python -m pytest -q
  python -c "import asyncio, main; asyncio.run(main.startup())"
  ```
  (the second against the local SQLite file; the test client never fires startup).

### 5.2 Example

```python
"""Migration 015 — Leads and outreach (Phase 5B)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from database import engine
from migrations._helpers import (dialect, add_column_if_missing, create_table_if_missing,
                                 create_index, enable_rls)

TAG = "m015"
_TABLES = {"leads": """
CREATE TABLE IF NOT EXISTS leads (
    id          INTEGER PRIMARY KEY {autoincrement},
    name        VARCHAR NOT NULL,
    status      VARCHAR NOT NULL DEFAULT 'new',
    created_at  DATETIME,
    updated_at  DATETIME
)"""}

def run(db=None):
    conn = engine.connect()
    try:
        d = dialect(conn, TAG)
        with conn.begin():
            created = [t for t, sql in _TABLES.items() if create_table_if_missing(conn, d, t, sql, TAG)]
            add_column_if_missing(conn, d, "todos", "lead_id", "INTEGER", TAG)
            create_index(conn, "ix_leads_status", "leads", "status")
            enable_rls(conn, d, list(_TABLES), TAG)   # all module tables, every run (no-op if already on)
        print(f"[{TAG}] complete on {d}.")
    finally:
        conn.close()

if __name__ == "__main__":
    run()
```

---

## 6. Router modules — one file per domain

### 6.1 Rules

- A domain lives in `backend/<domain>.py`: module docstring listing primitives and
  endpoints; `router = APIRouter()`; **primitives first** (plain functions taking `db`,
  used by tools, the parser and endpoints), **endpoints last** as thin wrappers; Pydantic
  request models defined in the module (not `schemas.py`) for rebuild-era modules.
  Templates: `notes.py` (CRUD + search), `syllabus.py` (import + scoring),
  `briefing.py` (read-only aggregation), `actions.py` (state machine).
- Register in `main.py` beside the others:
  ```python
  import leads as _leads
  app.include_router(_leads.router)
  ```
  Routers are included before `/app` static mount; no prefix — paths are written in full
  (`/leads`, `/leads/{id}`).
- `serialize(obj, *, body=True)`-style functions return plain dicts; dates are left as
  `date`/`datetime` and FastAPI serialises them to ISO strings.
- Import `main` **lazily inside functions** (`import main`) when you need something from
  it — `main` imports the routers, so a top-level import is circular.
- Auth is automatic: `auth.AuthMiddleware` protects every path except `/health`, `/ping`,
  `/auth/*`, `/`, `/index.html`, `/app/*` and OPTIONS. **A new unauthenticated path (a
  webhook, a jobs tick) must be added to `auth.EXEMPT_EXACT` deliberately and must carry
  its own secret check.**
- Fuzzy name lookups use `func.lower(col).contains(fragment.lower())` or Python, never
  `ILIKE`, so SQLite and Postgres agree.
- Error style: `HTTPException(404, "Deal not found")`, `HTTPException(400, "...")`,
  AI failures via `main._ai_http_error`.

### 6.2 Example skeleton

```python
"""
Leads — prospect database (Phase 5B).

Primitives: find_lead, create_lead, import_csv, qualify, convert
Endpoints:  GET /leads, GET /leads/{id}, POST /leads/import, PATCH /leads/{id}, …
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
import models, change_log
from database import get_db

router = APIRouter()

def serialize(l: models.Lead) -> dict: ...
def find_lead(db: Session, fragment: str) -> list[tuple[float, models.Lead]]: ...

class LeadPatch(BaseModel):
    status: str | None = None

@router.get("/leads")
def list_leads(status: str | None = None, db: Session = Depends(get_db)): ...
```

---

## 7. Orchestrator tools — `backend/orchestrator_tools.py`

### 7.1 Rules

- A tool is `def name(db, **kwargs) -> (result_dict, summary_str)`. The result is JSON
  for the model; the summary is the one human line in the SSE `tool_result` event.
  Tool bodies are thin: they call the domain module's primitives.
- **Register in three places**: the function, `_TOOL_DISPATCH["name"] = lambda db, a: …`
  (coerce types from the `args` dict there: `int(a.get("limit", 5))`), and an entry in
  `TOOL_SCHEMAS` (OpenAI function schema; `required` lists only what is truly required).
  Then add a routing paragraph to `context/core/orchestrator_prompt.md`.
- `run_tool(name, args, db)` **never raises**: unknown tool or exception → `{"error": …}`
  and a rollback. A tool therefore returns `{"error": "..."}` for expected failures too
  (`notes_read` not found, `set_exam_date` bad date) rather than raising.
- Tools that write **commit** (`db.commit()`) after recording changes — the orchestrator
  does not commit for them. They return the change entry (`"change": change`).
- Tools that could be ambiguous create suggestions and return `"ambiguous": True` with
  candidates (`complete_task`, `update_syllabus`). Tools never guess.
- **Authority rules live in the prompt**, with the `add_todos` / `run_market_research_agent`
  / `promote_research_opportunity` sections as the template for "explicit command only"
  tools. A write tool the model could misuse gets such a section.
- The prompt and `TOOL_SCHEMAS` are cached-prefix material: change them only when the
  change is needed, keep them stable between deploys, keep descriptions short.
- The realtime voice path converts `TOOL_SCHEMAS` automatically
  (`voice_realtime.realtime_tools`) and runs tools through `/voice/realtime/tool` with
  the same `run_tool`. Nothing extra to do for voice.
- Tool count today: 22 (+1 legacy alias). Phases 5–6 take it to roughly 50. Keep it under
  ~60: fold near-identical verbs into one tool (e.g. `draft_outreach` also revises when
  given a `message_id`), and never add a tool whose job an existing tool already does.

### 7.2 Example

```python
def update_deal(db, deal: str, stage: str | None = None, value: float | None = None,
                currency: str | None = None):
    import crm
    matches = crm.find_deal(db, deal)
    if not matches:
        return {"error": f"no deal matches '{deal}'"}, f"no deal matches '{deal}'"
    top, second = matches[0][0], (matches[1][0] if len(matches) > 1 else 0.0)
    if top < 0.75 or top - second < 0.2:
        return ({"ambiguous": True, "candidates": [{"id": d.id, "title": d.title, "score": s} for s, d in matches[:3]]},
                f"{len(matches)} deals could match '{deal}' — ask which")
    result = crm.update_deal(db, matches[0][1], stage=stage, value=value, currency=currency)
    db.commit()
    return result, f"deal '{result['title']}' → {result['stage']}"

_TOOL_DISPATCH["update_deal"] = lambda db, a: update_deal(
    db, deal=a.get("deal", ""), stage=a.get("stage"), value=a.get("value"), currency=a.get("currency"))
```

Schema entry: `{"type": "function", "function": {"name": "update_deal", "description": "…ONLY when Bandile states a deal moved stage or changed value…", "parameters": {"type": "object", "properties": {...}, "required": ["deal"]}}}`.

---

## 8. Conversation, memory and the SSE turn — `orchestrator.py`, `conversation.py`, `memory_service.py`

- One server-side thread (`conversation.DEFAULT_THREAD_ID == "default"`) shared across
  devices; the browser `session_id` is stored but never partitions history.
- `conversation.save_message(db, role, content, *, tool_calls=, tool_call_id=, changes=)`
  persists every message of a turn. Tool results are truncated to 1,500 chars on replay.
- Memories: `memory_service.remember(db, text, kind, tags)` (logged, embedded) and
  `recall(db, query, limit)` (embeddings when a key exists, keyword otherwise). The top
  5 are injected as the second system message every turn.
- Any new inbound channel (voice today, Telegram in Phase 6D) **reuses
  `orchestrator.orchestrator_stream`-equivalent plumbing**: build history, open a change
  batch, run `get_orchestrator_response`, persist every message, end the batch, return
  `final` + `changes`. Voice does this piecemeal (`voice_realtime.run_realtime_tool`); a
  text channel should call a shared `orchestrator.run_turn(text, db, source=...)` that
  Phase 6D extracts from `orchestrator_stream` (collect the events instead of yielding).

---

## 9. Retrieval — `backend/retrieval.py`

- One `embeddings` table for every source type (`vault`, `note`, `memory`; new types are
  just new strings). Vectors are JSON in TEXT, cosine in Python, corpus kept small.
- `index_items(db, source_type, [(source_id, chunk_index, text)])` embeds only texts whose
  hash is new, moves pointers for moved text, prunes stale rows. `index_one` for a single
  record on write. `search(db, query, source_types, limit)` returns `None` with no key, so
  callers fall back to keyword search (`notes.search_notes` is the template).
- A module that wants semantic search over its text (leads notes, communications, CRM
  notes) adds `retrieval.index_one(db, "<type>", id, text)` in its write primitives inside
  a `try/except` that logs and continues, and a keyword fallback in its search.
- Backfill after a key is set: `retrieval.index_notes(db)` / `index_memories(db)`; a
  module adds `index_<type>(db)` in the same shape.

---

## 10. Briefing items — `backend/briefing.py`

- `build_briefing(db, refresh=, now=, with_summary=)` is deterministic and needs no key.
  Sections are functions returning `(items, extras)`; items are built with `_item(...)`
  and carry `id, title, category, due_label, due_date, reason, suggested_action,
  target_type, target_id, completable, score` (+ `extra`). The list is sorted by `score`.
- **Score bands** (keep new categories inside them so ranking stays sane):
  overdue tasks 90–104; due today ~70–80; non-negotiables 55; target critical 60; alerts
  critical 65 / warn 45 / info 15; exams 5–80 by proximity; deadlines 40–61; syllabus
  20–40ish; high-priority undated 30–40.
- **Adding a section**: write `_<module>_items(db, today) -> list`, call it in
  `build_briefing`, extend `items = …` and (if it should count toward the bell)
  `attention`/`alert_count`. Add the new `category` and `target_type` strings to the
  docstring and to the hand-off shape. `completable=True` only when a checkbox can finish
  it with `POST /todos/{id}/complete` (i.e. the target is a todo).
- Progress: `progress` dict gains a key per area (`business` already exists, computed
  from revenue vs the $10K target). A module may replace `_business_pct` with a richer
  computation but must keep the key an int 0–100.
- Alerts: add a rule to `context/core/off_track_rules.json` and, if it needs a new
  `check`, an evaluator in `orchestrator_tools._RULE_CHECKS`. Rules are data; reusing an
  existing check needs no code.
- The summary LLM call sees the top 12 items; keep titles short and reasons specific.

---

## 11. Tests — `backend/tests/`

- `conftest.py`: one shared in-memory SQLite engine, `Base.metadata.create_all`, the
  dependency override for `get_db`, one shared `client = TestClient(app)`. The autouse
  `fresh_db` fixture drops and recreates every table per test. **Tests never fire
  startup**; `test_startup_handler_runs_with_no_key` calls `main.startup()` directly and
  is the only exercise of the migration chain. Keep it passing.
- Every test module starts with the no-key fixture:
  ```python
  @pytest.fixture(autouse=True)
  def _no_key(monkeypatch):
      monkeypatch.delenv("OPENAI_API_KEY", raising=False)
      llm.reset_client()
      yield
      llm.reset_client()
  ```
- **Mock seams** (patch these, never the SDK):
  - parser: `patch("main.get_parse_response", return_value=payload)` then `client.post("/parse", …)`
    (`_parsed(**overrides)` helper builds a full payload);
  - orchestrator: `patch("openai_client._stream_completion", _scripted_stream(rounds))`
    with `_tool_round(tool, args)` / `_final_round(text)` helpers, then read SSE frames
    from `client.post("/orchestrator", …).text` (`test_stage2_changes.py`);
  - plain completions: `patch("llm.chat_text", return_value="...")` or `patch("llm.chat_completion")`;
  - embeddings: `patch("llm.embed_texts", side_effect=fake)` with a deterministic fake and
    `monkeypatch.setenv("OPENAI_API_KEY", "test")` so `retrieval.available()` is true;
  - market intel: `patch("market_intel.agent._llm_json", side_effect=router_fn)` and
    `patch("market_intel.adapters.search_all", return_value=[...])`;
  - HTTP to third parties: patch the module-level `httpx` call (`patch("module.httpx.post")`)
    or the adapter's `_search`/`fetch`. **No network in tests.**
- Direct DB access in tests: `db = TestSessionLocal()` … `db.close()`.
- File naming: `test_<module>.py` or `test_phase<N>_<module>.py`. Each spec's acceptance
  tests map one-to-one to test functions named after them.
- Fixtures are synthetic (`tests/fixtures/syllabus_sample.csv` is the model): plausible
  shape, invented values, no real names other than the user's own public ones.
- Date-dependent assertions compute from `date.today()`; never hardcode a date that will
  pass only this month.

---

## 12. Frontend conventions — `frontend/index.html`

- One file; pages are `<div class="page" id="p-<name>">`, navigated by `nav('<name>')`,
  rendered in `renderPage(page)` (one branch per page calling `render<Name>()`).
- `API_BASE` is `location.origin` locally, the Render URL otherwise. `window.fetch` is
  wrapped to add the bearer token and open the login gate on 401 — use plain `fetch`.
- **`escHtml()` on every piece of user or server text before `innerHTML`; `mdHtml()` for
  markdown.** Never interpolate raw strings.
- Changes render through `renderChangesStrip(changes, container)`; suggestions through
  `loadSuggestions()` / `renderSuggestions()`; toasts through `showToast(text, kind)`.
  A new module returning `changes` from an endpoint passes them to `renderChangesStrip`.
- Screens are for **viewing and one-tap corrections**: a tap maps to exactly one POST
  (`/syllabus/{id}/advance`, `/todos/{id}/complete`, `/suggestions/{id}/accept`,
  `/actions/{id}/confirm`). No forms with more than one field for data the assistant can
  take from conversation; the only multi-field form that exists is the CSV import modal
  (file + a few selects), and that is the ceiling.
- Luna palette tokens (`--glow` cyan for signal only, `--muted`, `--danger`, `--success`),
  Bricolage Grotesque for display numerals, Geist for body. Glass cards `.card`,
  `.card-title`, badges `.badge b-glow|b-muted|b-suc|b-danger`, small buttons `.ar-btn-sm`,
  `.sr-btn`, `.btn-danger`. Respect `prefers-reduced-motion`.
- Mobile first: 390 px wide must work with no horizontal scroll.

---

## 13. Postgres and dual-dialect pitfalls (collected)

| Pitfall | Rule |
|---|---|
| NULLs are distinct in Postgres unique indexes | `COALESCE(col, '')` in the index expression (m013) |
| `DATETIME` is not a Postgres type | `TIMESTAMP` in `ADD COLUMN`; `DATETIME` only in the never-run CREATE fallback |
| Booleans: SQLite `0/1`, Postgres `TRUE/FALSE` | write booleans through the ORM, never raw SQL literals; `== True` filters are fine |
| `ILIKE` is Postgres-only | `func.lower(col).contains(...)` or Python |
| `ADD COLUMN IF NOT EXISTS` is Postgres-only | `_helpers.add_column_if_missing` handles both |
| `AUTOINCREMENT` is SQLite-only | `{autoincrement}` placeholder in DDL strings |
| Casting free text to DATE fails on Postgres | backfill in Python, leave `NULL` on failure |
| `Query.get()` is deprecated in SQLAlchemy 2 | `db.query(M).filter(M.id == x).first()` |
| SQLite ignores `ON DELETE CASCADE` by default | do not rely on cascades for correctness; delete children explicitly or keep them |
| Naive UTC timestamps everywhere (`datetime.utcnow()`) | never mix in aware datetimes; the briefing uses local `datetime.now()` for the greeting only |
| `Date` columns compare with `date` objects | never compare against strings |
| JSON in TEXT columns | `json.dumps(..., default=str)` on write, tolerant `json.loads` on read |
| Startup schema check | every ORM column must exist in the live DB after migrations |
| Render free tier spins down after ~15 min idle; first request 30–60 s | nothing in-process can be relied on to run on a schedule (see ADR-0001) |
| APScheduler `BackgroundScheduler` already runs the vault sync in-process | best effort only while the process is awake; not a scheduler of record |
| Long tool calls (market intel 30–90 s) run inside the SSE stream | keep any new synchronous tool under ~60 s or run it as an agent job |

---

## 14. Checklist for a new module

A module is done when every box is ticked.

1. **Spec first**: purpose, friction (how conversation and one tap cover every input), tables,
   endpoints, tools, briefing items, migration, tests, cost, external needs.
2. `models.py`: ORM classes, snake_case tables, `created_at`/`updated_at` with utcnow defaults.
3. `migrations/mNNN_<topic>.py`: idempotent, `_helpers`, Postgres-valid `ADD COLUMN` types,
   `enable_rls` for the module's tables; registered in `main.startup()` before `_validate_schema`.
4. `backend/<module>.py`: primitives, then endpoints; `router` included in `main.py`.
5. Every write: `change_log.record_*` inside a batch (`batch_scope` or the assistant's).
   Deletes and outward actions: `pending_actions`, never direct.
6. Ambiguity → suggestions; low confidence → suggestions; no guessing writes.
7. LLM calls: through `llm.*` with a `purpose`, a cap, and the stable prompt first.
8. Tools: function + `_TOOL_DISPATCH` + `TOOL_SCHEMAS` + prompt paragraph (authority rule
   if it writes). Return `(result, summary)`; expected failures as `{"error": …}`.
9. Briefing: `_<module>_items` in a sane score band; alerts as JSON rules.
10. Retrieval: `index_one` on text the assistant should find later; keyword fallback.
11. Tests: `tests/test_<module>.py`, no-key fixture, mocked seams, one test per acceptance
    criterion, startup test still green; `python -m pytest -q` passes.
12. Frontend: a view page and one-tap actions only; `escHtml` everywhere; changes via
    `renderChangesStrip`.
13. Docs: update `docs/REBUILD_HANDOFF.md` §2 (endpoint shapes) or the module's spec, and
    `CLAUDE.md` tables (file role, env vars) if either changed.
14. One commit, message in the existing style (`Phase 5B.1: lead database and CSV import`).
