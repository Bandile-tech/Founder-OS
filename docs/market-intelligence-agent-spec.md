# Market Intelligence Agent — Implementation Spec

Status: approved for build (this session)
Date: 2026-07-07

## What this is

A standing research department inside Founder OS. Given an objective ("find painful
problems in Zambian SMEs where AI automation could create a valuable business"), it
runs a multi-role research pipeline over pluggable sources and returns structured,
evidence-backed opportunities. It **surfaces** findings; it never writes them into the
Opportunity Pipeline on its own. Promotion is a separate, explicit action.

## Integration point (from scoped inspection)

- **Tool registry**: `backend/orchestrator_tools.py`. A tool is a plain function
  `(db, **kwargs) -> (result_dict, summary_str)`, registered in `_TOOL_DISPATCH` and
  described in `TOOL_SCHEMAS`. `run_tool()` dispatches by name. The SSE loop in
  `openai_client.get_orchestrator_response()` (max 6 rounds) executes tools and streams
  `tool_call` / `tool_result` events. No changes to that loop are needed.
- **Routing**: model-driven, governed by `context/core/orchestrator_prompt.md`. New
  tools get routing + strict-usage sections there (the `add_todos` /
  `query_cold_archive` sections are the template for "explicit command only" tools).
- **Migrations**: `backend/migrations/m00X_*.py` — idempotent, dual-dialect
  (sqlite/postgresql), existence-checked, run from `main.py` `startup()` in order.
  New tables must also get `ENABLE ROW LEVEL SECURITY` on Postgres (m008 convention:
  RLS on + no policies = anon access denied; backend superuser connection bypasses).
- **ORM**: `backend/models.py`, `snake_case` table names, `Column` style as in
  `ColdArchiveChunk`. `_validate_schema()` in `main.py` raises if ORM columns are
  missing from the live DB, so the migration must create every ORM-declared column.
- **Reference tool end-to-end**: `query_cold_archive` → `vault_sync.search_vault` →
  result dict + short summary string. The new agent follows the same shape: heavy
  logic in its own module, thin tool wrapper in `orchestrator_tools.py`.
- **LLM plumbing**: `openai_client.client` (OpenAI SDK 1.30.1), `gpt-4o-mini`
  everywhere. `httpx` is already a dependency — used for the web adapter. No new
  dependencies.

## Data model (migration `m009_market_intel.py` + ORM models)

All tables get RLS enabled on Postgres inside m009 itself.

```
research_projects
  id            INTEGER PK
  title         VARCHAR NOT NULL          -- short label derived from objective
  objective     TEXT    NOT NULL          -- the raw research objective
  status        VARCHAR NOT NULL DEFAULT 'running'   -- running|completed|failed
  error         TEXT                      -- failure detail when status='failed'
  created_at    DATETIME
  completed_at  DATETIME

research_findings
  id                INTEGER PK
  project_id        INTEGER NOT NULL → research_projects.id (CASCADE)
  problem           TEXT    NOT NULL      -- specific pain, one paragraph
  industry          VARCHAR
  customer_segment  VARCHAR               -- e.g. "independent pharmacies, Lusaka"
  persona           VARCHAR               -- e.g. "owner-operator, 1-3 staff"
  discovery         TEXT                  -- JSON: pain description, frequency,
                                          --   financial impact, current workaround
  market_analysis   TEXT                  -- JSON: existing solutions, why they fail,
                                          --   landscape, attractiveness, trends
  founder_fit       TEXT                  -- JSON: why I can solve it, difficulty,
                                          --   mvp feasibility, business model
  evidence          TEXT                  -- JSON array: {source_type, ref, excerpt}
  scores            TEXT                  -- JSON: 7 axes /10 + overall
  overall_score     REAL                  -- denormalised for sorting
  first_customer_path TEXT                -- plausible path to customer #1
  status            VARCHAR NOT NULL DEFAULT 'surfaced'  -- surfaced|promoted|dismissed
  notes             TEXT
  created_at        DATETIME

opportunity_pipeline
  id           INTEGER PK
  finding_id   INTEGER NOT NULL UNIQUE → research_findings.id (CASCADE)
  stage        VARCHAR NOT NULL DEFAULT 'discovered'
               -- discovered|researching|validating|building|rejected
  notes        TEXT
  promoted_at  DATETIME
  updated_at   DATETIME

research_memory_notes        -- the agent's lesson memory (see Memory below)
  id                INTEGER PK
  slug              VARCHAR NOT NULL UNIQUE   -- kebab-case lesson id
  summary           VARCHAR NOT NULL          -- the one-line summary
  content           TEXT    NOT NULL          -- markdown body of the lesson
  times_reinforced  INTEGER DEFAULT 1
  created_at        DATETIME
  updated_at        DATETIME
```

JSON lives in TEXT columns (SQLite-compatible; identical on Postgres). Scores object:
`{pain_severity, frequency, financial_value, market_size, competition_gap,
founder_fit, speed_to_mvp, overall}` — each 0–10, overall = mean rounded to 1dp.

Memory is DB-backed (not loose files) because Render's disk is ephemeral — a
file-per-lesson directory would be wiped on every deploy. Each row *is* one note:
slug = filename, `summary` = the one-line summary at the top, `content` = the body.
Upsert by slug = "update existing notes rather than creating duplicates".

## New module: `backend/market_intel/`

### `adapters.py` — source adapter architecture

```python
class SourceAdapter:                       # interface
    name: str                              # e.g. "web", "war_room", "cold_archive"
    def search(self, query: str, limit: int = 5) -> list[dict]:
        """Return evidence items:
        {"source_type": self.name, "ref": <url|path|doc title>,
         "title": str, "excerpt": str}
        Must never raise — degrade to [] on any failure."""
```

Initial adapters, registered in `ADAPTERS: dict[str, SourceAdapter]`:

1. `WebSearchAdapter` — DuckDuckGo HTML endpoint via `httpx` (10s timeout), regex
   parse of result titles/URLs/snippets. Covers web research, industry reports,
   reviews, forums, public discussions (the Planner writes per-channel queries, e.g.
   `site:reddit.com`, `"review"`, `"report" filetype:pdf` hints, against this one
   transport). Network failure → `[]`, never an exception.
2. `WarRoomAdapter` — wraps the existing `main.search_context` keyword search over
   doctrine documents. Opens its own DB session (thread-safe).
3. `ColdArchiveAdapter` — wraps `vault_sync.search_vault` (Obsidian vault chunks).
   Opens its own DB session.

Adding a new source = new class + one registry entry. The agent core only ever
iterates `ADAPTERS` — it has no knowledge of individual sources.

### `agent.py` — role pipeline

Roles are separate LLM calls (all `gpt-4o-mini`, JSON mode), not one mega-prompt:

1. **Research Planner** — input: objective + memory digest (all note slugs+summaries)
   + founder profile. Output: 2–4 independent research threads, each with a focus
   question and per-adapter search queries. Skips angles memory says are dead ends.
2. **Researcher** (one per thread, run in parallel via `ThreadPoolExecutor`) —
   executes the thread's queries across all adapters, then one LLM call to extract
   candidate problems + supporting evidence from the raw excerpts. Threads never
   share a DB session; adapters open their own.
3. **Analyst** — merges/dedupes candidates across threads; for each produces problem
   discovery + market analysis + the first five score axes.
4. **Founder Advisor** — evaluates each against the founder profile
   (`context/core/founder_profile.md`, with a built-in default: Python/FastAPI, AI
   agents, web apps, SME automation, emerging markets, solo-founder constraints);
   adds founder-fit analysis, founder_fit + speed_to_mvp scores, first-customer path.
5. **Verifier** — adversarial pass: checks every claim against the collected evidence
   list; enforces the quality bar (specific customer + specific pain + evidence items
   + plausible first-customer path; rejects generic/"AI will change everything"
   filler and anything outside solo-founder execution). Rejected candidates are
   dropped with a reason; survivors get `overall_score` finalised.
6. **Lesson extraction** — one final call produces 0–3 lessons (slug, summary,
   content) which `memory.py` upserts.

Entry point:

```python
def run_market_research(db, objective: str, max_opportunities: int = 3) -> dict
```

Creates a `ResearchProject` (status `running`), runs the pipeline, persists surviving
findings as `ResearchFinding` rows with status `surfaced`, marks the project
`completed` (or `failed` with error), and returns
`{project_id, title, opportunities: [...], rejected_count, lessons_updated}`.
It does **not** touch `opportunity_pipeline`.

Runtime guard: planner capped at 4 threads, evidence per thread capped, total ~8–12
LLM calls. Runs synchronously inside the tool call (the SSE stream pauses on the
`tool_call` event, then emits `tool_result` — same as every existing tool, just
slower: ~30–90s).

### `memory.py`

- `memory_digest(db) -> str` — one line per note (`slug: summary`), fed to Planner.
- `upsert_lessons(db, lessons) -> int` — insert by slug or update existing note
  (bump `times_reinforced`, refresh summary/content).

## Orchestrator tools to register (tool 9 + tool 10)

```python
def run_market_research_agent(db, objective: str, max_opportunities: int = 3)
    # → (result dict as above, "N opportunities surfaced for '<objective>'")

def promote_research_opportunity(db, finding_id: int, notes: str = None)
    # → creates the OpportunityPipeline row at stage 'discovered',
    #   flips finding.status to 'promoted'. Idempotent (UNIQUE finding_id →
    #   returns 'already promoted'). This is the ONLY write path into the pipeline.
```

Both added to `_TOOL_DISPATCH` + `TOOL_SCHEMAS`. Prompt rules added to
`orchestrator_prompt.md`:

- `run_market_research_agent`: only on an explicit research command ("research…",
  "find problems/opportunities in…"). Never autonomously.
- `promote_research_opportunity`: strict-usage rules modeled on `add_todos` — only on
  explicit save/promote language referencing a surfaced finding ("save opportunity 2",
  "promote that one to the pipeline"). Never as a side effect of research.

## REST endpoints (read + explicit promote, matching main.py style)

- `GET  /research/projects` — projects newest-first with finding counts
- `GET  /research/projects/{project_id}` — project + its findings (full JSON fields parsed)
- `GET  /research/pipeline` — pipeline entries joined to findings
- `POST /research/promote` — `{finding_id, notes?}` → same function as the tool
  (the frontend "save this opportunity" button)
- `PATCH /research/pipeline/{id}` — `{stage, notes?}` to move discovered → … → rejected

Schemas (`schemas.py`): `PromoteFindingRequest`, `PipelineUpdateRequest`.

## Tests — `backend/tests/test_market_intel.py`

LLM calls and the web adapter mocked (existing test convention: tests patch the
OpenAI seam). Cover:

1. `run_market_research` creates project + surfaced findings; pipeline untouched.
2. Verifier rejection: unevidenced/generic candidate is dropped.
3. `promote_research_opportunity` creates pipeline row, flips status, is idempotent.
4. Tool dispatch: both names resolve via `run_tool`; unknown args don't crash.
5. Memory upsert: same slug updates rather than duplicates; digest renders.
6. Adapter contract: web adapter returns `[]` on network failure.

## Execution order

1. `backend/migrations/m009_market_intel.py` — four tables + indexes + RLS; register
   in `main.py` `startup()` after m007.
2. `backend/models.py` — four ORM models (must match m009 exactly, or
   `_validate_schema` kills startup).
3. `backend/market_intel/adapters.py` — interface + three adapters + registry.
4. `backend/market_intel/memory.py` — digest + upsert.
5. `backend/market_intel/agent.py` — role pipeline + `run_market_research`.
6. `backend/orchestrator_tools.py` — tools 9 & 10 + dispatch + schemas.
7. `context/core/orchestrator_prompt.md` — routing + strict-usage rules;
   `context/core/founder_profile.md` — editable founder profile.
8. `backend/schemas.py` + `backend/main.py` — REST endpoints + startup registration.
9. `backend/tests/test_market_intel.py` — run the suite.
10. Smoke test: boot the app against SQLite, verify migration + schema validation
    pass and endpoints respond.

## Non-goals (v1)

- No background/async job queue — research runs inside the tool call.
- No embeddings/vector search — keyword search parity with existing War Room search.
- No auto-refresh/scheduled research runs.
- No frontend UI work (endpoints are ready for it).
