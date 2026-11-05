# Phase 5 — Business

Status: specification, ready to build. Builder: Opus-sized sessions following
`docs/phases/BUILD_ORDER.md`. Every pattern referenced here is defined in
`docs/CONVENTIONS.md`; **this spec makes the design decisions so the builder makes none**.
Where a decision depends on something outside this repository it is marked
**VERIFY** and the fallback is stated.

Modules:

- **5A** Market intelligence v2 — the existing agent, made cheap, scored, deduplicated, and
  wired into the pipeline and the briefing.
- **5B** Prospecting and outreach — lead database, CSV import, segmentation, qualification,
  drafted outreach behind a human approval gate, follow-ups, conversion tracking.
- **5C** CRM — clients, contacts, deals, projects, payments and recurring revenue,
  communication history.
- **5D** Personal-brand analytics — content performance from real platform data only, ideas,
  content calendar.
- **5E** Project portfolio — what deserves attention, bottlenecks, what to pause.

Build order inside the phase: 5A → 5B → 5C → 5E → 5D (5D has the most external
uncertainty and nothing else depends on it). 5E depends on the `projects` table from 5C.

---

## 0. Decisions that apply to the whole phase

### 0.1 Friction principle, concretely

Every input in Phase 5 enters through **one of exactly three doors**:

1. **Conversation** (typed or spoken through the dock, routed by the orchestrator to a
   tool). This is the default for every statement of fact: "Wamu's paid K2,000 for the
   site", "spoke to the pharmacy owner, she wants a demo Friday", "pause the newsletter
   project".
2. **One tap** on a screen: Approve & send, Mark replied, Convert, Pause, Focus, Dismiss,
   Promote. Each tap is one POST that is logged and undoable.
3. **File import** for bulk data that exists elsewhere already: a leads CSV, a LinkedIn
   analytics export. Importers are upserts with counts, like `POST /syllabus/import`.

Nothing in Phase 5 has a form with more than one free-text field. If a builder finds
they need one, the design is wrong: the field belongs to a tool argument.

### 0.2 Money

- Every money column is a pair: `amount REAL` + `currency VARCHAR` (`USD` | `ZMW`), exactly
  as `revenue`. Normalise through `main._normalise_currency`; convert through
  `main._to_usd` (`ZMW_PER_USD`). Totals are reported both `by_currency` and `total_usd`.
- The $10K scoreboard and `progress.business` keep reading the `revenue` table. 5C writes a
  `revenue` row for every paid payment so nothing downstream changes.

### 0.3 Matching people and things by name

Reuse the threshold shape of `main.resolve_todo_completion`: score candidates with
word-overlap/`difflib`, accept only when top ≥ 0.75 **and** ≥ 0.2 ahead of the runner-up;
otherwise return `ambiguous: True` with up to 3 candidates and (where a write was
requested) create suggestions. Implement once as `business_common.fuzzy_pick(rows,
key_fn, fragment)` in `backend/business_common.py` and reuse in every module.

### 0.4 Who may write

| Writer | How | Logged as |
|---|---|---|
| Orchestrator tools | module primitives inside the assistant batch | `assistant` |
| UI one-tap endpoints | `batch_scope(db, "ui")` | `ui` |
| Importers | `batch_scope(db, "ui")`, one batch per import | `ui` |
| Phase 6 agents | module primitives inside `batch_scope(db, "agent:<key>")` | `agent:<key>` |

Nothing sends a message to a human outside the system without a `pending_actions` row
confirmed by the user (§5B.6).

### 0.5 Shared helper module — `backend/business_common.py`

```python
def fuzzy_pick(rows, key_fn, fragment, *, min_score=0.5, accept=0.75, gap=0.2) -> tuple[obj|None, list[tuple[float, obj]]]
def money(amount, currency) -> dict            # {"amount": float, "currency": "USD|ZMW", "usd": float}
def sum_usd(rows, amount_attr="amount", currency_attr="currency") -> float
def days_since(dt) -> int | None
```
Tests: `tests/test_phase5_common.py` (fuzzy accept / ambiguous / none; money conversion
with `ZMW_PER_USD` patched).

### 0.6 Ideal customer profile — `context/core/icp.md`

A plain-text file like `founder_profile.md`, read by 5B qualification and 5A's advisor.
Seed content (the builder writes this file verbatim, the user edits it later):

```
Ideal customer — Aether AI services (first-revenue phase)

WHO: owner-operated SMEs in Zambia / Southern Africa with 1–20 staff: bakeries, pharmacies,
salons, schools, clinics, churches, small logistics and retail. Decision maker reachable
directly (owner, GM, PA to the CEO).
PAIN: manual admin that costs hours weekly — bookings, stock, invoicing, customer replies,
reporting; no website or a dead one; WhatsApp as the only system.
BUDGET: K1,500–K15,000 one-off builds; K300–K2,000/month retainers. Pays by mobile money
or bank transfer.
CHANNEL: referral, WhatsApp, walk-in, LinkedIn for professionals. No tenders, no enterprise
procurement.
DISQUALIFIERS: needs hardware or on-site operations; procurement cycle > 1 month;
outside Zambia with no referral; wants equity instead of payment.
```

### 0.7 The Business page (UI, view and one-tap only)

The Business tab removed in Stage 6.5 comes back as **one page `p-business` with four
sub-tabs**: Pipeline (5B), Clients (5C), Portfolio (5E), Brand (5D). Research stays on the
Agents page. Every sub-tab is a list with status chips and at most one tap per row.
Details of each sub-tab are in the module sections. Built in two UI sessions at the end of
the phase (`BUILD_ORDER.md` S9, S10), not module by module.

### 0.8 Running cost baseline

Model prices from `llm._DEFAULT_PRICING` (gpt-5.4-mini: $0.25 / M input, $0.025 / M
cached, $2.00 / M output; embeddings $0.02 / M). An orchestrator turn costs ≈ $0.0013
(8k mostly-cached input, 300 output). Phase 5 adds the per-module figures below;
**the whole phase stays under $1 / month at the stated usage**, on top of ≈ $1.20 / month
for 30 assistant turns a day. Hosting: Render free, Supabase free, Netlify free (unchanged).

---

## 5A. Market intelligence v2

### Purpose
The agent (`backend/market_intel/`) works but is expensive to reason about: it hardcodes
`gpt-4o-mini`, bypasses usage logging, uses an unweighted mean score, re-surfaces the same
opportunity on every run, writes nothing to the change log, and its output goes nowhere
unless the user remembers to look at the Agents page. 5A fixes those six things and
gives findings a path into prospecting (5B) and the daily briefing.

### Friction
- Research is still commanded in conversation (`research <objective>`); nothing changes.
- Standing objectives are set in conversation ("keep researching Lusaka pharmacies every
  two weeks") → `schedule_research` tool. Phase 6B runs them; until then one tap **Run**
  on the Agents page.
- Each surfaced finding gets exactly two taps on the Agents page: **Promote** (exists) and
  **Dismiss**. Dismiss asks nothing; the reason is optional and comes from conversation if
  given.
- New `pursue`-grade findings show up in the briefing with "Promote or dismiss" as the
  suggested action.

### Changes to existing code

1. **`market_intel/agent.py` → `llm.py`.** Delete `MODEL`. `_llm_json(system, user,
   temperature=0.4, purpose="market_intel")` calls
   `llm.chat_completion([...], db=None, purpose=purpose, model=llm.research_model(),
   response_format={"type": "json_object"}, temperature=temperature)` and returns `{}` on
   any exception (unchanged behaviour). Each role passes its purpose:
   `market_intel_planner`, `market_intel_researcher`, `market_intel_analyst`,
   `market_intel_advisor`, `market_intel_verifier`, `market_intel_lessons`.
   Add to `llm.py`: `def research_model() -> str: return os.getenv("RESEARCH_MODEL") or assistant_model()`.
   Update `tests/test_market_intel.py::_fake_llm` to `def _fake_llm(system, user, temperature=0.4, **_)`.
2. **Run cost.** After the run, `cost_usd` and `llm_calls` on the project = sum of
   `llm_usage` rows with `purpose LIKE 'market_intel_%'` and `created_at` between
   `project.created_at` and `project.completed_at`. (Phase 6B replaces this with
   `llm_usage.agent_run_id`.)
3. **Scoring — `backend/market_intel/scoring.py`** (new):
   ```python
   WEIGHTS_PATH = context/core/opportunity_weights.json
   DEFAULT_WEIGHTS = {"pain_severity": 1.5, "frequency": 1.0, "financial_value": 1.5,
                      "market_size": 0.75, "competition_gap": 1.0, "founder_fit": 2.0, "speed_to_mvp": 1.25}
   def overall(scores: dict) -> float            # weighted mean of the 7 axes, 1 dp
   def recommendation(overall: float) -> str     # "pursue" >= 7.5, "watch" >= 6.0, else "park"
   ```
   `agent._finalize_scores` uses `scoring.overall`; the finding stores `recommendation`.
   Weights file is created by the builder with `DEFAULT_WEIGHTS`; missing/invalid file →
   defaults. Update the existing test that asserts `overall == 7.6` to compute the expected
   value with `scoring.overall`.
4. **Deduplication across runs.** Before persisting a finding: index existing findings in
   `embeddings` as `source_type="finding"` (`retrieval.index_one(db, "finding", f.id,
   f"{f.problem}\n{f.customer_segment}")` on create). `retrieval.search(db, text,
   ["finding"], limit=1)` with score ≥ 0.90 → duplicate. Keyword fallback (no key): same
   `customer_segment` (case-insensitive) and ≥ 60 % word overlap of `problem`. A duplicate
   is **not** inserted; the existing finding gets `times_seen += 1`, and the run result
   lists it under `"repeated": [{finding_id, problem}]`. Evidence from the new run is
   appended to the existing finding's `evidence` JSON (deduped by `ref`).
5. **Change log.** Finding creation → `record_create` ("Surfaced: <problem[:70]>");
   promote → `record_update` on the finding and `record_create` on the pipeline row;
   dismiss → `record_update`; pipeline stage change (`PATCH /research/pipeline/{id}`) →
   `record_update`. All inside `batch_scope(db, "assistant")` for tool calls and `"ui"` for
   endpoints. (`run_market_research` commits several times during a run; keep that — the
   change log batch spans commits because it lives on `db.info`.)
6. **Prompt.** The research-agent section of `context/core/orchestrator_prompt.md` gains:
   "When a run reports `repeated`, say which findings were seen before instead of
   re-presenting them. Use `schedule_research` only when Bandile asks for research to
   recur. Use `dismiss_research_finding` only when he says a finding is not worth pursuing."

### Tables (migration `m014_market_intel_v2.py`)

Columns added (Postgres-valid types):

| Table | Column | Type |
|---|---|---|
| `research_projects` | `cost_usd` | `REAL` |
| `research_projects` | `llm_calls` | `INTEGER` |
| `research_projects` | `objective_id` | `INTEGER` (FK research_objectives.id, nullable) |
| `research_findings` | `recommendation` | `VARCHAR` (`pursue|watch|park`) |
| `research_findings` | `times_seen` | `INTEGER DEFAULT 1` |
| `research_findings` | `dismissed_reason` | `TEXT` |
| `research_findings` | `reviewed_at` | `TIMESTAMP` |
| `opportunity_pipeline` | `next_action` | `TEXT` |
| `opportunity_pipeline` | `due_date` | `DATE` |

New table `research_objectives`:

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `objective` | TEXT NOT NULL | |
| `cadence_days` | INTEGER NOT NULL DEFAULT 14 | |
| `enabled` | BOOLEAN DEFAULT TRUE | ORM `Boolean` |
| `max_opportunities` | INTEGER DEFAULT 3 | |
| `last_run_at` | TIMESTAMP | |
| `last_project_id` | INTEGER | |
| `created_at`, `updated_at` | TIMESTAMP | |

Backfill in Python: every existing finding with `scores` JSON gets `recommendation =
scoring.recommendation(scoring.overall(scores))` (do not rewrite `overall_score`).
Index: `ix_research_findings_status` exists; add `ix_research_objectives_enabled`.
RLS on `research_objectives`. ORM: `ResearchObjective`; new columns on the three models.

### Endpoints

| Method & path | Body | Returns |
|---|---|---|
| `POST /research/findings/{id}/dismiss` | `{reason?}` | finding (serialised) + `change` |
| `GET /research/objectives` | | `[{id, objective, cadence_days, enabled, max_opportunities, last_run_at, last_project_id, due: bool}]` |
| `POST /research/objectives` | `{objective, cadence_days?, max_opportunities?}` | 201 objective |
| `PATCH /research/objectives/{id}` | `{objective?, cadence_days?, enabled?, max_opportunities?}` | objective |
| `POST /research/objectives/{id}/run` | | the `run_market_research` result; sets `last_run_at`, `last_project_id`, `project.objective_id` |
| `GET /research/projects/{id}` | | existing + `cost_usd`, `llm_calls`, `repeated[]` |
| `GET /research/pipeline` | | existing + `next_action`, `due_date`, `finding.recommendation` |
| `PATCH /research/pipeline/{id}` | existing + `next_action?`, `due_date?` | logged |

`due` on an objective = `enabled and (last_run_at is None or last_run_at + cadence_days <= now)`.
Objectives live in a new module `backend/market_intel/objectives.py` with its own
`router`; the existing `/research/*` endpoints in `main.py` stay where they are (no
relocation in this phase — moving them is churn with no user value).

### Orchestrator tools

| Tool | Args | Behaviour |
|---|---|---|
| `schedule_research` | `objective` (req), `cadence_days` (default 14), `max_opportunities` (default 3) | creates or updates (fuzzy match on objective ≥ 0.75) a `research_objectives` row; logged; summary "research scheduled every N days" |
| `dismiss_research_finding` | `finding_id` (req), `reason?` | status `dismissed`, `dismissed_reason`, `reviewed_at`; logged; idempotent |

Existing `run_market_research_agent` result gains `repeated`, `cost_usd`, and each
opportunity gains `recommendation`. `promote_research_opportunity` sets `reviewed_at`.
Prompt authority: both new tools are "explicit instruction only" (same wording style as
`promote_research_opportunity`).

### Briefing integration
`briefing._opportunity_items(db, today)`:
- Each finding with `status == "surfaced"`, `recommendation == "pursue"`, `reviewed_at IS
  NULL`, created in the last 14 days → category `opportunity`, target_type
  `research_finding`, score **35**, due_label `"{overall_score}/10"`, reason
  `"Verified opportunity, not yet reviewed"`, action `"Promote or dismiss"`. Max 3.
- Each pipeline entry in stage `discovered` or `researching` with no `updated_at` in 14
  days, or with `due_date < today` → category `opportunity`, target_type
  `opportunity_pipeline`, score **30** (overdue: **48**), reason
  `"Pipeline entry stale for N days"` / `"Next action overdue"`, action = `next_action` or
  `"Decide: validate or reject"`.
- Each objective `due` → category `opportunity`, target_type `research_objective`, score
  **12**, action `"Run it (one tap on Agents)"`. Phase 6B removes this item once the agent
  runs on schedule.
`counts.opportunities_new` added. Not counted in `attention_count`.

### Migrations
`m014_market_intel_v2.py` as above. Registered after m013. Postgres: all adds are
`ADD COLUMN IF NOT EXISTS` with the types in the table; backfill in Python.

### Acceptance tests — `tests/test_phase5a_market_intel.py`
1. Every LLM call in a mocked run is logged: set `OPENAI_API_KEY` with `monkeypatch`,
   patch `llm.get_client` to return a fake client whose `chat.completions.create` returns
   a response object with `choices[0].message.content` (JSON per role, keyed on the
   system prompt like `_fake_llm`) and a `usage` object, and assert six `llm_usage` rows
   with purposes starting `market_intel_`, the project's `cost_usd > 0` and
   `llm_calls == 6`. (Patching `llm.chat_completion` itself would skip the logging under
   test.)
2. `scoring.overall` with default weights on `{8,8,7,6,7,9,8}` returns the weighted value
   (compute in the test) and `recommendation` bands are correct at 7.5 and 6.0.
3. A second run whose researcher returns the same problem/segment does not create a second
   finding (keyword fallback, no key); `times_seen == 2`; result lists it under `repeated`.
4. With a fake embedder (`llm.embed_texts` patched, key set) two paraphrased problems
   dedupe when cosine ≥ 0.9 and do not when < 0.9.
5. Dismiss via endpoint and via tool: status `dismissed`, change recorded, undo restores
   `surfaced`.
6. Objectives: create via tool, `due` is true on creation, `POST /run` (mocked agent) sets
   `last_run_at` and the project's `objective_id`; `due` becomes false.
7. Briefing: a fresh `pursue` finding appears as an `opportunity` item with score 35 and
   disappears after promote or dismiss; a stale pipeline entry appears.
8. Existing `test_market_intel.py` passes with the fake accepting `**_`.

### Dependencies
None beyond the rebuild. `retrieval` for dedup (optional path).

### Estimated running cost
A run ≈ 6–10 calls, ~30k input / 8k output tokens ≈ **$0.03** on gpt-5.4-mini
(was ~$0.01 on gpt-4o-mini; set `RESEARCH_MODEL=gpt-4o-mini` to keep the old price).
Eight runs a month ≈ $0.25. Dedup embeddings: negligible.

### External accounts, approvals, payments
None. The web adapter still uses the DuckDuckGo HTML endpoint without a key — **VERIFY**
it still answers `POST https://html.duckduckgo.com/html/`; if it blocks, the adapter
already degrades to `[]` and the run proceeds on War Room and vault evidence only.

---

## 5B. Prospecting and outreach

### Purpose
A lead database that fills from CSV imports and conversation, segments and qualifies
prospects against the ICP, drafts outreach the user approves with one tap, reminds about
follow-ups through the existing task system, and tracks who converted. Nothing is ever
sent without a human saying yes.

### Friction
| Need | Door |
|---|---|
| Bulk prospects from a spreadsheet | `POST /leads/import` (file + optional column mapping) |
| One prospect met in person | conversation → `add_lead` |
| "Which of these are worth it?" | conversation → `qualify_leads` (or one tap **Qualify** on an unqualified segment) |
| Write the first message | conversation → `draft_outreach` |
| Change the draft | conversation ("shorter, mention the bakery site") → `draft_outreach` with `message_id` |
| Send | one tap **Approve & send** on the draft, or "send it" → pending action → "yes" |
| They replied | conversation → `log_lead_reply` or one tap **Replied** |
| Chase later | automatic follow-up todo on send (default +3 days) or conversation → `set_follow_up` |
| They became a client | one tap **Convert** or conversation → `convert_lead` |

### Tables (migration `m015_leads_outreach.py`)

`leads`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `name` | VARCHAR NOT NULL | person or business |
| `company` | VARCHAR | |
| `role` | VARCHAR | |
| `email` | VARCHAR | lower-cased on write |
| `phone` | VARCHAR | digits and leading `+` only on write |
| `whatsapp` | VARCHAR | same normalisation; defaults to `phone` |
| `linkedin_url` | VARCHAR | |
| `website` | VARCHAR | |
| `location` | VARCHAR | |
| `segment` | VARCHAR | free text, lower-cased, e.g. `lusaka pharmacies` |
| `source` | VARCHAR NOT NULL DEFAULT 'manual' | `csv:<import_id>` \| `manual` \| `conversation` \| `market_intel` |
| `status` | VARCHAR NOT NULL DEFAULT 'new' | `new` → `qualified` \| `disqualified` → `contacted` → `replied` → `converted` \| `lost` |
| `qualification_score` | INTEGER | 0–100 |
| `qualification_json` | TEXT | `{fit, reasons[], disqualifiers[], suggested_angle, model, scored_at}` |
| `opportunity_id` | INTEGER | FK research_findings.id, nullable |
| `client_id` | INTEGER | FK clients.id, set on convert |
| `notes` | TEXT | dated lines, like notes bodies |
| `last_contact_at` | TIMESTAMP | |
| `next_follow_up_at` | DATE | mirrors the open follow-up todo's due_date |
| `created_at`, `updated_at` | TIMESTAMP | |

Indexes: `ix_leads_status`, `ix_leads_segment`, `ix_leads_email`.
Dedup key on import and `add_lead`: `email` if present, else `whatsapp`/`phone` if present,
else (`lower(name)`, `lower(company)`).

`lead_imports`

| Column | Type |
|---|---|
| `id` INTEGER PK; `filename` VARCHAR; `created` INTEGER; `updated` INTEGER; `skipped` INTEGER; `total_rows` INTEGER; `mapping_json` TEXT; `segment` VARCHAR; `created_at` TIMESTAMP |

`outreach_messages`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `lead_id` | INTEGER NOT NULL | FK leads.id |
| `channel` | VARCHAR NOT NULL | `whatsapp` \| `email` \| `linkedin` \| `call` \| `other` |
| `direction` | VARCHAR NOT NULL DEFAULT 'out' | `out` \| `in` |
| `subject` | VARCHAR | email only |
| `body` | TEXT NOT NULL | |
| `status` | VARCHAR NOT NULL DEFAULT 'draft' | `draft` → `approved` → `sent` \| `failed`; `cancelled`; inbound rows are `received` |
| `version` | INTEGER DEFAULT 1 | revisions increment; `supersedes_id` points to the previous |
| `supersedes_id` | INTEGER | |
| `brief` | TEXT | what the user asked for ("intro, mention the bakery site") |
| `pending_action_id` | INTEGER | set when a send is awaiting confirmation |
| `approved_at`, `sent_at` | TIMESTAMP | |
| `external_id` | VARCHAR | SMTP message-id (6C) |
| `error` | TEXT | |
| `created_by` | VARCHAR NOT NULL DEFAULT 'assistant' | `assistant` \| `ui` \| `agent:<key>` |
| `created_at`, `updated_at` | TIMESTAMP | |

Index `ix_outreach_lead_id`, `ix_outreach_status`.

`todos` gains `lead_id INTEGER` (nullable) and `client_id INTEGER` (nullable, used by 5C).
Follow-ups are todos with `category='outreach'`, `source='outreach'`, `lead_id` set — so
the briefing, `complete_task`, undo and the home-screen checkbox already work.

RLS on `leads`, `lead_imports`, `outreach_messages`. ORM: `Lead`, `LeadImport`,
`OutreachMessage`; `Todo.lead_id`, `Todo.client_id`.

### CSV import — `leads.import_csv(db, text, *, segment=None, mapping=None, filename=None)`

- Header synonyms (case-insensitive, trimmed) → column:
  `name`: name, full name, contact, contact name, owner; `company`: company, business,
  organisation, organization, org; `role`: role, title, position; `email`: email, e-mail,
  mail; `phone`: phone, tel, telephone, mobile, cell, number; `whatsapp`: whatsapp, wa;
  `linkedin_url`: linkedin, linkedin url, profile; `website`: website, site, url;
  `location`: location, city, town, area; `segment`: segment, industry, category, type;
  `notes`: notes, comment, comments.
  `mapping` (JSON `{csv_header: column}`) overrides synonyms. Unmapped columns are
  appended to `notes` as `key: value` lines so nothing is lost.
- `segment` argument applies to rows without a mapped segment.
- Rows with no `name` and no `company` → skipped. Dedup as above: existing row updated with
  non-empty incoming values (never blanks over data), counted as `updated`.
- One `lead_imports` row; leads get `source=f"csv:{import_id}"`. All inside one
  `batch_scope(db, "ui")`; return `{import_id, created, updated, skipped, total_rows,
  segment, columns_mapped{}, batch_id}`.
- Fixture: `tests/fixtures/leads_sample.csv` — 8 synthetic rows: five distinct leads, one
  row that repeats an earlier lead's email, one row that repeats an earlier lead's phone
  (so `created=5, updated=2`), one blank row (`skipped=1`), a `WhatsApp` column, an
  unmapped `Source` column, mixed-case headers, a `+260` number and a `0977…` number.

### Qualification — `leads.qualify(db, lead_ids: list[int]) -> list[dict]`

- Deterministic pre-score (0–40): +15 reachable (email or whatsapp/phone), +10 has
  `company`, +10 `segment` present, +5 `location` contains a Zambian city or `zambia`
  (list in code: lusaka, kitwe, ndola, kabwe, livingstone, chingola, mufulira, chipata,
  solwezi, kasama, zambia).
- LLM score (0–60): **one call per lead**, `purpose="lead_qualify"`,
  `response_format=json_object`, `max_tokens=300`, system prompt = stable text + the ICP
  file content (ICP after the stable text so the cache prefix holds), user = the lead's
  fields. Schema returned: `{"fit": 0-60, "reasons": [..≤3], "disqualifiers": [..],
  "suggested_angle": "one sentence"}`. Clamp. On `{}`/exception: `fit=None`, status
  unchanged, result carries `error`.
- `qualification_score = pre + fit`; status: `disqualified` if `disqualifiers` non-empty
  and `fit < 15`, else `qualified` if score ≥ 50, else stays `new` (reported as
  `"below_threshold"`). Status changes are `record_update` ("Qualified <name> 72/100").
- **Cap: 20 leads per call** (tool and endpoint); more → `{"error": "max 20 per call"}`.
  No key → deterministic part only, `fit=None`, no status change, `skipped_llm: true`.

### Outreach drafts — `outreach.draft(db, lead, channel, brief, *, message_id=None)`

- One LLM call, `purpose="outreach_draft"`, `max_tokens=400`. System prompt (stable):
  voice rules — plain, specific, no hype, ≤ 90 words for WhatsApp, ≤ 150 for email, one
  clear ask, written as Bandile (student founder, proof of work: Wamu's Bakes & Cakes
  website), British English, no emojis. Then (dynamic, second system message) the ICP and
  `founder_profile.md`. User message: lead fields, `qualification_json.suggested_angle`,
  the brief, and for a revision the previous body plus the instruction.
- Returns an `outreach_messages` row with status `draft`, `version = previous + 1`,
  `supersedes_id`. `record_create` ("Drafted WhatsApp to <name>"). The previous draft
  becomes `cancelled` (`record_update`).
- Email drafts return `{"subject", "body"}` JSON (`response_format=json_object`); other
  channels plain text.

### The approval gate — the only way anything leaves

```
draft ──(tap Approve & send | "send it" → pending action → "yes")──► approved ──send──► sent | failed
  └──(tap Dismiss | "scrap it")──► cancelled
```

- `outreach.request_send(db, message)`: creates `pending_actions` row `kind="send_outreach"`,
  `table_name="outreach_messages"`, `record_id=message.id`, summary
  `"Send {channel} to {lead.name}: {subject or body[:60]}"`, TTL 24 h; sets
  `message.pending_action_id`. Returns the pending dict. **Deletes/sends nothing.**
- `actions.confirm_action` is refactored to dispatch on `kind`:
  `_EXECUTORS = {"delete": _execute_delete, "send_outreach": outreach.execute_send}`.
  `execute_send` sets `approved` → calls the channel adapter → `sent`/`failed`, writes
  `sent_at`, `last_contact_at`, status `contacted` on the lead, creates the follow-up todo
  (+3 days, text `"Follow up with {lead.name}"`, category `outreach`, `lead_id`), appends a
  `communications` row when 5C exists (feature-detect `hasattr(models, "Communication")`).
  Everything `record_*`, one batch.
- UI tap `POST /outreach/{id}/send` is the same as request + confirm in one step: it is a
  human tap, which **is** the approval. It refuses (`409`) unless status is `draft` or
  `approved`.
- The orchestrator path never calls `/send`; the prompt forbids it and the tool
  `request_send` only exists in the pending flavour.

### Channel adapters — `backend/outreach_channels.py`

```python
class Channel:               # interface
    name: str
    def send(self, db, message, lead) -> dict   # {"ok": bool, "external_id"?: str, "open_url"?: str, "error"?: str}
```
- `WhatsAppLinkChannel` (`whatsapp`): no API. Returns `ok=True`, `open_url =
  f"https://wa.me/{digits}?text={urlencode(body)}"`. The UI opens it in a new tab on the
  **Approve & send** tap and the message is marked `sent` at that moment (undo reverts to
  `approved`). Conversation-path sends return `open_url` in the tool result and the
  assistant says "Tap the link to open WhatsApp"; the UI renders `open_url` from
  `changes`/`result` as a button.
- `EmailChannel` (`email`): delegates to `integrations_email.send(...)` from Phase 6C. Until
  6C exists: `{"ok": False, "error": "email sending is not configured"}`; the message
  becomes `failed` with that error and the UI offers **Copy text**. The builder creates
  the adapter now with that behaviour and 6C fills it in.
- `ManualChannel` (`linkedin`, `call`, `other`): `ok=True`, no side effect; the user copies
  the text (UI **Copy**) and the tap marks it sent.
- No WhatsApp Business API, no Twilio, no scraping in this phase (see ADR-0004 and the
  external section below).

### Conversion — `leads.convert(db, lead) -> dict`
Creates a `clients` row (`name`, `company`, `status="active"`, `service` from the brief of
the last sent message if any, `lead_id`), sets `lead.client_id`, `lead.status="converted"`,
closes open follow-up todos (`done=True`, logged). Idempotent (already converted → returns
the client). With 5C present also creates the primary `contacts` row from the lead's
email/phone (feature-detected).

### Endpoints — `backend/leads.py` and `backend/outreach.py`

| Method & path | Body / query | Returns |
|---|---|---|
| `GET /leads` | `status?`, `segment?`, `q?` (fuzzy on name/company), `limit=100` | `[lead]` |
| `GET /leads/segments` | | `[{segment, total, by_status{}}]` |
| `GET /leads/{id}` | | lead + `messages[]` + `follow_ups[]` (open todos) |
| `POST /leads` | `{name, company?, role?, email?, phone?, whatsapp?, linkedin_url?, website?, location?, segment?, notes?}` | 201 lead + `change` (dedupe → 200 existing + `deduped: true`) |
| `PATCH /leads/{id}` | any lead field incl. `status`, `segment` | lead + `change` |
| `POST /leads/import` | multipart `file`, `segment?`, `mapping?` (JSON) | import counts |
| `POST /leads/qualify` | `{ids: [..≤20]}` or `{segment}` (first 20 unqualified) | `[{lead_id, score, status, fit, reasons, disqualifiers, suggested_angle, error?}]` + `changes` |
| `POST /leads/{id}/replied` | `{summary?}` | lead + `change` (status `replied`, follow-up todo done) |
| `POST /leads/{id}/convert` | | `{lead, client, changes}` |
| `GET /outreach` | `status?=draft`, `lead_id?` | `[message + lead_name]` |
| `GET /outreach/{id}` | | message |
| `POST /outreach/{id}/send` | | `{message, open_url?, follow_up_todo_id?, changes}` or 409 |
| `POST /outreach/{id}/cancel` | | message + `change` |
| `GET /outreach/stats` | `days=90` | `{by_segment: [{segment, leads, contacted, replied, converted, reply_rate, conversion_rate}], by_channel: [...], totals: {...}}` |

Lead serialisation: all columns, `qualification` parsed from JSON, `open_follow_up`
(`{todo_id, due_date}` or null), `last_message` (`{id, channel, status, created_at}`).

### Orchestrator tools

| Tool | Args | Behaviour |
|---|---|---|
| `add_lead` | `name` (req), `company?`, `role?`, `email?`, `phone?`, `whatsapp?`, `linkedin_url?`, `website?`, `location?`, `segment?`, `notes?` | create or dedupe-update; logged |
| `find_leads` | `query?`, `segment?`, `status?`, `limit=10` | read-only list for the model |
| `qualify_leads` | `lead_ids?[]` or `segment?` (≤ 20) | runs `leads.qualify`; summary "qualified 7 of 9 (2 disqualified)" |
| `draft_outreach` | `lead` (name fragment or id, req), `channel` (req, enum), `brief?`, `message_id?` (revision) | fuzzy pick (ambiguous → candidates); creates the draft; returns `{message_id, channel, subject, body, version}` |
| `request_send` | `message_id` (req) | pending action; summary "awaiting your yes" |
| `log_lead_reply` | `lead` (req), `summary` (req), `channel?` | inbound `outreach_messages` row (`direction=in`, `status=received`), lead `replied`, follow-up done, `notes` appended; logged |
| `set_follow_up` | `lead` (req), `date` (req, text like `set_exam_date`), `reason?` | creates/moves the follow-up todo; logged |
| `convert_lead` | `lead` (req) | `leads.convert` |

Prompt additions (`orchestrator_prompt.md`, new section "PROSPECTING AND OUTREACH"):
- Add leads only when Bandile names a person or business he intends to approach.
- Draft when asked; never send. "Send it" → `request_send`, then ask for a plain yes; only
  then `confirm_action`. Never call `confirm_action` for a send in the same turn.
- Qualify only on request or when he asks which leads are worth pursuing.
- When he reports a reply, call `log_lead_reply`; when he says when to chase, `set_follow_up`.
- `convert_lead` only on explicit "they're a client now / signed / paid".

### Briefing integration
- Follow-ups are todos → already ranked (overdue 90+, today 70+). No new code except the
  title: when `todo.lead_id` is set, `_task_items` appends ` · {lead.name}` is **not**
  done (the todo text already carries the name).
- `briefing._outreach_items`: drafts awaiting approval (`status in (draft, approved)` and
  no pending action older than 24 h) → category `outreach`, target_type
  `outreach_message`, score **38**, reason `"Draft waiting for your approval"`, action
  `"Approve & send or dismiss"`. Max 3. Leads `contacted` with `last_contact_at` > 7 days
  and no reply and no open follow-up → score **28**, reason `"No reply in N days"`, action
  `"Follow up or mark lost"`.
- `progress.business` unchanged. `counts.drafts_waiting` added.
- Alert rule (data only): `{"id": "outreach_stalled", "severity": "warn", "check":
  "no_outreach_sent", "params": {"window_days": 7, "min_qualified": 3}, "message": "At
  least 3 qualified leads but nothing sent in 7 days. Prospecting is stalling."}` with a
  new evaluator `_check_no_outreach_sent` in `orchestrator_tools._RULE_CHECKS`.

### Migrations
`m015_leads_outreach.py`: three tables, `todos.lead_id`, `todos.client_id`, indexes, RLS.
`DELETABLE` gains `"leads": (models.Lead, "name")` and `"outreach_messages":
(models.OutreachMessage, "body")`; `propose_delete` schema enum updated.

### Acceptance tests — `tests/test_phase5b_leads.py`
1. Import the fixture: counts `created=5, updated=2, skipped=1`; `+260977…` and `0977…`
   normalise and dedupe; unmapped `Source` column lands in `notes`; re-import → `created=0`.
2. `add_lead` twice with the same email → one row, `deduped: true`, non-empty fields merged.
3. Qualification with `llm.chat_text` patched to a JSON string: score = pre + fit; status
   `qualified` at ≥ 50, `disqualified` when disqualifiers and fit < 15; 21 ids → error;
   no key → `skipped_llm` and no status change.
4. `draft_outreach` (patched LLM) creates a draft; revising with `message_id` creates
   version 2 and cancels version 1; **no `outreach_messages` row is ever `sent` without a
   confirm or a tap**: after `request_send` the message is still `draft`/pending; a
   scripted orchestrator turn that calls `request_send` then `confirm_action` in the same
   turn is rejected by `execute_send` (`{"error": "confirmation must come in a later
   message"}`) — implement by storing the `thread` message id on the pending row and
   checking a newer user message exists.
5. `POST /outreach/{id}/send` on a WhatsApp draft → status `sent`, `open_url` starts with
   `https://wa.me/260977`, follow-up todo created due today+3 with `lead_id`; undo of the
   batch returns the message to `draft` and removes the todo.
6. Email send without 6C → `failed` with the configured-error, lead stays `qualified`.
7. `log_lead_reply` → inbound row, lead `replied`, follow-up todo done.
8. `convert_lead` → client row linked both ways, open follow-ups closed, idempotent.
9. Briefing shows a waiting draft (score 38) and a 10-day-unanswered lead (score 28);
   `outreach_stalled` fires with 3 qualified leads and no sends in 7 days, not with sends.
10. `GET /outreach/stats` computes reply and conversion rates per segment.

### Dependencies
`actions.confirm_action` refactor (done here). 5C for `communications`/`contacts`
(feature-detected). 6C for real email sending.

### Estimated running cost
Qualification ≈ 1.5k in / 300 out ≈ **$0.001 per lead**; 200 leads/month ≈ $0.20.
Drafts ≈ 2.5k in / 400 out ≈ **$0.0014 each**; 100 drafts/month ≈ $0.14. Total ≈ $0.35.

### External accounts, approvals, payments
- WhatsApp: **none** — `wa.me` deep links are free and need no account. (The WhatsApp
  Business Cloud API needs a Meta Business account, business verification and per-message
  fees; deliberately not used — ADR-0004.)
- Email: deferred to 6C (Gmail app password, free).
- LinkedIn messaging has no permitted API for personal accounts; manual channel only.

---

## 5C. CRM

### Purpose
One record of every client relationship: who they are (clients, contacts), what is being
sold (deals), what is being delivered (projects), what has been and will be paid
(payments, recurring revenue), and every conversation (communications). The `clients`
and `revenue` tables already exist; 5C extends rather than replaces them.

### Friction
- Facts arrive in conversation: "sent Wamu's the K2,000 invoice, due end of month" →
  `record_payment(status=due)`; "they paid" → `record_payment` or tap **Paid** on the due
  payment; "Mrs Banda is the new contact, 0977…" → `add_contact`; "the clinic deal moved to
  proposal, K8,000" → `update_deal`; "called the school about the portal" →
  `log_communication`.
- One tap: **Paid**, deal stage chips (tap the next stage), project **Delivered**.
- The parser is **not** changed. Revenue stated in a brain dump still becomes a `revenue`
  row as today; 5C adds a nightly-safe reconciliation (`payments.reconcile`) that links a
  `revenue` row with `client_id` to an open `due` payment of the same client and currency
  within ±5 % of the amount, marking it paid. Called from `GET /crm/summary` and the Phase
  6B job; idempotent.

### Tables (migration `m016_crm.py`)

`clients` gains: `lead_id INTEGER`, `currency VARCHAR DEFAULT 'ZMW'`, `tags VARCHAR`,
`health VARCHAR DEFAULT 'green'` (`green|amber|red`, computed by `crm.client_health`,
stored for listing). `status` values stay `prospect|active|closed|lost` and gain `paused`.

`contacts`

| Column | Type |
|---|---|
| `id` PK; `client_id` INTEGER NOT NULL (FK clients); `name` VARCHAR NOT NULL; `role` VARCHAR; `email` VARCHAR; `phone` VARCHAR; `whatsapp` VARCHAR; `is_primary` BOOLEAN DEFAULT FALSE; `notes` TEXT; `created_at`, `updated_at` TIMESTAMP |

`deals`

| Column | Type | Notes |
|---|---|---|
| `id` PK; `client_id` INTEGER NOT NULL; `title` VARCHAR NOT NULL | | |
| `stage` VARCHAR NOT NULL DEFAULT 'lead' | | `lead → proposal → negotiation → won | lost` |
| `amount` REAL; `currency` VARCHAR DEFAULT 'ZMW' | | |
| `probability` INTEGER | | default by stage: lead 10, proposal 40, negotiation 70, won 100, lost 0; overridable |
| `expected_close` DATE; `won_at` TIMESTAMP; `lost_reason` TEXT; `notes` TEXT | | |
| `project_id` INTEGER | | set when a won deal becomes a project |
| `created_at`, `updated_at` TIMESTAMP | | |

`projects` (shared with 5E — all columns created here)

| Column | Type | Notes |
|---|---|---|
| `id` PK; `title` VARCHAR NOT NULL | | |
| `kind` VARCHAR NOT NULL DEFAULT 'client' | | `client` \| `internal` |
| `client_id` INTEGER; `deal_id` INTEGER | | nullable |
| `status` VARCHAR NOT NULL DEFAULT 'active' | | `planned` \| `active` \| `paused` \| `delivered` \| `closed` \| `cancelled` |
| `priority` INTEGER DEFAULT 3 | | 1 high … 5 low |
| `expected_value` REAL; `currency` VARCHAR DEFAULT 'ZMW' | | |
| `effort_hours_week` REAL | | |
| `start_date` DATE; `due_date` DATE; `delivered_at` TIMESTAMP | | |
| `next_action` TEXT; `blocked_reason` TEXT | | |
| `last_activity_at` TIMESTAMP | | bumped by any update/communication/payment |
| `health` VARCHAR DEFAULT 'green' | | computed (5E) |
| `notes` TEXT; `created_at`, `updated_at` TIMESTAMP | | |

`payments`

| Column | Type | Notes |
|---|---|---|
| `id` PK; `client_id` INTEGER NOT NULL; `deal_id` INTEGER; `project_id` INTEGER | | |
| `title` VARCHAR | | "Deposit", "Invoice #12", "Nov retainer" |
| `amount` REAL NOT NULL; `currency` VARCHAR NOT NULL DEFAULT 'ZMW' | | |
| `status` VARCHAR NOT NULL DEFAULT 'due' | | `due` \| `paid` \| `overdue` (derived on read, stored by job) \| `cancelled` |
| `due_date` DATE; `paid_at` DATE | | |
| `revenue_id` INTEGER | | FK revenue.id, set when paid |
| `recurring_id` INTEGER; `period_start` DATE | | set for generated instalments; partial unique index on (`recurring_id`, `period_start`) `WHERE recurring_id IS NOT NULL` |
| `notes` TEXT; `created_at`, `updated_at` TIMESTAMP | | |

`recurring_revenue`

| Column | Type | Notes |
|---|---|---|
| `id` PK; `client_id` INTEGER NOT NULL; `title` VARCHAR NOT NULL | | |
| `amount` REAL NOT NULL; `currency` VARCHAR NOT NULL DEFAULT 'ZMW' | | |
| `interval` VARCHAR NOT NULL DEFAULT 'monthly' | | `monthly` \| `quarterly` \| `annual` |
| `day_of_month` INTEGER DEFAULT 1 | | |
| `next_due` DATE NOT NULL | | |
| `active` BOOLEAN DEFAULT TRUE; `ended_at` DATE | | |
| `created_at`, `updated_at` TIMESTAMP | | |

`communications`

| Column | Type | Notes |
|---|---|---|
| `id` PK; `client_id` INTEGER; `contact_id` INTEGER; `lead_id` INTEGER | | at least one set |
| `channel` VARCHAR NOT NULL | | `whatsapp|email|call|meeting|linkedin|other` |
| `direction` VARCHAR NOT NULL | | `out` \| `in` |
| `summary` TEXT NOT NULL | | one or two lines |
| `occurred_at` TIMESTAMP NOT NULL | | defaults now |
| `outreach_message_id` INTEGER | | |
| `source` VARCHAR DEFAULT 'conversation' | | `conversation|ui|outreach|email_sync|agent:<key>` |
| `created_at` TIMESTAMP | | |

Indexes: `ix_contacts_client`, `ix_deals_client`, `ix_deals_stage`, `ix_projects_status`,
`ix_payments_client`, `ix_payments_status`, `ix_payments_due`, `ix_recurring_next_due`,
`ix_communications_client`, `ix_communications_occurred`,
`CREATE UNIQUE INDEX IF NOT EXISTS ux_payments_recurring_period ON payments (recurring_id, period_start) WHERE recurring_id IS NOT NULL`
— a **partial** unique index (supported by SQLite and Postgres alike) so the many manual
payments with `recurring_id NULL` never collide. `_helpers.create_index` has no `WHERE`
parameter; execute this one statement with `conn.execute(text(...))` on both dialects.

Backfill: `clients.currency` = `'USD'` for existing rows (their `value` was dollars);
every existing `revenue` row with `client_id` gets a `paid` payment (`title="Imported"`,
`paid_at=revenue.date`, `revenue_id`) so history is in one place — idempotent by
`revenue_id`.

RLS on the six new tables. ORM: `Contact`, `Deal`, `Project`, `Payment`, `RecurringRevenue`,
`Communication`; new columns on `Client`. Retrieval: `communications.summary` and
`projects.notes` are indexed as `source_type="crm"` (`index_one`) so the assistant can
find "what did the clinic say about pricing".

### Primitives — `backend/crm.py`

```python
find_client(db, fragment) -> (client|None, candidates)      # fuzzy_pick over name+company
find_deal(db, fragment, client=None), find_project(db, fragment)
client_health(db, client) -> "green"|"amber"|"red"
#  red:   any payment overdue > 14 d, or project blocked, or no communication in 45 d while active
#  amber: payment overdue, or no communication in 21 d, or deal in negotiation > 30 d
add_contact(db, client, **fields) ; log_communication(db, *, client=None, lead=None, contact=None, channel, direction, summary, occurred_at=None, source)
create_deal(db, client, title, amount, currency, stage="lead", expected_close=None)
update_deal(db, deal, *, stage=None, amount=None, currency=None, probability=None, expected_close=None, lost_reason=None)
#  stage→won: won_at=now; creates a project (kind=client, title=deal.title, expected_value=amount) unless deal.project_id set
record_payment(db, client, amount, currency, *, status="paid", title=None, due_date=None, paid_at=None, deal=None, project=None)
mark_paid(db, payment, paid_at=None)   # creates the revenue row (source=client.name, notes="payment #id"), links revenue_id
add_recurring(db, client, title, amount, currency, interval, day_of_month, start=None)
generate_due(db, today) -> int         # for each active recurring with next_due <= today: create payment(due, period_start=next_due), advance next_due; idempotent via the unique index
reconcile(db) -> int                   # link stray revenue rows to open payments (see Friction)
refresh_statuses(db, today)            # due & due_date < today → overdue; stored
summary(db) -> dict                    # see GET /crm/summary
```
Every write is `record_*`; `last_activity_at` bumped on the client's projects when a
communication or payment is logged for that client.

### Endpoints

| Method & path | Body / query | Returns |
|---|---|---|
| `GET /clients` | `status?`, `q?` | existing shape + `currency`, `health`, `tags[]`, `contacts_count`, `open_deals`, `outstanding{by_currency,total_usd}`, `mrr{by_currency,total_usd}`, `last_contact_at` |
| `GET /clients/{id}` | | client + `contacts[]`, `deals[]`, `projects[]`, `payments[]`, `recurring[]`, `communications[]` (last 50) |
| `PATCH /clients/{id}` | existing + `currency?`, `tags?`, `status?` | logged |
| `POST /clients/{id}/contacts` | `{name, role?, email?, phone?, whatsapp?, is_primary?}` | 201 contact + change |
| `PATCH /contacts/{id}` | fields | contact + change |
| `GET /deals` | `stage?`, `client_id?` | `[deal + client_name + amount_usd]` |
| `POST /deals` | `{client_id, title, amount?, currency?, stage?, expected_close?}` | 201 deal + change |
| `PATCH /deals/{id}` | `{stage?, amount?, currency?, probability?, expected_close?, lost_reason?, notes?}` | deal + change (+ `project` when won) |
| `GET /payments` | `status?=due,overdue`, `client_id?`, `from?`, `to?` | `[payment + client_name + amount_usd]` |
| `POST /payments` | `{client_id, amount, currency, status?, title?, due_date?, deal_id?, project_id?}` | 201 + change |
| `POST /payments/{id}/paid` | `{paid_at?}` | payment + `revenue_id` + changes |
| `POST /payments/{id}/cancel` | | payment + change |
| `GET /recurring` | | `[recurring + client_name]` |
| `POST /recurring` | `{client_id, title, amount, currency, interval, day_of_month?, start?}` | 201 + change |
| `PATCH /recurring/{id}` | `{active?, amount?, currency?, interval?, day_of_month?, ended_at?}` | + change |
| `GET /communications` | `client_id?`, `lead_id?`, `limit=50` | list |
| `POST /communications` | `{client_id?, lead_id?, contact_id?, channel, direction, summary, occurred_at?}` | 201 + change |
| `GET /crm/summary` | | `{clients:{active, paused, total}, deals:{by_stage:{stage:{count, amount_usd}}, weighted_pipeline_usd}, payments:{due_usd, overdue_usd, overdue_count, paid_30d_usd}, mrr:{by_currency, total_usd}, revenue_30d_usd, health:{green, amber, red}}` — calls `generate_due`, `refresh_statuses`, `reconcile` first |
| `GET /revenue` | unchanged | unchanged |

### Orchestrator tools

| Tool | Args | Behaviour |
|---|---|---|
| `crm_lookup` | `query` (req), `kind?` enum `client|contact|deal|project|any` | read-only; returns the matched record(s) with `GET /clients/{id}`-style detail for a single clear match, candidates otherwise |
| `log_communication` | `who` (client or lead fragment, req), `summary` (req), `channel?` (default `whatsapp`), `direction?` (default `out`), `when?` (date text) | fuzzy pick across clients then leads; ambiguous → candidates; logged |
| `update_deal` | `deal` (fragment or id), `client?`, `stage?`, `amount?`, `currency?`, `expected_close?`, `lost_reason?`, `create?: bool` (default false) | update; when no match and `create` is true, create (requires `client` resolvable); won → project created and returned |
| `record_payment` | `client` (req), `amount` (req), `currency?` (default ZMW), `status?` (`paid`/`due`), `due_date?`, `title?` | `record_payment`; `paid` creates the revenue row |
| `crm_summary` | | `crm.summary(db)` |
| `add_contact` | `client` (req), `name` (req), `role?`, `email?`, `phone?`, `whatsapp?`, `is_primary?` | logged |

Prompt section "CLIENTS, DEALS AND PAYMENTS": log what Bandile states; `record_payment`
only when he states money was invoiced or received for a **client** (revenue in a brain
dump without an invoice context stays with `route_brain_dump`); `update_deal` with
`create: true` only when he explicitly describes a new deal; never change stages on your
own assessment.

### Briefing integration
`briefing._crm_items(db, today)` (after `generate_due`/`refresh_statuses`):
- Overdue payment → category `payment`, target_type `payment`, score **72 + min(days,14)**,
  due_label `"{days}d overdue"`, reason `"K{amount} from {client} overdue"`, action
  `"Chase it or mark paid"`.
- Payment due within 7 days → category `payment`, score **42**, action `"Expect payment"`.
- Deal in `negotiation` > 30 days or `expected_close < today` → category `deal`, score
  **40**, action `"Close or mark lost"`.
- Active client with no communication in 21 days → category `client`, score **26**, action
  `"Check in"`. Max 3.
- `progress.business` becomes `round(100 * revenue_usd_since_TEN_K_START / 10000)` as now,
  **plus** the payload gains `business: {revenue_pct, mrr_usd, outstanding_usd,
  overdue_count}` under a new key `business_detail` (progress stays an int).
- `counts.payments_overdue` added to `attention_count`.
- Alert rule: `{"id": "payment_overdue_14d", "severity": "critical", "check":
  "payment_overdue", "params": {"days": 14}, "message": "A client payment is more than
  14 days overdue. Chase it today."}` with evaluator `_check_payment_overdue`.

### Migrations
`m016_crm.py`: four `ADD COLUMN`s on `clients`, six tables, indexes incl. the partial
unique index, Python backfills (`clients.currency`, payments from revenue), RLS.
`DELETABLE` gains `contacts` (name), `deals` (title), `projects` (title), `payments`
(title), `recurring_revenue` (title), `communications` (summary).

### Acceptance tests — `tests/test_phase5c_crm.py`
1. Migration backfill: an existing `revenue` row with `client_id` yields exactly one paid
   payment on two consecutive runs; `clients.currency` is `USD` for pre-existing rows.
2. `record_payment(status=paid, K2000)` → payment paid + revenue row `2000 ZMW` with
   `client_id`; `GET /revenue total_usd` reflects `ZMW_PER_USD`; undo of the batch removes
   both rows.
3. `POST /payments/{id}/paid` on a due payment → `revenue_id` set; idempotent (second call
   no new revenue).
4. Recurring monthly from `start` two months ago → `generate_due` creates two due payments
   once; a third call creates none; `next_due` advanced.
5. `refresh_statuses` flips due→overdue for past `due_date`; `client_health` returns `red`
   at 15 days overdue, `amber` at 1 day.
6. `update_deal(stage=won)` creates a project linked both ways; second call does not.
7. `log_communication("Wamu's", ...)` resolves the client, bumps `last_activity_at` on its
   active project, writes a `communications` row; an ambiguous fragment ("the clinic" with
   two clinics) returns candidates and writes nothing.
8. `reconcile`: a `revenue` row with `client_id` and amount within 5 % of an open due
   payment marks it paid and links it; a different currency does not.
9. Briefing: overdue payment item score ≥ 72 and counted in `attention_count`; stale
   negotiation deal appears at 40; `payment_overdue_14d` fires at 15 days.
10. `GET /crm/summary` numbers match a hand-built fixture (two clients, three payments, one
    recurring).

### Dependencies
5B for `clients.lead_id` population (optional). 5E reads `projects`.

### Estimated running cost
No LLM calls of its own. ≈ $0. Retrieval indexing of communications: ~1 embedding per
logged communication, negligible.

### External accounts, approvals, payments
None. (Invoicing/mobile-money integration is out of scope; payments are recorded, not
collected.)

---

## 5D. Personal-brand analytics

### Purpose
Know which content works, keep the idea backlog and the publishing calendar in one place,
and never show a number that did not come from the platform. "Real platform data only"
means: official APIs, or the platform's own analytics export files. No scraping, no
estimates, no manually typed metrics.

### Decision on data access (summary of ADR-0004)
| Platform | v1 access | Why |
|---|---|---|
| YouTube | **YouTube Data API v3 with an API key** (public statistics of the user's own channel and videos). No OAuth. | free, 10,000 units/day quota, key-only for public data |
| LinkedIn | **Export importer**: the creator-mode "Content performance" / post analytics export (XLSX saved as CSV by the user) | personal-profile analytics have no API without Marketing Partner approval |
| X | Export importer **only if** the account can export analytics CSV — **VERIFY** (analytics export was moved behind Premium in 2024–25) | free API tier is write-only |
| Instagram / TikTok | **Not in v1.** Instagram insights need a Business/Creator account linked to a Facebook Page plus a Meta app in review; TikTok Display API needs app approval. Adapter interface left ready. | approval processes; not worth it before there is content |

Where a platform is not connected the UI says "Not connected" and the assistant says it
has no data. It never estimates.

### Friction
- Connect YouTube once: paste the channel id (or handle) in **Settings → Integrations**
  (one field) after `YOUTUBE_API_KEY` is set on Render. One tap **Sync**; Phase 6B syncs
  daily.
- LinkedIn/X: drop the export file on **Brand → Import**.
- Ideas and scheduling by conversation: "idea: a short on how I built Wamu's site" →
  `add_content_idea`; "post the Wamu's short on Friday" → `add_content_idea(scheduled_for)`
  or one tap **Schedule** → date picker (the single allowed date control).
- "What's working?" → `brand_performance`.

### Tables (migration `m017_brand.py`)

`social_accounts`

| Column | Type | Notes |
|---|---|---|
| `id` PK; `platform` VARCHAR NOT NULL | | `youtube|linkedin|x|instagram|tiktok` |
| `handle` VARCHAR; `external_id` VARCHAR | | YouTube channel id |
| `access` VARCHAR NOT NULL DEFAULT 'api_key' | | `api_key` \| `export` \| `oauth` |
| `config_json` TEXT | | adapter config (no secrets; the API key is an env var) |
| `status` VARCHAR NOT NULL DEFAULT 'connected' | | `connected` \| `error` \| `disconnected` |
| `last_synced_at` TIMESTAMP; `last_error` TEXT | | |
| `created_at`, `updated_at` | | |

`content_posts`

| Column | Type | Notes |
|---|---|---|
| `id` PK; `account_id` INTEGER NOT NULL | | |
| `external_id` VARCHAR NOT NULL | | unique with account_id |
| `url` VARCHAR; `title` VARCHAR; `kind` VARCHAR | | `video|short|post|article|thread` |
| `posted_at` TIMESTAMP | | |
| `topic_tags` VARCHAR | | comma list, set from `content_ideas` link or conversation |
| `idea_id` INTEGER | | FK content_ideas |
| `created_at`, `updated_at` | | |

`content_metrics` (append-only snapshots)

| Column | Type |
|---|---|
| `id` PK; `post_id` INTEGER NOT NULL; `captured_at` TIMESTAMP NOT NULL; `views` INTEGER; `likes` INTEGER; `comments` INTEGER; `shares` INTEGER; `saves` INTEGER; `impressions` INTEGER; `watch_time_s` INTEGER; `source` VARCHAR (`api|export`); `raw_json` TEXT |

`account_metrics` (append-only)

| Column | Type |
|---|---|
| `id` PK; `account_id` INTEGER NOT NULL; `captured_at` TIMESTAMP NOT NULL; `followers` INTEGER; `following` INTEGER; `posts_count` INTEGER; `total_views` INTEGER; `source` VARCHAR; `raw_json` TEXT |

`content_ideas`

| Column | Type | Notes |
|---|---|---|
| `id` PK; `title` VARCHAR NOT NULL; `angle` TEXT | | |
| `platform` VARCHAR | | nullable = any |
| `format` VARCHAR | | `short|video|post|article|thread` |
| `status` VARCHAR NOT NULL DEFAULT 'idea' | | `idea → drafted → scheduled → posted | dropped` |
| `scheduled_for` DATE | | the calendar |
| `posted_post_id` INTEGER | | FK content_posts when linked |
| `source` VARCHAR DEFAULT 'conversation' | | `conversation|inbox|agent:<key>` |
| `score` INTEGER | | optional 1–5 set in conversation |
| `created_at`, `updated_at` | | |

`brand_insights` (cached LLM text, one per platform per ISO week)

| Column | Type |
|---|---|
| `id` PK; `week_start` DATE NOT NULL; `platform` VARCHAR NOT NULL; `text` TEXT NOT NULL; `model` VARCHAR; `created_at` TIMESTAMP; unique (`week_start`, `platform`) |

Indexes on `content_posts(account_id, external_id)` unique, `content_metrics(post_id,
captured_at)`, `content_ideas(status, scheduled_for)`. RLS on all six. ORM classes
`SocialAccount`, `ContentPost`, `ContentMetric`, `AccountMetric`, `ContentIdea`,
`BrandInsight`.

### Adapters — `backend/brand/adapters.py`

```python
class PlatformAdapter:
    platform: str
    def fetch_account(self, account) -> dict | None     # {followers, following, posts_count, total_views, raw}
    def fetch_posts(self, account, since: datetime | None) -> list[dict]
        # [{external_id, url, title, kind, posted_at, metrics: {views, likes, comments, shares, saves, impressions, watch_time_s}, raw}]
    # Must never raise; return None / [] and set account.last_error through the caller.
```
- `YouTubeAdapter`: `httpx.get` to `https://www.googleapis.com/youtube/v3/channels`
  (`part=statistics,contentDetails&id=<channel>&key=`), then `playlistItems` on the
  uploads playlist (`part=contentDetails&maxResults=50`, paginate up to 200 videos), then
  `videos` (`part=statistics,snippet,contentDetails&id=<50 ids>`). `kind="short"` when
  duration ≤ 60 s (**VERIFY**: the API does not flag Shorts; duration is the accepted
  heuristic). Handle → channel id: `channels?forHandle=@handle` (**VERIFY** parameter name
  `forHandle`; fallback `search?type=channel&q=`). Quota: ~1 unit per list call → a full
  sync is < 20 units. `YOUTUBE_API_KEY` env var; missing → `last_error="YOUTUBE_API_KEY
  not set"`, status `error`.
- `ExportAdapter` base + `LinkedInExportAdapter`: parses the CSV the user saved from the
  XLSX export. Expected columns (**VERIFY against a real export**; the builder writes the
  parser from the fixture and the fixture mirrors LinkedIn's "Content" sheet as of the
  verification): `Post URL`, `Post publish date`, `Post title` (or first text),
  `Impressions`, `Reactions`/`Likes`, `Comments`, `Reposts`/`Shares`, `Engagement rate`.
  Header synonyms handled like the leads importer. Followers come from the "Followers"
  sheet if included, else skipped.
- `XExportAdapter`: same base, columns `Tweet id`, `Tweet permalink`, `Tweet text`,
  `time`, `impressions`, `engagements`, `retweets`, `replies`, `likes` (**VERIFY**
  availability before building; if unavailable, skip this adapter entirely).
- `brand.sync(db, account)`: upsert posts (by `account_id, external_id`), append one
  `content_metrics` row per post **only if** any metric changed since the last snapshot,
  one `account_metrics` row per sync if changed; set `last_synced_at`; on error set
  `status="error"`, `last_error`. Logged as `record_create` on new posts only (metric
  snapshots are not user-facing changes; they are not logged — they are append-only data).
- `brand.import_export(db, platform, text, filename)`: creates the `export` account if
  missing, runs the adapter on the text, same upsert; returns counts.

### Analytics — `backend/brand/analytics.py` (deterministic)

```python
performance(db, platform=None, days=90) -> dict
# {
#  "accounts": [{platform, handle, followers, followers_delta_30d, last_synced_at, status}],
#  "top_posts": [{post, views, engagement, engagement_rate, posted_at}]  # top 5 by engagement_rate then views, posts in window
#  "by_format": {"short": {count, median_views, median_engagement_rate}, ...},
#  "cadence": {"posts_in_window": n, "per_week": x, "gap_days_since_last": g},
#  "trend": [{week_start, posts, views, engagement}]  # last 12 weeks
# }
# engagement = likes+comments+shares+saves; engagement_rate = engagement / max(views or impressions, 1)
```
Latest snapshot per post is used; "no data" is `None`, never 0.

Weekly insight (optional, cached): `brand.insight(db, platform, week_start, refresh=False)`
→ one LLM call (`purpose="brand_insight"`, max 250 tokens) on the `performance` dict,
3–4 plain sentences: what worked, what did not, one experiment for next week. Cached in
`brand_insights`; regenerated only with `?refresh=1`. No key → `None`.

### Endpoints — `backend/brand/__init__.py` exposes `router`

| Method & path | Body / query | Returns |
|---|---|---|
| `GET /brand/accounts` | | `[account + latest account_metrics]` |
| `POST /brand/accounts` | `{platform, handle?, external_id?}` | 201 (YouTube resolves handle → id at creation; error → 400 with the adapter message) |
| `POST /brand/accounts/{id}/sync` | | `{posts_new, posts_updated, metrics_rows, status, last_error}` |
| `DELETE /brand/accounts/{id}` | | sets `status=disconnected`; nothing is deleted and the data stays (the UI shows a confirm dialog before calling it) |
| `POST /brand/import` | multipart `file`, `platform` | counts |
| `GET /brand/performance` | `platform?`, `days=90` | `performance()` + `insight` (cached text or null) |
| `GET /brand/posts` | `platform?`, `limit=50` | posts with latest metrics |
| `PATCH /brand/posts/{id}` | `{topic_tags?, idea_id?}` | + change |
| `GET /brand/ideas` | `status?` | ideas |
| `POST /brand/ideas` | `{title, angle?, platform?, format?, scheduled_for?}` | 201 + change |
| `PATCH /brand/ideas/{id}` | `{title?, angle?, platform?, format?, status?, scheduled_for?, score?, posted_post_id?}` | + change |
| `POST /brand/ideas/{id}/advance` | | one tap: `idea→drafted→scheduled` (needs `scheduled_for`, else 409) `→posted` |
| `GET /brand/calendar` | `from?`, `to?` (default this week + next) | `{days: [{date, ideas: [...], posted: [...]}]}` |
| `GET /brand/insight` | `platform`, `refresh=0|1` | `{week_start, text, cached}` |

### Orchestrator tools

| Tool | Args | Behaviour |
|---|---|---|
| `brand_performance` | `platform?`, `days=90` | `performance()` + cached insight; read-only; says "not connected" when no accounts |
| `add_content_idea` | `title` (req), `angle?`, `platform?`, `format?`, `scheduled_for?` (date text), `idea_id?` (to reschedule/update) | create or update; `scheduled_for` moves status to `scheduled`; logged |
| `mark_posted` | `idea` (fragment), `url?` | status `posted`; if `url` matches a known post's URL, links `posted_post_id`; logged |

Prompt section "PERSONAL BRAND": ideas only when he labels them ideas or asks to schedule;
report numbers only from `brand_performance`; when nothing is connected say so — never
estimate reach or engagement.

### Briefing integration
- Idea `scheduled_for == today` → category `content`, target_type `content_idea`, score
  **58**, due_label `Today`, action `"Post it, then say 'posted'"`; overdue scheduled ideas
  → score **66**, `"{n}d overdue"`.
- Account `status == error` → category `alert`-like item in `content`, score **20**, reason
  `last_error`, action `"Fix the connection in Settings"`.
- Cadence gap: last post > 14 days ago on a connected account → score **22**, action
  `"Pick an idea from the backlog"`.
- `progress.brand`: `None` when no account; otherwise `min(100, round(100 * posts_in_last_30d / 8))`
  (8 posts/month = 100 %). Document the target in the response as `brand_target_posts_30d: 8`.

### Migrations
`m017_brand.py`: six tables, indexes, RLS. No backfill.

### Acceptance tests — `tests/test_phase5d_brand.py`
1. YouTube adapter with `httpx.get` patched to canned JSON (channel, playlistItems with
   `nextPageToken`, videos): 2 pages → N posts, shorts flagged by duration, quota-safe
   batching of 50 ids; account metrics captured. No network.
2. `sync` twice with identical canned data → no new `content_metrics`; changed view count →
   exactly one new snapshot for that post.
3. Missing `YOUTUBE_API_KEY` → account `error`, `last_error` set, no exception.
4. LinkedIn export fixture (`tests/fixtures/linkedin_content_sample.csv`, 6 synthetic rows,
   header synonyms) imports; re-import updates, no duplicates.
5. `performance`: top posts by engagement rate, `None` where no metrics, `by_format`
   medians, 12-week trend length.
6. Insight cached once per platform-week (patched LLM counted), `refresh=1` regenerates;
   no key → `None`.
7. Ideas: tool creates; `scheduled_for` → `scheduled`; `advance` from `drafted` without a
   date → 409; `mark_posted` links by URL.
8. Briefing: idea scheduled today appears at 58; `progress.brand` is `None` with no account.
9. No endpoint or tool returns an estimated metric: `brand_performance` with an account but
   no metrics returns `None` fields (assert explicitly).

### Dependencies
None in-repo. 6B for daily sync; 6C Settings → Integrations card hosts the connect field.

### Estimated running cost
YouTube API: free within quota (a daily sync is < 20 of 10,000 units). Weekly insight:
≈ 4k in / 300 out ≈ **$0.002**; two platforms ≈ $0.02 / month. Embeddings: none.

### External accounts, approvals, payments
- **Google Cloud project with YouTube Data API v3 enabled and an API key** (free; restrict
  the key to that API). Env var `YOUTUBE_API_KEY` on Render. No OAuth, no verification,
  because only public data of the user's own channel is read. **VERIFY** the `forHandle`
  parameter and quota costs on the YouTube Data API reference.
- LinkedIn: no account changes; the user exports from LinkedIn creator analytics
  (**VERIFY** the export exists for their account type and its column names).
- X: **VERIFY** whether analytics export is available on the user's tier; otherwise skip.
- Instagram/TikTok: none in v1 (would need Meta Business verification / TikTok app review).

---

## 5E. Project portfolio

### Purpose
Rank every live project (client and internal) by how much attention it deserves right
now, name the bottleneck on each, and say which to pause — deterministically, from the
data the other modules already keep, so the answer is the same at 23:00 as at 06:00 and
costs nothing to compute.

### Friction
- Projects are created by 5C when a deal is won, or by conversation: "new internal
  project: Founder OS Phase 5, due end of Jan, high priority" → `update_project(create)`.
- Progress is logged by conversation: "shipped the booking page for the salon" →
  `log_project_update`; "blocked on the clinic's logo" → `update_project(blocked_reason)`.
- One tap on the Portfolio sub-tab: **Focus** (sets `priority=1`), **Pause**, **Resume**,
  **Delivered**. Each is one POST.
- The assistant's pause recommendations are **suggestions** (`kind="project_pause"`),
  accepted with one tap; nothing is paused automatically.

### Tables (migration `m018_portfolio.py`)

`projects` exists (5C). New table `project_updates`:

| Column | Type | Notes |
|---|---|---|
| `id` PK; `project_id` INTEGER NOT NULL | | |
| `kind` VARCHAR NOT NULL DEFAULT 'note' | | `note|milestone|blocker|unblocked|status` |
| `text` TEXT NOT NULL | | |
| `occurred_at` TIMESTAMP NOT NULL | | |
| `source` VARCHAR DEFAULT 'conversation' | | |
| `created_at` TIMESTAMP | | |

Index `ix_project_updates_project`. RLS. ORM `ProjectUpdate`. `projects.last_activity_at`
is bumped by every update, communication (via client), payment (via client/project) and
by any `record_update` on the row.

### Scoring — `backend/portfolio.py`

```python
def attention(db, project, today) -> dict:
    # components (all 0..n, documented so the UI can show "why")
    value_w   = min(1.0, usd(expected_value) / 5000) * 3            # 0..3
    urgency   = 4 if due_date and due_date < today else 3 if within 7d else 2 if within 14d else 1 if within 30d else 0
    staleness = 2 if days_since(last_activity_at) > 14 else 1 if > 7 else 0   # None activity counts as 30 days
    blocked   = 2 if blocked_reason else 0
    priority  = (6 - clamp(priority, 1, 5)) / 5 * 2                   # 2.0 for priority 1 … 0.4 for 5
    waiting   = 1 if last communication for the client is 'out' and > 7 days with no 'in'  else 0
    score     = round(value_w + urgency + staleness + blocked + priority + waiting, 2)
    return {"score", "components": {...}, "bottleneck": first truthy of
            ["blocked: {blocked_reason}", "overdue by N days", "waiting on client for N days",
             "no activity for N days", "payment overdue (K…)", None]}
def recommendation(project, attention, rank, today) -> str:
    # "focus"    rank <= 3 among active projects (by score)
    # "pause"    active and staleness == 2 and no due_date within 30d and value_w < 1.0   (i.e. < K? ~$1,667 / low value, stale, nothing due)
    # "close"    status == 'delivered' and delivered_at older than 30d
    # "maintain" otherwise
def review(db, today=None) -> dict:
    # {"ranked": [{project, attention, recommendation, bottleneck}], "paused": [...],
    #  "pause_suggestions": [...], "bottlenecks": [...], "totals": {active, paused, blocked, overdue, value_usd_active}}
def propose_pauses(db) -> list[dict]
    # for each 'pause' recommendation without an open project_pause suggestion → actions.add_suggestion(
    #   db, "project_pause", {"project_id": id, "title": title}, confidence=0.8,
    #   reason="Stale for N days, low value, nothing due", source="assistant")
```
`health` on the project: `red` if blocked or overdue, `amber` if staleness ≥ 1 or waiting,
else `green`; stored on every `review` call.

`actions.accept_suggestion` learns `project_pause` → `parsed["project_updates"] =
[{"project_id": …, "status": "paused"}]`, and `_apply_parse_updates_inner` applies
`project_updates` (status change, logged, `project_updates` row kind `status`). This is
the only parser-side addition in Phase 5 and it is not reachable from the LLM parser
schema (no prompt change) — it exists for suggestions only.

### Endpoints — `backend/portfolio.py` `router`

| Method & path | Body / query | Returns |
|---|---|---|
| `GET /portfolio` | `include_paused=1` | `review()` (also runs `propose_pauses` and stores `health`) |
| `GET /projects` | `status?`, `kind?`, `client_id?` | `[project + attention + client_name]` |
| `GET /projects/{id}` | | project + `updates[]` + `payments[]` + `communications[]` (via client, last 20) |
| `POST /projects` | `{title, kind?, client_id?, priority?, expected_value?, currency?, due_date?, effort_hours_week?, next_action?}` | 201 + change |
| `PATCH /projects/{id}` | any of the above + `status?`, `blocked_reason?` (empty string clears and writes an `unblocked` update) | + change |
| `POST /projects/{id}/status` | `{status}` one tap: `paused|active|delivered|closed` | + change + `project_updates` row |
| `POST /projects/{id}/focus` | | `priority=1`; every other active project with priority 1 drops to 2 (logged) |
| `POST /projects/{id}/updates` | `{text, kind?, occurred_at?}` | 201 + change |

### Orchestrator tools

| Tool | Args | Behaviour |
|---|---|---|
| `portfolio_review` | `include_paused?` | `review()` — read-only but creates `project_pause` suggestions; summary "3 focus, 1 blocked, 2 pause suggestions waiting" |
| `update_project` | `project` (fragment/id; req unless `create`), `create?: bool`, `title?`, `kind?`, `client?`, `status?`, `priority?`, `expected_value?`, `currency?`, `due_date?` (date text), `effort_hours_week?`, `next_action?`, `blocked_reason?` | fuzzy pick; ambiguous → candidates; `status=paused` from the model is **not allowed** (returns `{"error": "pausing needs a tap or an explicit 'pause X'"}` unless `explicit: true` which the prompt may set only when Bandile literally said pause); logged |
| `log_project_update` | `project` (req), `text` (req), `kind?` | `project_updates` row + `last_activity_at`; `kind=blocker` sets `blocked_reason`, `unblocked` clears it |

Prompt section "PROJECTS AND PORTFOLIO": the review is what to show for "what should I
work on this week / what should I pause"; recommendations are advice until he taps or
says so; `update_project(create: true)` only when he announces a new project.

### Briefing integration
`briefing._portfolio_items(db, today)` (calls `review` once per briefing build):
- Blocked project → category `project`, target_type `project`, score **62**, reason
  `"Blocked: {reason}"`, action `"Unblock or re-plan"`.
- Overdue project → score **70 + min(days, 9)**, `"{n}d overdue"`, action `"Deliver or re-date"`.
- Waiting on client > 7 d → score **36**, action `"Nudge {client}"`.
- Top `focus` project (rank 1) → score **45**, due_label `"FOCUS"`, reason = its bottleneck
  or `"Highest attention score"`, action = `next_action` or `"Decide the next action"`.
- `pause_suggestions` surface through the existing suggestions strip, not as items.
- `progress.projects`: `round(100 * delivered_or_closed_in_90d / max(1, delivered_or_closed_in_90d + active))`.
- Alert rule: `{"id": "projects_blocked", "severity": "warn", "check": "projects_blocked",
  "params": {"min": 2}, "message": "Two or more projects are blocked. Clear a bottleneck
  before starting anything new."}`; evaluator `_check_projects_blocked`.

### Migrations
`m018_portfolio.py`: `project_updates`, index, RLS. `DELETABLE` gains `project_updates`
(text).

### Acceptance tests — `tests/test_phase5e_portfolio.py`
1. Attention components on a fixture of five projects (overdue+blocked, due in 5 days high
   value, stale internal low value, fresh paused, delivered 40 days ago) produce the
   expected scores (compute with the formula in the test) and recommendations `focus ×3`,
   `pause ×1`, `close ×1`.
2. `review` creates exactly one `project_pause` suggestion per pause recommendation across
   repeated calls; accepting it pauses the project, writes a `status` update, and undo
   resumes it.
3. `update_project(status="paused")` from the tool without `explicit` → error and no
   change; with `explicit: true` → paused.
4. `log_project_update(kind="blocker")` sets `blocked_reason`, `health=red`,
   `last_activity_at` bumped; `unblocked` clears.
5. `POST /projects/{id}/focus` demotes other priority-1 projects; all logged in one batch;
   batch undo restores.
6. Briefing: blocked item 62, overdue ≥ 70, focus item 45; `projects_blocked` fires at 2.
7. Waiting-on-client detection uses the last `communications` row direction/age.

### Dependencies
5C (`projects`, `communications`, `payments`). 5A/5B not required.

### Estimated running cost
None. Purely deterministic.

### External accounts, approvals, payments
None.

---

## Phase 5 totals

| Module | New tables | New tools | LLM $/month (stated usage) | External |
|---|---|---|---|---|
| 5A | 1 (+9 columns) | 2 | ≈ 0.25 | none |
| 5B | 3 (+2 columns) | 8 | ≈ 0.35 | none (wa.me links) |
| 5C | 6 (+4 columns) | 6 | ≈ 0 | none |
| 5D | 6 | 3 | ≈ 0.02 | Google Cloud API key (free) |
| 5E | 1 | 3 | 0 | none |
| **Total** | **17 tables, m014–m018** | **22 (registry → 44)** | **≈ $0.65** | |

Env vars added: `RESEARCH_MODEL` (optional), `YOUTUBE_API_KEY`.
Files added: `backend/business_common.py`, `backend/leads.py`, `backend/outreach.py`,
`backend/outreach_channels.py`, `backend/crm.py`, `backend/portfolio.py`,
`backend/brand/{__init__,adapters,analytics}.py`, `backend/market_intel/{scoring,objectives}.py`,
`context/core/icp.md`, `context/core/opportunity_weights.json`, five migrations, five
test modules and three fixtures.
