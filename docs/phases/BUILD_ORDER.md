# Build order — Phases 5 and 6

Twenty-two sessions, each sized for one Opus session (one module slice, one migration at
most, one commit, suite green). Run them in order; the **"Must exist first"** line on each
session is the hard dependency. Specs: `docs/phases/PHASE_5_SPEC.md`,
`docs/phases/PHASE_6_SPEC.md`. Patterns: `docs/CONVENTIONS.md`. Decisions: `docs/adr/`.

## How to run a session

1. Start a fresh session on the integration branch (currently
   `claude/rebuild-spec-review-vfrg4o`; after merge, `main`). Branch per session is
   optional; one commit per session is required.
2. Paste the session's prompt verbatim. Every prompt is self-contained: it names the spec
   section, the files, the tests and the commit message.
3. The session ends when the **Definition of done** holds and the commit exists. If the
   session cannot finish, it must leave the suite green and write what is missing into
   `docs/phases/PROGRESS.md` (created by S1) rather than commit half a feature.
4. Before moving on, the next session reads `docs/phases/PROGRESS.md` (two lines per
   finished session: what shipped, what was deferred).

Every prompt starts with the same eight rules. They are repeated in each prompt so a
prompt can be pasted on its own.

---

## Phase 5

### S1 — 5A.1 Market intelligence on the gateway: logging, scoring, dedup, change log

**Must exist first:** the rebuild branch (Stages 1–6), suite green (391 tests).

**Files touched:** `backend/llm.py` (`research_model`), `backend/market_intel/agent.py`,
`backend/market_intel/scoring.py` (new), `backend/models.py`,
`backend/migrations/m014_market_intel_v2.py` (new), `backend/main.py` (register m014),
`context/core/opportunity_weights.json` (new), `backend/tests/test_market_intel.py`
(fake accepts `**_`; expected overall via `scoring.overall`),
`backend/tests/test_phase5a_market_intel.py` (new), `docs/phases/PROGRESS.md` (new).

**Definition of done:** no `gpt-4o-mini` literal remains in `market_intel/`; six
`llm_usage` rows per mocked run with `market_intel_*` purposes; `research_projects.cost_usd`
and `llm_calls` set; `recommendation` on every finding, backfilled for existing rows;
dedup by keyword and by embeddings (tests 1–5 and 8 of §5A); findings and promotions
appear in `GET /changes`; `python -m pytest -q` green; startup chain runs
(`python -c "import asyncio, main; asyncio.run(main.startup())"`); one commit
`Phase 5A.1: market intelligence on the LLM gateway, weighted scoring, dedup`.

**Prompt:**
```
You are building Founder OS, a FastAPI + SQLite/Postgres + single-file-frontend personal
operating system. Rules for this session:
1. Read docs/CONVENTIONS.md in full, then docs/phases/PHASE_5_SPEC.md §0 and §5A in full,
   before writing any code. Follow them exactly; make no design decisions of your own —
   if the spec is silent, choose the simplest option and write it down in
   docs/phases/PROGRESS.md.
2. Python 3.12. Tests never reach OpenAI or the network; the suite must pass with no
   OPENAI_API_KEY set. Run `cd backend && python -m pytest -q` before you commit.
3. Every write goes through change_log inside a batch; every LLM call goes through llm.py
   with a purpose; every new table/column has a migration registered in main.startup()
   before _validate_schema, with RLS, and Postgres-valid ADD COLUMN types.
4. Do not touch the frontend in this session. Do not delete or rename anything.
5. Work in backend/ with lazy `import main` inside functions to avoid circular imports.
6. After the migration, run both `python -m pytest -q` and
   `python -c "import asyncio, main; asyncio.run(main.startup())"` from backend/.
7. One commit at the end, message exactly: `Phase 5A.1: market intelligence on the LLM
   gateway, weighted scoring, dedup`. No model names in the commit or in code comments.
8. If you cannot finish, leave the suite green, commit nothing half-done, and write what
   is missing in docs/phases/PROGRESS.md.

Task — implement §5A "Changes to existing code" items 1–5 and the m014 migration:
- llm.research_model() (env RESEARCH_MODEL, default assistant_model()).
- market_intel/agent.py: delete MODEL; _llm_json(system, user, temperature=0.4,
  purpose="market_intel") → llm.chat_completion(... response_format json_object ...),
  returning {} on any exception; each role passes its purpose (planner, researcher,
  analyst, advisor, verifier, lessons). Set project.cost_usd and llm_calls from llm_usage
  rows with purpose LIKE 'market_intel_%' between created_at and completed_at.
- market_intel/scoring.py with DEFAULT_WEIGHTS, overall(), recommendation(); weights file
  context/core/opportunity_weights.json; agent uses scoring.overall and stores
  finding.recommendation.
- Dedup before insert: embeddings source_type "finding" (retrieval.index_one on create;
  retrieval.search ≥ 0.90), keyword fallback (same customer_segment, ≥ 60 % word overlap
  of problem); duplicates increment times_seen, append new evidence by ref, are reported
  under result["repeated"], and are not inserted.
- change_log.record_create on finding insert, record_update on promote/dismiss/pipeline
  stage change, record_create on the pipeline row (batch_scope "assistant" in tools,
  "ui" in endpoints).
- models.py + m014: the nine columns and the research_objectives table from §5A "Tables"
  (create the table and ORM model now; endpoints and tools come in S2). Python backfill of
  recommendation for existing findings.
- Update tests/test_market_intel.py as the spec says; add tests/test_phase5a_market_intel.py
  with §5A acceptance tests 1, 2, 3, 4, 5 (dismiss via a primitive is enough — the endpoint
  comes in S2; test the primitive), and 8.
- Create docs/phases/PROGRESS.md with a "S1" entry: what shipped, what was deferred.
```

---

### S2 — 5A.2 Standing objectives, dismiss, tools, briefing items

**Must exist first:** S1.

**Files touched:** `backend/market_intel/objectives.py` (new, router), `backend/main.py`
(include router; `/research/findings/{id}/dismiss`; extend project/pipeline responses),
`backend/orchestrator_tools.py` (`schedule_research`, `dismiss_research_finding`,
`run_market_research_agent` result additions), `context/core/orchestrator_prompt.md`,
`backend/briefing.py` (`_opportunity_items`), `backend/tests/test_phase5a_market_intel.py`
(tests 6–7), `docs/REBUILD_HANDOFF.md` §2 (new endpoints), `docs/phases/PROGRESS.md`.

**Definition of done:** objectives CRUD and `/run` work; `due` computed; two tools
registered in function + dispatch + schema + prompt; briefing shows `opportunity` items
at the specified scores and `counts.opportunities_new`; tests 6–7 pass; suite green;
commit `Phase 5A.2: standing research objectives, dismiss, briefing items`.

**Prompt:**
```
You are building Founder OS (FastAPI backend in backend/, single-file frontend). Rules:
1. Read docs/CONVENTIONS.md, docs/phases/PHASE_5_SPEC.md §0 and §5A, and
   docs/phases/PROGRESS.md first. Make no design decisions of your own.
2. Python 3.12; tests never reach OpenAI/network; suite green with no key.
3. Writes through change_log in a batch; LLM calls through llm.py; tools registered in
   function + _TOOL_DISPATCH + TOOL_SCHEMAS + a prompt paragraph with authority rules.
4. No frontend changes. No deletions or renames.
5. Lazy `import main` inside functions.
6. Run `python -m pytest -q` and the startup one-liner from backend/ before committing.
7. One commit: `Phase 5A.2: standing research objectives, dismiss, briefing items`.
8. If blocked, keep the suite green, commit nothing half-done, record it in PROGRESS.md.

Task — implement the rest of §5A:
- backend/market_intel/objectives.py with a router: GET/POST/PATCH /research/objectives,
  POST /research/objectives/{id}/run (calls run_market_research, sets last_run_at,
  last_project_id and project.objective_id), `due` as defined. Include the router in
  main.py beside the others.
- POST /research/findings/{id}/dismiss in main.py next to the other /research endpoints,
  using the dismiss primitive from S1; extend GET /research/projects/{id} (cost_usd,
  llm_calls, repeated) and GET/PATCH /research/pipeline (next_action, due_date,
  finding.recommendation; PATCH logged).
- Tools schedule_research and dismiss_research_finding exactly as the §5A table; extend
  run_market_research_agent's result and summary; prompt additions as written in §5A
  "Changes" item 6, plus explicit-instruction-only wording for both tools.
- briefing._opportunity_items per §5A "Briefing integration" (scores 35 / 30 / 48 / 12,
  categories, target types, max 3 findings), counts.opportunities_new; not counted in
  attention_count.
- Tests 6 and 7 of §5A. Update docs/REBUILD_HANDOFF.md §2 with the new endpoint shapes
  and append the S2 entry to PROGRESS.md.
```

---

### S3 — 5B.1 Lead database, CSV import, segmentation

**Must exist first:** S1 (for `PROGRESS.md`); S2 not required.

**Files touched:** `backend/business_common.py` (new), `context/core/icp.md` (new),
`backend/models.py` (`Lead`, `LeadImport`, `OutreachMessage`, `Todo.lead_id`,
`Todo.client_id`), `backend/migrations/m015_leads_outreach.py` (new), `backend/main.py`
(register m015; include `leads` router), `backend/leads.py` (new: serialize, dedupe,
import_csv, create, find, endpoints `GET /leads`, `GET /leads/segments`, `GET /leads/{id}`,
`POST /leads`, `PATCH /leads/{id}`, `POST /leads/import`), `backend/orchestrator_tools.py`
(`add_lead`, `find_leads`), `context/core/orchestrator_prompt.md` (section stub),
`backend/actions.py` (`DELETABLE` + `propose_delete` enum: leads, outreach_messages),
`backend/tests/fixtures/leads_sample.csv` (new), `backend/tests/test_phase5_common.py`
(new), `backend/tests/test_phase5b_leads.py` (new, tests 1–2), `docs/phases/PROGRESS.md`.

**Definition of done:** fixture imports with `created=5, updated=2, skipped=1`;
re-import creates nothing; phone normalisation and dedupe keys as specified; `add_lead`
dedupes; `business_common.fuzzy_pick` tested; migration registered; all three tables
created with RLS even though outreach endpoints come in S4; suite green; commit
`Phase 5B.1: lead database, CSV import, segmentation`.

**Prompt:**
```
You are building Founder OS (FastAPI backend in backend/). Rules:
1. Read docs/CONVENTIONS.md, docs/phases/PHASE_5_SPEC.md §0 and §5B, and PROGRESS.md
   first. Make no design decisions of your own; where the spec is silent pick the
   simplest option and note it in PROGRESS.md.
2. Python 3.12; tests never reach OpenAI/network; suite green with no key.
3. Writes through change_log in a batch; new tables via a migration registered in
   main.startup() before _validate_schema, with RLS; ADD COLUMN types Postgres-valid.
4. No frontend changes. No deletions or renames.
5. Lazy `import main` inside functions.
6. Run `python -m pytest -q` and the startup one-liner from backend/ before committing.
7. One commit: `Phase 5B.1: lead database, CSV import, segmentation`.
8. If blocked, keep the suite green, commit nothing half-done, record it in PROGRESS.md.

Task — §5B through "CSV import" plus the shared helpers:
- backend/business_common.py: fuzzy_pick, money, sum_usd, days_since exactly as §0.5;
  tests/test_phase5_common.py.
- context/core/icp.md with the seed text from §0.6 verbatim.
- models.py: Lead, LeadImport, OutreachMessage (all columns from §5B "Tables", even
  though outreach logic arrives in S4), Todo.lead_id, Todo.client_id.
- migrations/m015_leads_outreach.py: three tables, todos.lead_id and todos.client_id
  (INTEGER), the three indexes, RLS for all module tables on every run; register in
  main.startup() after m014.
- backend/leads.py: serialize (per §5B "Endpoints" note), normalisation (email lower;
  phone/whatsapp digits + leading '+', '0977…' kept as typed digits — do NOT add a country
  code), dedupe key rules, create_lead (dedupe → update non-empty fields, deduped=true),
  import_csv with the header-synonym table, mapping override, unmapped columns to notes,
  one LeadImport row, batch_scope "ui"; endpoints GET /leads (status, segment, q, limit),
  GET /leads/segments, GET /leads/{id} (messages[] and follow_ups[] may be empty lists for
  now), POST /leads, PATCH /leads/{id}, POST /leads/import (multipart: file, segment?,
  mapping? JSON string). Include the router in main.py.
- Tools add_lead and find_leads (function + dispatch + schema) and a "PROSPECTING AND
  OUTREACH" section in the prompt with the first bullet of §5B's prompt additions (add
  leads only when Bandile names someone he intends to approach); the other bullets come
  in S4/S5.
- actions.DELETABLE + propose_delete enum gain leads and outreach_messages.
- tests/fixtures/leads_sample.csv: 8 synthetic rows as described (two email duplicates,
  one phone duplicate, one blank row, a WhatsApp column, an unmapped Source column,
  mixed-case headers, one +260 number and one 0977 number). Tests 1 and 2 of §5B in
  tests/test_phase5b_leads.py. Append S3 to PROGRESS.md.
```

---

### S4 — 5B.2 Qualification, outreach drafts, channels, the approval gate, follow-ups

**Must exist first:** S3.

**Files touched:** `backend/leads.py` (`qualify`, `POST /leads/qualify`),
`backend/outreach.py` (new: draft, request_send, execute_send, cancel, endpoints
`GET /outreach`, `GET /outreach/{id}`, `POST /outreach/{id}/send`,
`POST /outreach/{id}/cancel`), `backend/outreach_channels.py` (new: Channel,
WhatsAppLinkChannel, EmailChannel stub, ManualChannel), `backend/actions.py`
(`confirm_action` → `_EXECUTORS` dispatch; `send_outreach` kind; "confirmation must come
in a later message" check), `backend/orchestrator_tools.py` (`qualify_leads`,
`draft_outreach`, `request_send`, `set_follow_up`), `context/core/orchestrator_prompt.md`,
`backend/main.py` (include outreach router), `backend/tests/test_phase5b_leads.py`
(tests 3–6), `backend/tests/test_stage2_changes.py` (delete confirmation tests still pass
after the refactor), `docs/phases/PROGRESS.md`.

**Definition of done:** no `outreach_messages` row reaches `sent` except through
`POST /outreach/{id}/send` or a confirmed pending action; the same-turn confirm is
rejected; WhatsApp `open_url` correct; follow-up todo created on send with `lead_id`;
email channel fails cleanly; existing delete-confirmation tests unchanged and green;
commit `Phase 5B.2: qualification, outreach drafts, approval gate, follow-ups`.

**Prompt:**
```
You are building Founder OS (FastAPI backend in backend/). Rules:
1. Read docs/CONVENTIONS.md (especially §3 and §4), docs/phases/PHASE_5_SPEC.md §0 and
   §5B, and PROGRESS.md first. Make no design decisions of your own.
2. Python 3.12; tests never reach OpenAI/network; suite green with no key.
3. Writes through change_log in a batch; LLM calls through llm.py with a purpose and the
   stable prompt first; anything that sends to a human goes through pending_actions.
4. No frontend changes. No deletions or renames. Existing tests must keep passing
   unchanged except where the spec says to update them.
5. Lazy `import main` inside functions.
6. Run `python -m pytest -q` and the startup one-liner from backend/ before committing.
7. One commit: `Phase 5B.2: qualification, outreach drafts, approval gate, follow-ups`.
8. If blocked, keep the suite green, commit nothing half-done, record it in PROGRESS.md.

Task — §5B "Qualification", "Outreach drafts", "The approval gate", "Channel adapters"
and the follow-up todo behaviour:
- leads.qualify(db, lead_ids) with the deterministic pre-score, one llm.chat_text call
  per lead (purpose lead_qualify, json_object, max_tokens 300, ICP appended after the
  stable text), clamp, status rules, cap 20, no-key behaviour; POST /leads/qualify
  ({ids} or {segment}).
- outreach.py: draft() (one call, purpose outreach_draft, max_tokens 400, email returns
  {subject, body} JSON, others plain text; revision via message_id creates version n+1
  and cancels the previous), request_send() (pending action kind send_outreach, stores
  the id of the latest user chat_messages row in payload_json as "after_message_id"),
  execute_send() (approved → channel → sent/failed; lead.last_contact_at and status
  contacted; follow-up todo +3 days, category outreach, source outreach, lead_id;
  communications row only if hasattr(models, "Communication")), cancel(); endpoints GET
  /outreach, GET /outreach/{id}, POST /outreach/{id}/send (409 unless draft/approved;
  returns open_url for whatsapp), POST /outreach/{id}/cancel. Include the router.
- outreach_channels.py exactly as specified (wa.me URL with digits only and URL-encoded
  text; EmailChannel returns ok False "email sending is not configured" until Phase 6C;
  ManualChannel ok True).
- actions.confirm_action refactored to _EXECUTORS = {"delete": ..., "send_outreach":
  outreach.execute_send}; for send_outreach, refuse with {"error": "confirmation must come
  in a later message"} unless a user chat_messages row newer than after_message_id exists.
  Keep the delete path byte-for-byte equivalent in behaviour (test_stage2_changes must pass).
- Tools qualify_leads, draft_outreach, request_send, set_follow_up as the §5B tool table
  (set_follow_up reuses syllabus.parse_date_text; moves an existing open follow-up todo
  rather than adding a second). Prompt: the "draft, never send; request_send then ask
  for a plain yes; never confirm_action in the same turn" bullets.
- Tests 3, 4, 5, 6 of §5B. Append S4 to PROGRESS.md.
```

---

### S5 — 5B.3 Replies, conversion, stats, briefing and alert

**Must exist first:** S4.

**Files touched:** `backend/leads.py` (`convert`, `POST /leads/{id}/replied`,
`POST /leads/{id}/convert`), `backend/outreach.py` (`GET /outreach/stats`, inbound rows),
`backend/orchestrator_tools.py` (`log_lead_reply`, `convert_lead`,
`_check_no_outreach_sent`), `context/core/off_track_rules.json` (`outreach_stalled`),
`context/core/orchestrator_prompt.md`, `backend/briefing.py` (`_outreach_items`,
`counts.drafts_waiting`), `backend/tests/test_phase5b_leads.py` (tests 7–10),
`docs/REBUILD_HANDOFF.md` §2, `docs/phases/PROGRESS.md`.

**Definition of done:** tests 7–10 of §5B pass; briefing items at 38 / 28; alert rule
fires and does not fire as specified; hand-off doc lists every `/leads` and `/outreach`
endpoint shape; commit `Phase 5B.3: replies, conversion, outreach stats, briefing`.

**Prompt:**
```
You are building Founder OS (FastAPI backend in backend/). Rules:
1. Read docs/CONVENTIONS.md, docs/phases/PHASE_5_SPEC.md §0 and §5B, and PROGRESS.md
   first. Make no design decisions of your own.
2. Python 3.12; tests never reach OpenAI/network; suite green with no key.
3. Writes through change_log in a batch; briefing items via _item() in the specified
   score bands; alert rules as JSON plus an evaluator in _RULE_CHECKS.
4. No frontend changes. No deletions or renames.
5. Lazy `import main` inside functions.
6. Run `python -m pytest -q` and the startup one-liner from backend/ before committing.
7. One commit: `Phase 5B.3: replies, conversion, outreach stats, briefing`.
8. If blocked, keep the suite green, commit nothing half-done, record it in PROGRESS.md.

Task — finish §5B:
- leads.convert (client row with lead_id, status converted, follow-ups closed, idempotent;
  primary contact only if hasattr(models, "Contact")); POST /leads/{id}/replied (inbound
  outreach_messages row direction in/status received, lead replied, follow-up done, notes
  appended with a dated line); POST /leads/{id}/convert.
- GET /outreach/stats per the spec (by_segment, by_channel, totals; reply_rate =
  replied/contacted, conversion_rate = converted/contacted, zero-safe).
- Tools log_lead_reply and convert_lead; prompt bullets for replies, follow-ups and
  explicit conversion.
- briefing._outreach_items (drafts waiting 38, no-reply > 7 days 28, max 3 each),
  counts.drafts_waiting; wire into build_briefing.
- Alert outreach_stalled in off_track_rules.json with evaluator _check_no_outreach_sent
  (≥ min_qualified leads with status qualified and no outreach_messages sent in
  window_days).
- Tests 7–10 of §5B; update docs/REBUILD_HANDOFF.md §2 with all 5B endpoint shapes;
  append S5 to PROGRESS.md.
```

---

### S6 — 5C.1 CRM schema, clients, contacts, deals, communications

**Must exist first:** S3 (for `todos.client_id`, `business_common`).

**Files touched:** `backend/models.py` (`Contact`, `Deal`, `Project`, `Payment`,
`RecurringRevenue`, `Communication`, `Client` columns), `backend/migrations/m016_crm.py`
(new, all six tables + four client columns + indexes incl. the COALESCE unique index +
Python backfills), `backend/main.py` (register m016; include `crm` router; `/clients`
endpoints extended), `backend/crm.py` (new: find_*, client_health, add_contact,
log_communication, create_deal, update_deal, endpoints for clients detail, contacts,
deals, communications), `backend/orchestrator_tools.py` (`crm_lookup`,
`log_communication`, `update_deal`, `add_contact`), `context/core/orchestrator_prompt.md`,
`backend/actions.py` (`DELETABLE`), `backend/retrieval.py` (`index_crm` helper),
`backend/tests/test_phase5c_crm.py` (new, tests 1, 6, 7), `docs/phases/PROGRESS.md`.

**Definition of done:** migration idempotent on repeated startup; backfilled payments
from revenue exactly once; `update_deal(stage=won)` creates a project once;
`log_communication` resolves clients fuzzily and bumps `last_activity_at`; tools
registered; suite green; commit `Phase 5C.1: CRM schema, contacts, deals, communications`.

**Prompt:**
```
You are building Founder OS (FastAPI backend in backend/). Rules:
1. Read docs/CONVENTIONS.md (especially §5 and §13), docs/phases/PHASE_5_SPEC.md §0 and
   §5C, and PROGRESS.md first. Make no design decisions of your own.
2. Python 3.12; tests never reach OpenAI/network; suite green with no key.
3. Writes through change_log in a batch; new tables via one migration registered before
   _validate_schema with RLS; Postgres-valid ADD COLUMN types; Python backfills.
4. No frontend changes. No deletions or renames. Existing /clients and /revenue endpoints
   keep their current response fields (add, never remove).
5. Lazy `import main` inside functions.
6. Run `python -m pytest -q` and the startup one-liner from backend/ before committing.
7. One commit: `Phase 5C.1: CRM schema, contacts, deals, communications`.
8. If blocked, keep the suite green, commit nothing half-done, record it in PROGRESS.md.

Task — §5C "Tables", the first half of "Primitives", and the clients/contacts/deals/
communications endpoints and tools:
- models.py and migrations/m016_crm.py exactly as §5C "Tables" (all six tables and all
  projects columns now, because 5E depends on them; the COALESCE unique index on
  payments; backfills: clients.currency 'USD' for existing rows, one paid payment per
  existing revenue row with client_id, idempotent by revenue_id).
- backend/crm.py: find_client, find_deal, find_project (business_common.fuzzy_pick),
  client_health, add_contact, log_communication (bumps last_activity_at on the client's
  active projects; retrieval.index_one source_type "crm" in try/except), create_deal,
  update_deal (default probability by stage; won → won_at and a linked project once).
  Endpoints: GET /clients extended fields, GET /clients/{id} detail, PATCH /clients/{id}
  (currency, tags, status), POST /clients/{id}/contacts, PATCH /contacts/{id}, GET/POST
  /deals, PATCH /deals/{id}, GET/POST /communications. Put the new endpoints in crm.py's
  router; extend the existing GET /clients in main.py in place.
- Tools crm_lookup, log_communication, update_deal (with create: true semantics),
  add_contact; prompt section "CLIENTS, DEALS AND PAYMENTS" (the record_payment bullet
  can be included now even though the tool arrives in S7).
- actions.DELETABLE gains contacts, deals, projects, payments, recurring_revenue,
  communications (and the propose_delete enum).
- Tests 1, 6, 7 of §5C in tests/test_phase5c_crm.py. Append S6 to PROGRESS.md.
```

---

### S7 — 5C.2 Payments, recurring revenue, reconciliation, CRM summary, briefing

**Must exist first:** S6.

**Files touched:** `backend/crm.py` (record_payment, mark_paid, add_recurring,
generate_due, reconcile, refresh_statuses, summary; payments/recurring endpoints;
`GET /crm/summary`), `backend/orchestrator_tools.py` (`record_payment`, `crm_summary`,
`_check_payment_overdue`), `context/core/off_track_rules.json` (`payment_overdue_14d`),
`backend/briefing.py` (`_crm_items`, `business_detail`, `counts.payments_overdue` in
attention), `backend/tests/test_phase5c_crm.py` (tests 2–5, 8–10),
`docs/REBUILD_HANDOFF.md` §2, `docs/phases/PROGRESS.md`.

**Definition of done:** a paid payment always has exactly one linked `revenue` row and
`GET /revenue total_usd` moves accordingly; recurring generation idempotent; reconcile
links within ±5 % same currency; briefing overdue payment ≥ 72 and in `attention_count`;
alert fires at 15 days; commit `Phase 5C.2: payments, recurring revenue, CRM summary,
briefing`.

**Prompt:**
```
You are building Founder OS (FastAPI backend in backend/). Rules:
1. Read docs/CONVENTIONS.md, docs/phases/PHASE_5_SPEC.md §0 and §5C, and PROGRESS.md
   first. Make no design decisions of your own.
2. Python 3.12; tests never reach OpenAI/network; suite green with no key.
3. Writes through change_log in a batch; money as amount + currency with
   main._to_usd / main._normalise_currency; briefing items via _item() in the specified
   bands; alert rules as JSON plus an evaluator.
4. No frontend changes. No deletions or renames. The parser (get_parse_response and
   apply_parse_updates) is not modified in this session.
5. Lazy `import main` inside functions.
6. Run `python -m pytest -q` and the startup one-liner from backend/ before committing.
7. One commit: `Phase 5C.2: payments, recurring revenue, CRM summary, briefing`.
8. If blocked, keep the suite green, commit nothing half-done, record it in PROGRESS.md.

Task — finish §5C:
- crm.py: record_payment, mark_paid (creates the revenue row: amount, currency, source =
  client.name, client_id, date = paid_at, notes "payment #id"; links revenue_id;
  idempotent), add_recurring, generate_due (idempotent via the unique index: catch
  IntegrityError per row and continue, or check existence first — do the existence check
  so SQLite and Postgres behave the same), reconcile (±5 %, same client and currency,
  oldest open due payment first), refresh_statuses, summary. Endpoints GET/POST
  /payments, POST /payments/{id}/paid, POST /payments/{id}/cancel, GET/POST/PATCH
  /recurring, GET /crm/summary (runs generate_due, refresh_statuses, reconcile first).
- Tools record_payment and crm_summary.
- briefing._crm_items (overdue 72 + min(days,14); due-within-7 42; stale negotiation 40;
  no-communication-21d 26, max 3), business_detail key, counts.payments_overdue added to
  attention_count; call generate_due and refresh_statuses at the start of _crm_items.
- Alert payment_overdue_14d with evaluator _check_payment_overdue.
- Tests 2, 3, 4, 5, 8, 9, 10 of §5C; update docs/REBUILD_HANDOFF.md §2 with all 5C
  endpoint shapes; append S7 to PROGRESS.md.
```

---

### S8 — 5E Project portfolio

**Must exist first:** S6 (`projects`, `communications`), S7 (payments for the bottleneck
check; if S7 is not merged, implement the payment bottleneck as feature-detected and note
it).

**Files touched:** `backend/models.py` (`ProjectUpdate`),
`backend/migrations/m018_portfolio.py` (new), `backend/main.py` (register m018; include
router; `project_updates` key in `_apply_parse_updates_inner`), `backend/portfolio.py`
(new: attention, recommendation, review, propose_pauses, endpoints), `backend/actions.py`
(`project_pause` suggestion kind; `DELETABLE` project_updates),
`backend/orchestrator_tools.py` (`portfolio_review`, `update_project`,
`log_project_update`, `_check_projects_blocked`), `context/core/off_track_rules.json`,
`context/core/orchestrator_prompt.md`, `backend/briefing.py` (`_portfolio_items`,
`progress.projects`), `backend/tests/test_phase5e_portfolio.py` (new, tests 1–7),
`docs/REBUILD_HANDOFF.md` §2, `docs/phases/PROGRESS.md`.

**Definition of done:** the five-project fixture yields the specified scores and
recommendations; pause suggestions are created once and accepted through the suggestions
endpoint; the model cannot pause without `explicit`; briefing items 62 / ≥ 70 / 45;
`projects_blocked` fires at 2; commit `Phase 5E: project portfolio — attention scoring,
bottlenecks, pause suggestions`.

**Prompt:**
```
You are building Founder OS (FastAPI backend in backend/). Rules:
1. Read docs/CONVENTIONS.md (especially §3 on suggestions), docs/phases/PHASE_5_SPEC.md
   §0 and §5E, and PROGRESS.md first. Make no design decisions of your own.
2. Python 3.12; tests never reach OpenAI/network; suite green with no key. This module
   makes no LLM calls at all.
3. Writes through change_log in a batch; the only parser change allowed is the new
   `project_updates` key in _apply_parse_updates_inner (no change to the LLM parser
   schema or prompt).
4. No frontend changes. No deletions or renames.
5. Lazy `import main` inside functions.
6. Run `python -m pytest -q` and the startup one-liner from backend/ before committing.
7. One commit: `Phase 5E: project portfolio — attention scoring, bottlenecks, pause
   suggestions`.
8. If blocked, keep the suite green, commit nothing half-done, record it in PROGRESS.md.

Task — implement §5E in full:
- models.ProjectUpdate; migrations/m018_portfolio.py (table, index, RLS); register.
- backend/portfolio.py: attention() with the exact components and bottleneck order,
  recommendation() with the exact rules, review(), propose_pauses() (one open
  project_pause suggestion per project), health stored on review; endpoints GET
  /portfolio, GET/POST /projects, GET /projects/{id}, PATCH /projects/{id} (empty
  blocked_reason clears + 'unblocked' update), POST /projects/{id}/status, POST
  /projects/{id}/focus (demote other priority-1 projects, one batch), POST
  /projects/{id}/updates. Include the router.
- actions.accept_suggestion learns kind project_pause → parsed["project_updates"];
  _apply_parse_updates_inner applies project_updates (status change logged + a
  project_updates row kind status).
- Tools portfolio_review, update_project (status paused refused without explicit: true),
  log_project_update (blocker/unblocked semantics); prompt section "PROJECTS AND
  PORTFOLIO".
- briefing._portfolio_items (62 blocked, 70 + min(days, 9) overdue, 36 waiting, 45 focus
  rank 1), progress.projects; alert projects_blocked with _check_projects_blocked.
- Tests 1–7 of §5E (build the five-project fixture in the test; compute expected scores
  with the formula written in the test, not by calling attention()). Update
  REBUILD_HANDOFF §2; append S8 to PROGRESS.md.
```

---

### S9 — 5D.1 Brand schema, YouTube adapter, export importer, sync

**Must exist first:** S3 (shared helpers). Independent of 5B/5C otherwise.

**Files touched:** `backend/models.py` (six brand models),
`backend/migrations/m017_brand.py` (new), `backend/main.py` (register m017; include
`brand` router), `backend/brand/__init__.py` (router; accounts, sync, import endpoints),
`backend/brand/adapters.py` (PlatformAdapter, YouTubeAdapter, ExportAdapter,
LinkedInExportAdapter, optional XExportAdapter), `backend/brand/sync.py` (sync,
import_export), `backend/tests/fixtures/linkedin_content_sample.csv` (new),
`backend/tests/test_phase5d_brand.py` (new, tests 1–4), `CLAUDE.md` (env var
`YOUTUBE_API_KEY`), `docs/phases/PROGRESS.md`.

**Definition of done:** canned YouTube JSON (two pages) imports with shorts flagged;
repeated sync writes no new metrics unless a number changed; missing key → account
`error`; LinkedIn fixture imports and re-imports without duplicates; nothing in the module
produces an estimated number; commit `Phase 5D.1: brand analytics schema, YouTube adapter,
export importer`.

**Prompt:**
```
You are building Founder OS (FastAPI backend in backend/). Rules:
1. Read docs/CONVENTIONS.md, docs/phases/PHASE_5_SPEC.md §0 and §5D, and
   docs/adr/0004-social-data-access.md, and PROGRESS.md first. Make no design decisions
   of your own.
2. Python 3.12; tests never reach the network: patch `brand.adapters.httpx.get`. Suite
   green with no key and no YOUTUBE_API_KEY.
3. Real platform data only: an adapter returns None/[] when it has nothing; the code
   never fills a metric with a default number. Writes through change_log only for new
   posts (metric snapshots are append-only data and are not logged).
4. No frontend changes. No deletions or renames. No new dependencies (httpx and the
   stdlib csv module only).
5. Lazy `import main` inside functions.
6. Run `python -m pytest -q` and the startup one-liner from backend/ before committing.
7. One commit: `Phase 5D.1: brand analytics schema, YouTube adapter, export importer`.
8. If blocked, keep the suite green, commit nothing half-done, record it in PROGRESS.md.

Task — §5D "Tables", "Adapters" and the account/sync/import endpoints:
- models.py: SocialAccount, ContentPost, ContentMetric, AccountMetric, ContentIdea,
  BrandInsight; migrations/m017_brand.py with the unique and plain indexes and RLS.
- brand/adapters.py exactly as specified: YouTubeAdapter (channels → uploads playlist →
  playlistItems paginated to 200 → videos in batches of 50; short = duration ≤ 60 s;
  handle resolution via channels?forHandle with search fallback; YOUTUBE_API_KEY from
  env; never raises), ExportAdapter base reading CSV text with header synonyms,
  LinkedInExportAdapter with the listed columns. Mark the places that depend on the real
  LinkedIn export format with a `# VERIFY` comment.
- brand/sync.py: sync(db, account) (upsert posts by (account_id, external_id); one
  content_metrics row only when any metric changed since the last snapshot; one
  account_metrics row per sync if changed; last_synced_at; error → status error +
  last_error) and import_export(db, platform, text, filename).
- brand/__init__.py router: GET /brand/accounts, POST /brand/accounts (YouTube resolves
  handle → id; 400 with the adapter message on failure), POST /brand/accounts/{id}/sync,
  DELETE /brand/accounts/{id} (sets status disconnected; keeps data), POST /brand/import
  (multipart file + platform). Include the router.
- Fixture tests/fixtures/linkedin_content_sample.csv (6 synthetic rows, header
  synonyms). Tests 1–4 of §5D. Add YOUTUBE_API_KEY to the env table in CLAUDE.md.
  Append S9 to PROGRESS.md.
```

---

### S10 — 5D.2 Performance analytics, weekly insight, ideas and calendar, tools, briefing

**Must exist first:** S9.

**Files touched:** `backend/brand/analytics.py` (new), `backend/brand/__init__.py`
(performance, posts, ideas, calendar, insight endpoints), `backend/orchestrator_tools.py`
(`brand_performance`, `add_content_idea`, `mark_posted`),
`context/core/orchestrator_prompt.md`, `backend/briefing.py` (content items,
`progress.brand`, `brand_target_posts_30d`), `backend/actions.py` (`DELETABLE`
content_ideas), `backend/tests/test_phase5d_brand.py` (tests 5–9),
`docs/REBUILD_HANDOFF.md` §2, `docs/phases/PROGRESS.md`.

**Definition of done:** `performance()` returns `None` for absent data; insight cached
per platform-week; ideas advance and schedule; briefing item 58 for today's scheduled
idea; `progress.brand` null with no account; test 9 asserts no estimated numbers;
commit `Phase 5D.2: brand performance, insight, ideas, content calendar`.

**Prompt:**
```
You are building Founder OS (FastAPI backend in backend/). Rules:
1. Read docs/CONVENTIONS.md, docs/phases/PHASE_5_SPEC.md §0 and §5D, and PROGRESS.md
   first. Make no design decisions of your own.
2. Python 3.12; tests never reach OpenAI/network; suite green with no key.
3. Real data only: None where there is no measurement, never 0 or an estimate. One LLM
   call per platform per ISO week for the insight (purpose brand_insight, max 250
   tokens), cached in brand_insights, regenerated only with refresh=1.
4. No frontend changes. No deletions or renames.
5. Lazy `import main` inside functions.
6. Run `python -m pytest -q` and the startup one-liner from backend/ before committing.
7. One commit: `Phase 5D.2: brand performance, insight, ideas, content calendar`.
8. If blocked, keep the suite green, commit nothing half-done, record it in PROGRESS.md.

Task — finish §5D:
- brand/analytics.py performance() exactly as the spec (latest snapshot per post;
  engagement and engagement_rate definitions; top 5; by_format medians; cadence; 12-week
  trend) and brand.insight() with the cache.
- Endpoints GET /brand/performance (+ insight), GET /brand/posts, PATCH /brand/posts/{id},
  GET/POST/PATCH /brand/ideas, POST /brand/ideas/{id}/advance (409 when scheduling
  without scheduled_for), GET /brand/calendar, GET /brand/insight.
- Tools brand_performance, add_content_idea (scheduled_for parsed with
  syllabus.parse_date_text; idea_id updates), mark_posted (link by URL). Prompt section
  "PERSONAL BRAND" with the never-estimate rule.
- Briefing: content items (today 58, overdue 66, account error 20, cadence gap 22),
  progress.brand and brand_target_posts_30d.
- actions.DELETABLE gains content_ideas (title).
- Tests 5–9 of §5D; REBUILD_HANDOFF §2 for all /brand endpoints; append S10 to
  PROGRESS.md.
```

---

### S11 — Phase 5 UI, part 1: Business page with Pipeline and Clients sub-tabs

**Must exist first:** S5, S7.

**Files touched:** `frontend/index.html` only (nav item, `p-business` page, sub-tab
switcher, `renderBusiness()`, `renderPipeline()`, `renderClients()`, one-tap handlers, CSS
for status chips and draft cards within the existing token set), `docs/phases/PROGRESS.md`.

**Definition of done:** Business appears in the nav; Pipeline lists leads grouped by
status with segment filter chips, drafts awaiting approval as cards with **Approve &
send** (opens `open_url` in a new tab when present), **Copy**, **Dismiss**; leads rows
have **Replied** and **Convert**; Clients lists clients with health chip, outstanding and
MRR, deals by stage with tap-to-advance stage chips, payments due with **Paid**; every
mutation renders `changes` through `renderChangesStrip`; all text through `escHtml`; works
at 390 px; checked in headless Chromium with a screenshot of each sub-tab; commit
`Phase 5 UI.1: Business page — pipeline and clients`.

**Prompt:**
```
You are building Founder OS. This session touches only frontend/index.html (single
file, vanilla JS, no build step). Rules:
1. Read docs/CONVENTIONS.md §12, docs/phases/PHASE_5_SPEC.md §0.7, §5B and §5C
   ("Endpoints"), docs/REBUILD_HANDOFF.md §2 and PROGRESS.md first. Read index.html by
   line range (it is ~5,600 lines): the nav (search `nav-item`), renderPage(), the
   syllabus page (`p-syllabus`, renderSyllabus) as the template for a list page with
   one-tap actions, and renderChangesStrip / showToast / escHtml / mdHtml.
2. Screens are for viewing and one-tap corrections. No forms. No input except the
   existing dock and a segment filter (chips).
3. Every user or server string goes through escHtml before innerHTML. Every mutation is
   one fetch to the endpoint named in the spec, then renderChangesStrip(changes) when
   the response has changes, then a re-render.
4. Use the existing tokens and classes (.card, .card-title, .badge b-glow|b-muted|b-suc|
   b-danger, .ar-btn-sm, .sr-btn, .btn-danger). Cyan is for signal only. Respect
   prefers-reduced-motion. Must work at 390 px wide with no horizontal scroll.
5. Do not change any backend file. Do not remove or rename existing functions.
6. Verify with headless Chromium (Playwright is pre-installed: `require('playwright')`)
   against `uvicorn main:app` with a seeded local SQLite: take a 390-px and a 1280-px
   screenshot of each sub-tab and look at them. Note what you saw in PROGRESS.md.
7. One commit: `Phase 5 UI.1: Business page — pipeline and clients`.
8. If blocked, commit nothing half-done and record it in PROGRESS.md.

Task — add the Business page (nav item between Agents and War Room) with a sub-tab
switcher (Pipeline, Clients, Portfolio, Brand — the last two render "Coming in the next
session" placeholders now) and implement:
- Pipeline: GET /leads/segments → filter chips; GET /leads?segment= → rows grouped by
  status (new, qualified, contacted, replied, converted, disqualified/lost collapsed)
  showing name · company · segment · score chip · last message chip · open follow-up date;
  row actions: Replied (POST /leads/{id}/replied), Convert (POST /leads/{id}/convert).
  Above the list: "Drafts waiting" cards from GET /outreach?status=draft — lead name,
  channel chip, subject/body (escHtml, preserve line breaks), buttons Approve & send
  (POST /outreach/{id}/send; if the response has open_url, window.open it in a new tab),
  Copy (navigator.clipboard.writeText(body)), Dismiss (POST /outreach/{id}/cancel).
  A small funnel line from GET /outreach/stats (contacted → replied → converted).
- Clients: GET /clients → rows with health dot, name · company, outstanding and MRR
  (by currency, from the extended fields); tap a row → detail panel from
  GET /clients/{id}: contacts, deals with stage chips where tapping the next stage calls
  PATCH /deals/{id} {stage}, payments with Paid (POST /payments/{id}/paid) on due/overdue
  rows, last 10 communications. Header strip from GET /crm/summary (MRR, outstanding,
  overdue count, weighted pipeline).
- renderPage("business") wiring; remember the active sub-tab in localStorage.
- Append S11 to PROGRESS.md with the screenshot observations.
```

---

### S12 — Phase 5 UI, part 2: Portfolio and Brand sub-tabs, Agents page additions, docs

**Must exist first:** S8, S10, S11, S2.

**Files touched:** `frontend/index.html`, `docs/REBUILD_HANDOFF.md` (Phase 5 section),
`CLAUDE.md` (file table, env vars, key concepts), `ROADMAP.md` (Phase 5 → "core built"
with the remaining list), `docs/phases/PROGRESS.md`.

**Definition of done:** Portfolio shows the ranked review with components tooltip,
bottleneck line, recommendation chip and taps **Focus / Pause / Resume / Delivered**;
pause suggestions appear in the existing suggestions strip; Brand shows account tiles
with Sync, top posts, 14-day calendar with **Advance** on ideas, Import (file + platform
select, the one allowed import control), and "Not connected" states; Agents page gains
**Dismiss** on surfaced findings and a Standing objectives card with **Run**; docs
updated; screenshots checked; commit `Phase 5 UI.2: portfolio and brand sub-tabs, agents
page additions, docs`.

**Prompt:**
```
You are building Founder OS. This session touches frontend/index.html and three docs.
Rules:
1. Read docs/CONVENTIONS.md §12, docs/phases/PHASE_5_SPEC.md §0.7, §5A, §5D, §5E
   ("Endpoints" and "Friction"), docs/REBUILD_HANDOFF.md §2 and PROGRESS.md first. Read
   index.html by line range; start from the Business page built in S11 and the Agents
   page (renderAgents, renderAgentDetail).
2. Screens are for viewing and one-tap corrections. The only input controls allowed are
   a file picker + platform select for Brand import and a date picker when scheduling an
   idea.
3. escHtml on every string; one fetch per tap; renderChangesStrip(changes) after each
   mutation; existing tokens and classes; works at 390 px.
4. Do not change any backend file. Do not remove or rename existing functions.
5. Verify with headless Chromium (Playwright pre-installed) at 390 px and 1280 px for
   Portfolio, Brand (connected and not-connected states) and the Agents page; record
   observations in PROGRESS.md.
6. Docs: update docs/REBUILD_HANDOFF.md with a "Phase 5" section summarising what shipped
   per module and listing every endpoint added (point to the spec for shapes), update
   CLAUDE.md's file table, env vars and key concepts, and set ROADMAP.md Phase 5 to
   "core built" with a Remaining list taken from PROGRESS.md.
7. One commit: `Phase 5 UI.2: portfolio and brand sub-tabs, agents page additions, docs`.
8. If blocked, commit nothing half-done and record it in PROGRESS.md.

Task:
- Portfolio sub-tab: GET /portfolio → ranked list (rank, title, client, recommendation
  chip focus/maintain/pause/close, score with a title attribute listing the components,
  bottleneck line, due label), paused list collapsed; taps: Focus (POST
  /projects/{id}/focus), Pause/Resume/Delivered (POST /projects/{id}/status). Totals
  strip from review().totals.
- Brand sub-tab: GET /brand/accounts → tiles (platform, handle, followers with 30-day
  delta, last sync, Sync button → POST /brand/accounts/{id}/sync; error state shows
  last_error); "Connect YouTube" tile with a single handle field when no YouTube account
  exists (POST /brand/accounts); Import button → file picker + platform select → POST
  /brand/import; GET /brand/performance → top posts list and by_format row (None renders
  as "—", never 0); GET /brand/calendar → 14-day strip with ideas, Advance (POST
  /brand/ideas/{id}/advance; when 409, open a date picker then PATCH scheduled_for and
  retry); insight text under the header when present.
- Agents page: Dismiss button on surfaced findings (POST /research/findings/{id}/dismiss)
  next to Promote; a "Standing objectives" card from GET /research/objectives with a Run
  button (POST /research/objectives/{id}/run, show a running state) and an Enabled
  toggle (PATCH).
- Docs as rule 6. Append S12 to PROGRESS.md.
```

---

## Phase 6

### S13 — 6B.1 Agent runner, registry, schedule, events, cost attribution, jobs tick

**Must exist first:** S1 (PROGRESS.md). Phase 5 modules are optional for this session
(the shipped agents arrive in S14).

**Files touched:** `backend/agents/__init__.py`, `backend/agents/base.py`,
`backend/agents/runner.py`, `backend/agents/schedule.py` (all new), `backend/models.py`
(`Agent`, `AgentRun`, `AgentEvent`, `JobTick`, `LLMUsage.agent_run_id`),
`backend/migrations/m020_agents.py` (new), `backend/llm.py` (run context,
`BudgetExceeded`, `log_usage(agent_run_id)`), `backend/auth.py` (`/jobs/tick` exempt),
`backend/main.py` (register m020; `POST /jobs/tick`, `GET /jobs/status`; env `JOBS_TOKEN`
warning), `backend/tests/test_phase6b_agents.py` (new, tests 1–8, 11), `CLAUDE.md` (env
vars), `docs/phases/PROGRESS.md`.

**Definition of done:** a test agent runs through `tick`; violations are undone; schedule
semantics exactly as §6B; events consumed/dropped; LLM calls attributed and capped; tick
budget respected; `/jobs/tick` 503/401/200 behaviour; commit `Phase 6B.1: agent runner,
registry, schedules, events, jobs tick`.

**Prompt:**
```
You are building Founder OS (FastAPI backend in backend/). Rules:
1. Read docs/CONVENTIONS.md (especially §2 and §6 on auth exemptions),
   docs/phases/PHASE_6_SPEC.md §0 and §6B, docs/adr/0001 and docs/adr/0003, and
   PROGRESS.md first. Make no design decisions of your own.
2. Python 3.12; tests never reach OpenAI/network; suite green with no key. Use zoneinfo
   for FOUNDER_OS_TZ (default Africa/Lusaka).
3. Every agent run opens its own SessionLocal(), a change_log batch with source
   `agent:<key>`, and the llm run context; the permission audit undoes the whole batch on
   a write outside tables_write. The runner and tick never raise.
4. No frontend changes. No deletions or renames. No new dependencies.
5. Lazy `import main` inside functions.
6. Run `python -m pytest -q` and the startup one-liner from backend/ before committing.
7. One commit: `Phase 6B.1: agent runner, registry, schedules, events, jobs tick`.
8. If blocked, keep the suite green, commit nothing half-done, record it in PROGRESS.md.

Task — §6B "Architecture", "Tables", the tick endpoint, and llm.py changes:
- agents/base.py dataclasses exactly as the spec (Schedule, Permissions, AgentSpec,
  AgentContext with run_tool allow-list / notify stub that returns {"error": "6D not
  built"} / emit / log, AgentResult). agents/schedule.py due(). agents/runner.py
  run_agent() steps 1–6 and tick() steps 1–5 with TICK_BUDGET_SECONDS from
  JOBS_TICK_BUDGET (default 80). agents/__init__.py REGISTRY (empty except in tests for
  now), emit(), register().
- models + migrations/m020_agents.py: agents, agent_runs, agent_events, job_ticks,
  llm_usage.agent_run_id (INTEGER), indexes, RLS; register in startup.
- llm.py: set_run_context(run_id, max_calls), clear_run_context(), current_run_id(),
  BudgetExceeded; log_usage(..., agent_run_id=None) resolving from the argument, then
  db.info["agent_run_id"], then the run context; chat_completion/chat_text/embed_texts
  count calls against the context and raise BudgetExceeded at the cap.
- main.py: POST /jobs/tick (header X-Jobs-Token compared with hmac.compare_digest to
  JOBS_TOKEN; 503 when unset; 401 when wrong) and GET /jobs/status; add /jobs/tick to
  auth.EXEMPT_EXACT; startup warning when JOBS_TOKEN is shorter than 32 chars.
- Tests 1–8 and 11 of §6B using fake agents registered inside the test module (patch
  agents.REGISTRY). Add the new env vars to CLAUDE.md. Append S13 to PROGRESS.md.
```

---

### S14 — 6B.2 Shipped agents, agents API and tools, in-process tick, GitHub workflow

**Must exist first:** S13; S2, S5, S7, S8, S10 for the corresponding agents (any missing
module → the agent is registered but returns `skipped`).

**Files touched:** `backend/agents/{crm_housekeeper,lead_qualifier,follow_up_sentinel,
portfolio_reviewer,brand_sync,market_intel}.py` (new), `backend/agents/api.py` (new
router), `backend/main.py` (include router; APScheduler job), `backend/orchestrator_tools.py`
(`agent_status`, `run_agent`, `set_agent_schedule`), `context/core/orchestrator_prompt.md`,
`.github/workflows/jobs-tick.yml` (new), `backend/tests/test_phase6b_agents.py` (tests
9–10 + endpoint tests), `docs/REBUILD_HANDOFF.md` §2, `docs/phases/PROGRESS.md`.

**Definition of done:** six agents registered with the default schedules and permissions
from the §6B table; `crm_housekeeper` emits `payment.overdue` once per payment per day;
`market_intel` runs one due objective per tick; `GET /agents` lists them with effective
schedules; `PATCH` toggles are logged; workflow file present with retries; in-process tick
guarded by `JOBS_IN_PROCESS`; commit `Phase 6B.2: shipped agents, agents API, scheduled
tick`.

**Prompt:**
```
You are building Founder OS (FastAPI backend in backend/). Rules:
1. Read docs/CONVENTIONS.md, docs/phases/PHASE_6_SPEC.md §0 and §6B, docs/adr/0001 and
   0003, docs/phases/PHASE_5_SPEC.md (the "Primitives" of §5B, §5C, §5D, §5E and the
   objectives of §5A — the agents only call those primitives), and PROGRESS.md first.
   Make no design decisions of your own.
2. Python 3.12; tests never reach OpenAI/network; suite green with no key.
3. Agents call module primitives, never endpoints; they write only to the tables in
   their Permissions; deterministic agents have max_llm_calls = 0; every agent bounds
   its own work (≤ 20 leads, one objective per tick).
4. No frontend changes. No deletions or renames.
5. Lazy imports of Phase 5 modules inside run(); if a module is missing, return
   AgentResult(status="skipped", summary="<module> not built").
6. Run `python -m pytest -q` and the startup one-liner from backend/ before committing.
7. One commit: `Phase 6B.2: shipped agents, agents API, scheduled tick`.
8. If blocked, keep the suite green, commit nothing half-done, record it in PROGRESS.md.

Task:
- The six agent modules in the §6B "Agents shipped" table with the stated schedules,
  events, tables_write, LLM caps: crm_housekeeper (generate_due, refresh_statuses,
  reconcile, client_health; emit payment.overdue once per payment per day — dedupe by
  checking agent_events in the last 24 h with the same payment id), lead_qualifier
  (≤ 20 'new' leads with a contact channel), follow_up_sentinel (contacted > 7 d no reply
  → ensure one open follow-up todo; on lead.replied events do nothing but log),
  portfolio_reviewer (portfolio.review + propose_pauses), brand_sync (every connected
  account; emit brand.sync_failed on error), market_intel (one due objective per tick,
  others reported as deferred).
- agents/api.py router: GET /agents, GET /agents/{key}, PATCH /agents/{key} (logged
  change on the agents row), POST /agents/{key}/run, GET /agents/{key}/runs, GET
  /agents/runs, POST /agents/runs/{id}/undo. Include the router.
- Tools agent_status, run_agent, set_agent_schedule; prompt section "AGENTS".
- main.start_vault_scheduler: add the jobs_tick interval job (15 min) guarded by
  JOBS_IN_PROCESS (default "1").
- .github/workflows/jobs-tick.yml exactly as the spec (cron */15, workflow_dispatch,
  three attempts with sleep 20, -m 120).
- Tests 9 and 10 of §6B plus endpoint tests (list, patch+undo, run now). Update
  REBUILD_HANDOFF §2; append S14 to PROGRESS.md including the GitHub secrets the user
  must set (FOUNDER_OS_URL, JOBS_TOKEN) and the Render env vars.
```

---

### S15 — 6C.1 Integrations table, redaction, Telegram pairing and webhook registration

**Must exist first:** S13 (events, for later), S3 (business helpers not required).

**Files touched:** `backend/models.py` (`Integration`, `CalendarEvent`, `EmailMessage`,
`OutreachMessage.replied_at`), `backend/migrations/m021_integrations.py` (new, all three
tables + the column), `backend/integrations.py` (new: router, `redact`, `GET /integrations`,
provider registry of actions), `backend/integrations_telegram.py` (new: pair_code,
set-webhook, unpair), `backend/main.py` (register m021; include router),
`backend/tests/test_phase6c_integrations.py` (new, tests 7–8 and pairing-code tests),
`CLAUDE.md` (env vars), `docs/phases/PROGRESS.md`.

**Definition of done:** `GET /integrations` lists four providers with `configured` and
`actions`, never a secret; `redact` scrubs every configured secret and the `bot<token>`
pattern; `POST /integrations/telegram/set-webhook` calls the Bot API (patched) with the
secret token; pairing code is stable and derived from `FOUNDER_OS_SECRET`; commit `Phase
6C.1: integrations table, redaction, Telegram pairing`.

**Prompt:**
```
You are building Founder OS (FastAPI backend in backend/). Rules:
1. Read docs/CONVENTIONS.md, docs/phases/PHASE_6_SPEC.md §0 and §6C (and the pairing
   parts of §6D), docs/adr/0002, and PROGRESS.md first. Make no design decisions.
2. Python 3.12; tests never reach the network (patch httpx in the module under test);
   suite green with no key and no tokens set.
3. secret_json is never serialised by any endpoint; every log line and last_error passes
   through integrations.redact(). New tables via one migration with RLS.
4. No frontend changes. No deletions or renames. No new dependencies.
5. Lazy `import main` inside functions.
6. Run `python -m pytest -q` and the startup one-liner from backend/ before committing.
7. One commit: `Phase 6C.1: integrations table, redaction, Telegram pairing`.
8. If blocked, keep the suite green, commit nothing half-done, record it in PROGRESS.md.

Task:
- models + migrations/m021_integrations.py: integrations, calendar_events,
  email_messages (all columns from §6C "Tables"), outreach_messages.replied_at
  (TIMESTAMP), indexes, RLS; register.
- backend/integrations.py: get_or_create(provider), redact(text) (values from every
  row's secret_json plus TELEGRAM_BOT_TOKEN, EMAIL_APP_PASSWORD, GOOGLE_CLIENT_SECRET,
  JOBS_TOKEN, YOUTUBE_API_KEY, and the regex `bot[0-9]+:[A-Za-z0-9_-]+`), serialize()
  without secret_json, GET /integrations with `configured` (env presence per provider)
  and `actions` per provider status. Include the router.
- backend/integrations_telegram.py: pair_code() (HMAC-SHA256 of "telegram-pair" with
  auth._secret(), first 6 chars of base32, uppercase), is_paired(), chat_id(), POST
  /integrations/telegram/set-webhook (Bot API setWebhook with url, secret_token,
  allowed_updates=["message"]; returns {ok, description}; 503 when TELEGRAM_BOT_TOKEN or
  FOUNDER_OS_API_URL unset), POST /integrations/telegram/unpair. The webhook receiver
  itself is built in S17.
- Tests: redaction of password/token in last_error and logs (test 7), pairing code is
  deterministic for a given secret and differs for another, GET /integrations never
  contains a secret string, set-webhook posts the secret token (patched httpx). Add env
  vars to CLAUDE.md; append S15 to PROGRESS.md.
```

---

### S16 — 6D.1 Notifications: queue, dispatch, settings, Telegram channel, couriers

**Must exist first:** S13 (agents), S15 (Telegram pairing row), S7 (payment events are
optional).

**Files touched:** `backend/models.py` (`Notification`, `NotificationSettings`),
`backend/migrations/m022_notifications.py` (new; creates the settings row),
`backend/notify.py` (new: settings, is_quiet, queue, dispatch, render_briefing,
send_test, TelegramChannel, router), `backend/agents/{briefing_courier,alert_courier,
reminder_courier}.py` (new), `backend/orchestrator_tools.py` (`set_reminder`,
`notification_settings`), `backend/syllabus.py` (`parse_date_text` gains time-of-day and
relative forms, or a new `parse_when` in `business_common`), `context/core/orchestrator_prompt.md`,
`backend/briefing.py` (`delivery` key), `backend/main.py` (register m022; include router),
`backend/tests/test_phase6d_notify.py` (new, tests 1–3, 6–8, 10–11), `CLAUDE.md`,
`docs/phases/PROGRESS.md`.

**Definition of done:** dedupe, quiet hours, daily cap, not-paired suppression, message
splitting, briefing rendering, couriers' once-per-day behaviour all tested with
`httpx.post` patched; `AgentContext.notify` now works; commit `Phase 6D.1: notifications
queue, Telegram channel, briefing and alert couriers`.

**Prompt:**
```
You are building Founder OS (FastAPI backend in backend/). Rules:
1. Read docs/CONVENTIONS.md, docs/phases/PHASE_6_SPEC.md §0, §6B (AgentContext.notify)
   and §6D, docs/adr/0002, and PROGRESS.md first. Make no design decisions of your own.
2. Python 3.12; tests never reach the network: patch `notify.httpx.post`. Suite green
   with no key and no TELEGRAM_BOT_TOKEN.
3. Nothing sends directly: queue() then dispatch(); dispatch enforces pairing, channel
   enabled, quiet hours (replies and force bypass), daily cap (briefing and reply
   bypass), one retry on the next dispatch, failed after 3 attempts. Messages > 4,000
   chars split on blank lines.
4. No frontend changes. No deletions or renames. No new dependencies.
5. Lazy `import main` inside functions.
6. Run `python -m pytest -q` and the startup one-liner from backend/ before committing.
7. One commit: `Phase 6D.1: notifications queue, Telegram channel, briefing and alert
   couriers`.
8. If blocked, keep the suite green, commit nothing half-done, record it in PROGRESS.md.

Task — §6D except the inbound webhook (S17):
- models + migrations/m022_notifications.py (notifications with unique dedupe_key,
  notification_settings row id=1 inserted by the migration with the spec defaults, plus
  a muted_until TIMESTAMP column), RLS; register.
- backend/notify.py exactly as "Core": settings(), is_quiet() (ranges across midnight;
  muted_until), sent_today(), queue(), dispatch(), render_briefing() with the documented
  layout and limits (≤ 6 attention items, ≤ 5 today items, ≤ 3,500 chars, empty sections
  omitted, OVERNIGHT from suppressed rows of the last 24 h), send_test(),
  TelegramChannel.send() (sendMessage, disable_web_page_preview, 10 s timeout, never
  raises). Router: GET/PATCH /notifications/settings (paired, pair_code, bot_link,
  channel_configured), GET /notifications, POST /notifications/test, POST
  /notifications/brief-now, POST /notifications/{id}/cancel.
- agents: briefing_courier (at settings.morning_time; dedupe briefing:{date}; evening
  message when evening_time set), alert_courier (every 15 min + events per the spec;
  dedupe keys as listed; severity threshold), reminder_courier (dispatch only). Wire
  AgentContext.notify to notify.queue (refused unless may_notify).
- Tools set_reminder and notification_settings; a parse_when(text, now_local) helper
  (date text via syllabus.parse_date_text plus "HH:MM", "at 5", "in 2 hours", "tomorrow
  17:00"; returns a UTC datetime) — put it in business_common.py. Prompt section
  "REMINDERS AND NOTIFICATIONS".
- briefing.build_briefing gains delivery: {last_sent_at, channel, paired}.
- Tests 1, 2, 3, 6, 7, 8, 10, 11 of §6D. Env vars to CLAUDE.md; append S16 to PROGRESS.md.
```

---

### S17 — 6D.2 Two-way Telegram: `run_turn` extraction and the inbound webhook

**Must exist first:** S16.

**Files touched:** `backend/orchestrator.py` (`run_turn` extracted; `orchestrator_stream`
re-implemented on top of it or sharing helpers — behaviour and SSE events unchanged),
`backend/telegram_inbound.py` (new: webhook, pairing, commands, turn), `backend/auth.py`
(`/integrations/telegram/webhook` exempt), `backend/main.py` (include router),
`backend/tests/test_phase6d_notify.py` (tests 4, 5, 9), `backend/tests/test_stage1_assistant.py`
and `test_stage2_changes.py` (must still pass unchanged), `docs/REBUILD_HANDOFF.md` §2,
`docs/phases/PROGRESS.md`.

**Definition of done:** all existing orchestrator tests pass unchanged; webhook verifies
the secret header; pairing with the right code only; `/brief`, `/undo`, `/yes`, `/no`,
`/mute` behave as specified; a plain message runs a full orchestrator turn and replies
with changes; commit `Phase 6D.2: two-way Telegram into the orchestrator`.

**Prompt:**
```
You are building Founder OS (FastAPI backend in backend/). Rules:
1. Read docs/CONVENTIONS.md §8, docs/phases/PHASE_6_SPEC.md §6D "Two-way", docs/adr/0002,
   backend/orchestrator.py, backend/openai_client.get_orchestrator_response, and
   PROGRESS.md first. Make no design decisions of your own.
2. Python 3.12; tests never reach the network or OpenAI: the orchestrator is driven by
   the scripted _stream_completion patch used in tests/test_stage2_changes.py; Telegram
   sends are patched at notify.httpx.post.
3. orchestrator_stream's observable behaviour (SSE event sequence, persistence of every
   message, change batch, final event with changes) must not change; every existing test
   passes unmodified.
4. No frontend changes. No deletions or renames.
5. Lazy `import main` inside functions.
6. Run `python -m pytest -q` and the startup one-liner from backend/ before committing.
7. One commit: `Phase 6D.2: two-way Telegram into the orchestrator`.
8. If blocked, keep the suite green, commit nothing half-done, record it in PROGRESS.md.

Task:
- orchestrator.run_turn(text, db, *, source="dock", session_id=None, context=None) ->
  {"final", "changes", "pending": [pending actions created this turn], "suggestions":
  [suggestions created this turn], "tool_calls": [names]}: same history building,
  memory recall, change batch (source stays "assistant"; store `source` in the saved
  assistant row's session_id prefix, e.g. "telegram:<chat_id>"), same persistence via the
  on_message callback, consuming get_orchestrator_response to completion. Keep
  orchestrator_stream as the SSE generator; share the message-building and persistence
  helpers so there is one implementation of each.
- backend/telegram_inbound.py: POST /integrations/telegram/webhook (auth-exempt; 403
  unless header X-Telegram-Bot-Api-Secret-Token equals TELEGRAM_WEBHOOK_SECRET; always
  200 {} otherwise). Flow exactly as §6D "Two-way" steps 1–5: pairing by code; ignore
  other chats; /brief, /undo (newest change_log batch whose assistant row session_id
  starts with "telegram:" within 24 h), /yes and /no (newest pending action created in a
  telegram turn within 24 h), /mute <n>h (settings.muted_until); otherwise run_turn and
  reply via notify.queue(kind="reply") + dispatch(force=True) with the documented reply
  format. Non-text messages → "Text only for now". Dedupe by update_id with a small
  in-memory LRU of the last 200 ids (document in PROGRESS.md that a table replaces it if
  Telegram retries prove common).
- Tests 4, 5, 9 of §6D. REBUILD_HANDOFF §2; append S17 to PROGRESS.md.
```

---

### S18 — 6C.2 Google Calendar: OAuth, sync, create event with undo, tools, briefing

**Must exist first:** S15, S13 (agent registration), S16 not required.

**Files touched:** `backend/integrations_google.py` (new), `backend/integrations.py`
(google actions), `backend/auth.py` (callback exempt), `backend/change_log.py`
(`_after_undo_hook` for `calendar_events`), `backend/agents/calendar_sync.py` (new),
`backend/orchestrator_tools.py` (`calendar_today`, `calendar_add_event`),
`context/core/orchestrator_prompt.md`, `backend/briefing.py` (`event` items, `calendar`
and `integrations` keys), `context/core/off_track_rules.json` (`integration_error`),
`backend/main.py` (include router), `backend/tests/test_phase6c_integrations.py` (tests
1–4, 9), `CLAUDE.md`, `docs/phases/PROGRESS.md`.

**Definition of done:** OAuth start/callback with HMAC state; refresh on expiry;
`invalid_grant` → error state; sync upserts and soft-deletes; `calendar_add_event`
creates remotely (patched) and is undoable with a remote delete; briefing shows timed
events at 50–70; commit `Phase 6C.2: Google Calendar integration`.

**Prompt:**
```
You are building Founder OS (FastAPI backend in backend/). Rules:
1. Read docs/CONVENTIONS.md (§2 undo hooks, §6 auth exemptions), docs/phases/
   PHASE_6_SPEC.md §0 and §6C "Google Calendar", docs/adr/0004 (OAuth paragraph), and
   PROGRESS.md first. Make no design decisions of your own.
2. Python 3.12; tests never reach the network: patch `integrations_google.httpx`
   (get/post/delete). Suite green with no GOOGLE_* env vars.
3. Tokens live in integrations.secret_json and are never returned; all logs through
   integrations.redact(). Event creation is logged (record_create on the cache row) and
   undo deletes the remote event best effort. Sync does not write change_log rows.
4. No frontend changes. No deletions or renames. No new dependencies (httpx + stdlib).
5. Lazy `import main` inside functions.
6. Run `python -m pytest -q` and the startup one-liner from backend/ before committing.
7. One commit: `Phase 6C.2: Google Calendar integration`.
8. If blocked, keep the suite green, commit nothing half-done, record it in PROGRESS.md.

Task — §6C "Google Calendar" in full:
- integrations_google.py: start URL builder (scope calendar.events, access_type offline,
  prompt consent, HMAC state with 10-minute validity stored as pending_state), callback
  (auth-exempt path; verify state; exchange code; store tokens; redirect to
  FOUNDER_OS_FRONTEND_URL/#settings?connected=google or ?error=...), _token() with
  refresh, sync(window_days=30) with the documented query and soft-delete, create_event()
  in FOUNDER_OS_TZ (RFC3339), today(), disconnect() with revoke. Mark every Google field
  name with `# VERIFY` where the spec says so.
- change_log._after_undo_hook: calendar_events create undo → remote DELETE best effort.
- Endpoints: GET /integrations/google/start, GET /integrations/google/callback (exempt
  in auth.EXEMPT_EXACT), POST /integrations/google/sync, POST
  /integrations/google/disconnect, GET /calendar/today, GET /calendar/week, POST
  /calendar/events. Agent calendar_sync (every 30 min, tables_write calendar_events and
  integrations, emits calendar.changed).
- Tools calendar_today and calendar_add_event (error when no clock time); prompt section
  "CALENDAR AND EMAIL" (calendar bullets now, email bullets in S19).
- Briefing: event items (50 + max(0, 20 - hours_until*2), cap 70, HH:MM labels), the
  calendar and integrations keys, today_events for all-day; alert integration_error with
  _check_integration_error.
- Tests 1–4 and 9 of §6C. Env vars to CLAUDE.md; append S18 to PROGRESS.md with the exact
  Google Cloud console steps the user must do (project, enable API, OAuth client with
  the Render callback URL, consent screen scope, publish).
```

---

### S19 — 6C.3 Email over IMAP/SMTP: sync, reply detection, outreach sending

**Must exist first:** S15, S5 (leads/outreach), S6 (contacts/communications), S13.

**Files touched:** `backend/integrations_email.py` (new), `backend/outreach_channels.py`
(`EmailChannel` wired), `backend/agents/email_sync.py` (new), `backend/orchestrator_tools.py`
(`email_lookup`), `context/core/orchestrator_prompt.md`, `backend/integrations.py`
(email actions; `GET /email/messages`), `backend/main.py`,
`backend/tests/test_phase6c_integrations.py` (tests 5–6), `backend/tests/test_phase5b_leads.py`
(email send test updated to the configured path), `CLAUDE.md`, `docs/phases/PROGRESS.md`.

**Definition of done:** fake IMAP yields the specified matching behaviour and events;
SMTP send stores an `out` row and 5B's email channel marks `sent`; unknown senders are
never stored; commit `Phase 6C.3: email sync and sending over IMAP/SMTP`.

**Prompt:**
```
You are building Founder OS (FastAPI backend in backend/). Rules:
1. Read docs/CONVENTIONS.md, docs/phases/PHASE_6_SPEC.md §0 and §6C "Email",
   docs/phases/PHASE_5_SPEC.md §5B "Channel adapters" and "The approval gate",
   docs/adr/0004, and PROGRESS.md first. Make no design decisions of your own.
2. Python 3.12; tests never reach the network: patch `integrations_email.imaplib.IMAP4_SSL`
   and `integrations_email.smtplib.SMTP_SSL` with fakes. Suite green with no EMAIL_* env.
3. Only messages from known lead/contact/client addresses are stored, as snippets
   (≤ 500 chars) — never full bodies. Sending happens only through the outreach
   approval gate. Credentials never appear in logs or last_error (integrations.redact).
4. No frontend changes. No deletions or renames. Stdlib only (imaplib, smtplib, email).
5. Lazy `import main` inside functions.
6. Run `python -m pytest -q` and the startup one-liner from backend/ before committing.
7. One commit: `Phase 6C.3: email sync and sending over IMAP/SMTP`.
8. If blocked, keep the suite green, commit nothing half-done, record it in PROGRESS.md.

Task — §6C "Email" in full:
- integrations_email.py: configured(), send() (SMTP_SSL, EmailMessage, make_msgid,
  plain text; stores an email_messages row direction out; returns {ok, message_id,
  error}), fetch(since=None) (IMAP4_SSL, SELECT INBOX, SEARCH SINCE last 3 days, header
  fields + first 2000 bytes of text + X-GM-THRID where present; skip known message_ids;
  store only when the sender matches a lead, contact or client email; communications
  row; lead contacted → replied + follow-up done + emit lead.replied; In-Reply-To
  matching outreach external_id → outreach_message_id and replied_at), error handling →
  integrations row status/last_error (redacted). Endpoints POST /integrations/email/sync,
  POST /integrations/email/test, GET /email/messages.
- outreach_channels.EmailChannel.send → integrations_email.send (external_id =
  message_id); when not configured keep the existing failure message.
- Agent email_sync (every 30 min; tables_write email_messages, communications, leads,
  todos, integrations; emits lead.replied).
- Tool email_lookup; prompt email bullets.
- Tests 5 and 6 of §6C; update the 5B email test so that with EMAIL_* set and SMTP
  patched the message reaches `sent`. Env vars to CLAUDE.md; append S19 to PROGRESS.md
  with the Gmail steps (2-Step Verification, App Password, IMAP enabled).
```

---

### S20 — 6A.1 Applications: schema, requirements import, endpoints, tools

**Must exist first:** S1 (PROGRESS.md). Independent of Phase 5.

**Files touched:** `backend/models.py` (`Application`, `ApplicationRequirement`,
`ApplicationGap`), `backend/migrations/m019_applications.py` (new),
`backend/applications.py` (new: primitives except `review_gaps`; endpoints),
`backend/orchestrator_tools.py` (`add_application`, `import_requirements`,
`update_requirement`, `application_status`), `context/core/orchestrator_prompt.md`,
`backend/actions.py` (`DELETABLE`), `backend/main.py` (register m019; include router),
`backend/tests/test_phase6a_applications.py` (new, tests 1–3, 7), `docs/phases/PROGRESS.md`.

**Definition of done:** import drops dates without a source phrase; user deadlines win;
upsert by title; advance/make_todo semantics; `application_status` reports `unknown`
deadlines honestly; commit `Phase 6A.1: applications, requirements import, tools`.

**Prompt:**
```
You are building Founder OS (FastAPI backend in backend/). Rules:
1. Read docs/CONVENTIONS.md, docs/phases/PHASE_6_SPEC.md §0 and §6A, and PROGRESS.md
   first. Make no design decisions of your own.
2. Python 3.12; tests never reach OpenAI/network (patch llm.chat_text); suite green with
   no key.
3. Dates come only from the user or the pasted text: a requirement whose due_date has
   no due_text is stored without a date; app.deadline is only set from a document when
   deadline is null or deadline_source != "user". The LLM prompt says so explicitly.
   Writes through change_log in one batch per import.
4. No frontend changes. No deletions or renames.
5. Lazy `import main` inside functions.
6. Run `python -m pytest -q` and the startup one-liner from backend/ before committing.
7. One commit: `Phase 6A.1: applications, requirements import, tools`.
8. If blocked, keep the suite green, commit nothing half-done, record it in PROGRESS.md.

Task — §6A except review_gaps, the briefing and the agent (S21):
- models + migrations/m019_applications.py (three tables incl. the `superseded` gap
  status; indexes; RLS); register.
- applications.py: find_application, create (deadline_source "user" when a deadline is
  given), import_requirements (one llm.chat_text call, purpose application_parse,
  json_object, max_tokens 1200, text truncated to 12,000 chars; the schema and the
  date rule from the spec; upsert by lower(title); requirements_updated_at; returns
  {created, updated, deadline_set, changes}), advance, set_status, make_todo (text
  "{app.name}: {req.title}", due = req.due_date or deadline - 7 days, category
  applications, idempotent while open), timeline(). Endpoints: GET /applications, GET
  /applications/{id}, POST /applications, PATCH /applications/{id} (deadline →
  source user), POST /applications/{id}/requirements/import, POST
  /applications/{id}/requirements, PATCH /requirements/{id}, POST
  /requirements/{id}/advance, POST /requirements/{id}/todo. GET /applications/{id}/gaps
  returns stored open gaps only for now (review arrives in S21). Include the router.
- Tools add_application, import_requirements, update_requirement, application_status
  (deadline and deadline_source always present in the result); prompt section
  "APPLICATIONS" with the never-state-a-date-from-knowledge rule.
- actions.DELETABLE gains applications (name), application_requirements (title).
- Tests 1, 2, 3, 7 of §6A. Append S20 to PROGRESS.md.
```

---

### S21 — 6A.2 Profile-gap review, briefing, deadline agent and alert

**Must exist first:** S20, S13 (agent), S16 optional (notifications of the events).

**Files touched:** `backend/applications.py` (`review_gaps`, `PATCH /gaps/{id}`, gaps
endpoint with refresh), `backend/agents/application_watch.py` (new),
`backend/orchestrator_tools.py` (`application_gaps`, `_check_application_at_risk`),
`context/core/off_track_rules.json`, `backend/briefing.py` (`_application_items`,
`counts.applications_at_risk` in attention), `backend/tests/test_phase6a_applications.py`
(tests 4–6), `docs/REBUILD_HANDOFF.md` §2, `docs/phases/PROGRESS.md`.

**Definition of done:** cached review (no call when unchanged), previous gaps superseded;
briefing scores as specified; `application_watch` emits once per mark; alert fires inside
14 days with missing items; commit `Phase 6A.2: application gap review, briefing,
deadline watch`.

**Prompt:**
```
You are building Founder OS (FastAPI backend in backend/). Rules:
1. Read docs/CONVENTIONS.md, docs/phases/PHASE_6_SPEC.md §0, §6A and the agent table in
   §6B, and PROGRESS.md first. Make no design decisions of your own.
2. Python 3.12; tests never reach OpenAI/network; suite green with no key.
3. review_gaps is cached: no LLM call while gaps_reviewed_at >= requirements_updated_at
   unless refresh=True; at most 2 LLM calls per application_watch run.
4. No frontend changes. No deletions or renames.
5. Lazy `import main` inside functions.
6. Run `python -m pytest -q` and the startup one-liner from backend/ before committing.
7. One commit: `Phase 6A.2: application gap review, briefing, deadline watch`.
8. If blocked, keep the suite green, commit nothing half-done, record it in PROGRESS.md.

Task — finish §6A:
- applications.review_gaps (deterministic context: requirements, founder_profile.md,
  syllabus progress per subject, 3 notes_search hits for the name, recalled memories;
  one llm.chat_text call purpose application_gaps, max_tokens 700, ≤ 8 gaps; previous
  open gaps → superseded; record_create per gap; gaps_reviewed_at). GET
  /applications/{id}/gaps?refresh=, PATCH /gaps/{id}.
- Tool application_gaps; prompt bullet (relay as the system's view; say when last run).
- briefing._application_items with the exact scores (deadline max(10, 82 - days) within
  60 days; requirement due ≤ 14 d → 60 + (14 - days); high gaps within 90 days → 24, max
  2), counts.applications_at_risk added to attention_count.
- Agent application_watch (at 05:45; tables_write application_gaps, applications; emits
  application.deadline_near at the marks in notification_settings.deadline_days_json,
  default [30, 14, 7, 3, 1], once per (application, days); runs review_gaps(refresh=True)
  for applications whose requirements changed since the last review).
- Alert application_at_risk with _check_application_at_risk.
- Tests 4, 5, 6 of §6A. REBUILD_HANDOFF §2 for /applications; append S21 to PROGRESS.md.
```

---

### S22 — Phase 6 UI: Agents page rework, Settings cards, Applications page, Command Centre indicators, docs

**Must exist first:** S14, S16, S17, S18, S19, S21.

**Files touched:** `frontend/index.html`, `docs/REBUILD_HANDOFF.md` (Phase 6 section),
`CLAUDE.md`, `ROADMAP.md` (Phase 6 → "core built"), `README.md` (status paragraph and
feature list brought up to date), `docs/phases/PROGRESS.md`.

**Definition of done:** Agents page lists the registry with Enable/Run and a run log with
Undo per run and a scheduler strip; Settings has Integrations (status chip + one action
per provider, Google connect via `start.url`, Telegram pair code and bot link) and
Notifications (five controls) cards; Applications page lists applications with
countdown, requirement rows with **Advance** and **Make it a task**, open gaps with
**Addressed / Accept risk**; Command Centre shows today's timed events, "Sent to Telegram
at HH:MM", and integration warnings; screenshots checked; docs updated; commit `Phase 6
UI: agents, integrations, notifications, applications, command centre`.

**Prompt:**
```
You are building Founder OS. This session touches frontend/index.html and four docs.
Rules:
1. Read docs/CONVENTIONS.md §12, docs/phases/PHASE_6_SPEC.md ("Endpoints", "Friction"
   and the frontend notes of §6B, §6C, §6D, §6A), docs/REBUILD_HANDOFF.md §2 and
   PROGRESS.md first. Read index.html by line range: the Agents page (renderAgents),
   the Settings page (p-settings), the Command Centre (renderHome, loadBriefing), and
   the Business page from S11/S12 as the list-page template.
2. Screens are for viewing and one-tap corrections. Allowed inputs: the five
   notification settings controls (time fields and selects), the Enabled toggles, and
   nothing else.
3. escHtml on every string; one fetch per tap; renderChangesStrip(changes) after
   mutations; existing tokens and classes; works at 390 px; respect
   prefers-reduced-motion.
4. Do not change any backend file. Do not remove or rename existing functions.
5. Verify with headless Chromium (Playwright pre-installed) at 390 px and 1280 px for
   Agents, Settings, Applications and the Command Centre (with and without integrations
   connected); record observations in PROGRESS.md.
6. Docs: REBUILD_HANDOFF.md "Phase 6" section; CLAUDE.md file table, env vars, auth
   exemptions and key concepts; ROADMAP.md Phases 5 and 6 to "core built" with Remaining
   lists from PROGRESS.md; README.md status and feature list rewritten to match (keep it
   short; link to ROADMAP.md).
7. One commit: `Phase 6 UI: agents, integrations, notifications, applications, command
   centre`.
8. If blocked, commit nothing half-done and record it in PROGRESS.md.

Task:
- Agents page: scheduler strip from GET /jobs/status (last tick age, trigger, token
  configured; warning when older than 60 min); registry from GET /agents with phase chip,
  effective schedule, last run chip, cost this month, Enable toggle (PATCH), Run (POST
  …/run with a running state); row tap → GET /agents/{key} runs list with Undo (POST
  /agents/runs/{id}/undo) and the market-intelligence detail for that agent.
- Settings: Integrations card from GET /integrations (status chip, account label, last
  sync, one button per `actions`: Connect → GET /integrations/google/start then
  location.href = url; Sync; Disconnect behind a confirm dialog; Pair → shows bot_link
  and pair_code from GET /notifications/settings; Set webhook). Notifications card from
  GET/PATCH /notifications/settings (enabled, morning_time, evening_time, quiet range,
  alert_min_severity, daily_cap) plus Send test and Brief now buttons and the last 10
  notifications with status chips. Keep the existing Session, Social Score, KPI Targets
  and Data cards.
- Applications page (new nav item under Annual Targets): GET /applications → cards with
  countdown (or "deadline unknown"), status chip, requirement rows (status colour,
  Advance → POST /requirements/{id}/advance, Make it a task → POST /requirements/{id}/todo),
  open gaps with Addressed / Accept risk (PATCH /gaps/{id}) and a Refresh review button
  (GET …/gaps?refresh=1 with a loading state).
- Command Centre: from the extended GET /briefing/today render calendar.events_today as
  a "Today" strip with HH:MM, `delivery.last_sent_at` as "Sent to Telegram at HH:MM",
  and an integrations warning line when any status is error or Telegram is not paired
  (links to Settings).
- Docs as rule 6. Append S22 to PROGRESS.md.
```

---

## After S22

- Set the Render env vars and GitHub secrets listed in `PROGRESS.md` (S14, S16, S18,
  S19). Pair Telegram, connect Google, create the Gmail app password, set
  `YOUTUBE_API_KEY`.
- Watch the first 24 hours of `GET /jobs/status` and `GET /agents/runs`.
- Resolve every **VERIFY** item from the specs against the live services and record the
  outcome in `docs/phases/PROGRESS.md`; any divergence becomes a small follow-up session
  with the same eight rules.
