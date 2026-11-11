# Phases 5 and 6 — progress

Branch `phase-5-6`, cut from `main` at `15bdbfd` (which already contains the assistant
current-data fix and the tool-call history sanitiser). One commit per session, pushed as it
lands. Nothing here is merged into `main`.

**Resume here:** read the last entry under "Sessions" and start the next item in the order
below. Run the checks before starting; do not start a session while anything is red.

## Order

S1, S3, S6, S7, S4, S5, S11, Portfolio UI, S8, S2, S13, S14, Agents UI, S20, S21,
Applications UI, S9, S10, Brand UI, S15, S16, S17, S18, S19, rest of S22 (Settings cards,
Command Centre indicators, docs).

## Checks after every session

```
cd backend
python -m pytest -q
python -c "import asyncio, main; asyncio.run(main.startup())"
cd ..
node tests/e2e/smoke.js          # PW_CHANNEL=chrome on this machine
node tests/e2e/screens.js        # after UI sessions
```

On this machine the checks run with the repo's `.venv` (Python 3.12), a Playwright install
outside the repo (`NODE_PATH`) and `PW_CHANNEL=chrome` (Playwright's own Chromium is not
installed and the `msedge` channel fails to launch headless here).

## Where the repo differs from the session prompts

- The frontend is on the shared design system (tokens, `.card`, `.row`, `.badge`, `.chip`,
  `.btn`, the Design Guide page). Where a prompt names an older class or function
  (`.ar-btn-sm`, `.sr-btn`, `.card-title`, `layoutHome()`), the shared equivalent is used and
  the substitution is listed in that session's entry.
- New tools read current data through `assistant_state.py` and never read the legacy
  tables; `tests/test_assistant_current_data.py` stays green.
- One host: Render serves the app at `/app/` and the API beside it. There is no Netlify
  site; the ADRs are corrected in the sessions that touch them.
- Hosting stays on the Render free tier, so the 15-minute GitHub tick of ADR-0001 ships
  with `workflow_dispatch` only (schedule commented out) and the in-process tick stays.

## Sessions

### S1 — 5A.1 market intelligence on the gateway (done)

Shipped: `llm.research_model()` (`RESEARCH_MODEL`, default the assistant model); every
market-intelligence role calls `llm.chat_completion` with a `market_intel_<role>` purpose;
`research_projects.cost_usd` and `llm_calls` summed from `llm_usage` after each run;
`market_intel/scoring.py` with weights in `context/core/opportunity_weights.json`;
`recommendation` stored on every finding and backfilled by m014; dedup before insert
(embeddings at cosine ≥ 0.90, keyword fallback); findings, repeats, promotions, dismissals
and pipeline stage changes are in the change log; `research_objectives` table and model;
`agent.dismiss_finding` primitive. Tests: `tests/test_phase5a_market_intel.py` (acceptance
tests 1–5 and the m014 backfill), `tests/test_market_intel.py` updated (test 8).

Deferred to S2: objectives endpoints and tools, the dismiss endpoint and tool, the extended
project and pipeline responses, briefing items, the prompt paragraph.

Decisions where the spec was silent:
- Keyword dedup overlap is shared words ÷ the shorter problem's word count (≥ 60 %), with
  the customer segment compared case-insensitively after trimming.
- When embeddings are available and no stored finding reaches 0.90, the candidate is new;
  the keyword rule runs only when there is no key.
- A repeat is written to the change log ("Seen again: …") like any other write, so its
  `times_seen` bump and appended evidence can be undone.
- A dismissed finding still counts as "already on file", so a later run does not re-surface
  something Bandile has rejected.
- `POST /research/promote` and `PATCH /research/pipeline/{id}` now return `changes` /
  `change` in addition to their existing fields.
- The two inline fake model functions in `tests/test_market_intel.py` also accept `**_`
  (the spec names only `_fake_llm`; all three receive the new `purpose` argument).

### S3 — 5B.1 lead database, CSV import, segmentation (done)

Shipped: `business_common.py` (`fuzzy_pick`, `money`, `sum_usd`, `days_since`);
`context/core/icp.md`; `Lead`, `LeadImport`, `OutreachMessage` models and
`todos.lead_id` / `todos.client_id` (m015, with RLS on the three tables); `leads.py` with
normalisation, the dedupe key, `create_lead`, `import_csv`, segments and the endpoints
`GET /leads`, `GET /leads/segments`, `GET /leads/{id}`, `POST /leads`, `PATCH /leads/{id}`,
`POST /leads/import`; tools `add_lead` and `find_leads`; the "PROSPECTING AND OUTREACH"
prompt section (first bullet); `leads` and `outreach_messages` are deletable through the
confirmation flow. Tests: `tests/test_phase5_common.py`, `tests/test_phase5b_leads.py`
(acceptance tests 1–2), fixture `tests/fixtures/leads_sample.csv`.

Deferred: qualification, drafts, the approval gate and follow-ups (S4); replies, conversion,
stats, briefing and the alert (S5).

Decisions where the spec was silent:
- New tools are registered at the end of `orchestrator_tools.py` through a small
  `_register(name, fn, description, properties, required)` helper, so the rebuild's schema
  list stays byte-stable (prompt caching) and each tool's dispatch and schema sit together.
- Every new tool is also exercised in `tests/test_assistant_current_data.py`, whose guard
  requires every registered tool to run without touching a legacy table.
- The dedupe key is strict: an incoming row with an email is matched on the email only;
  a row with no email is matched on its WhatsApp or phone number against either stored
  number; a row with neither is matched on lower-cased name and company.
- A row with a company and no name is stored with the company as its name (`name` is
  `NOT NULL`).
- On a duplicate, incoming notes are appended line by line (lines already present are not
  repeated), so a re-import is idempotent. A re-import counts every matched row as
  `updated` even when nothing changed, and writes no change-log row for it.
- A phone number supplied without a WhatsApp number fills WhatsApp on create, and never
  replaces a stored WhatsApp number on update.
- When two CSV columns map to the same lead field, the first wins and the second is kept
  in `notes` as `Header: value`.
- `GET /leads?q=` is a case-insensitive contains match on name or company; the fuzzy
  accept rule (`fuzzy_pick`) is used where a write depends on the match.

### S6 — 5C.1 CRM schema, clients, contacts, deals, communications (done)

Shipped: `Contact`, `Deal`, `Project`, `Payment`, `RecurringRevenue`, `Communication`
models and four new `clients` columns (m016, with RLS, the partial unique index on
`payments (recurring_id, period_start)` and both backfills); `crm.py` with the lookups,
`client_health`, `add_contact`, `log_communication`, `create_deal`, `update_deal` and the
endpoints `GET /clients/{id}`, `POST /clients/{id}/contacts`, `PATCH /contacts/{id}`,
`GET/POST /deals`, `PATCH /deals/{id}`, `GET/POST /communications`; `GET /clients` and
`PATCH /clients/{id}` extended in place in `main.py` (existing fields kept, the patch is now
logged); tools `crm_lookup`, `log_communication`, `update_deal`, `add_contact`; the
"CLIENTS, DEALS AND PAYMENTS" prompt section; six more deletable tables;
`retrieval.index_crm`; `business_common.parse_day`. Tests: `tests/test_phase5c_crm.py`
(acceptance tests 1, 6, 7 plus clients, contacts and health).

Deferred to S7: payments, recurring revenue, reconciliation, `GET /crm/summary`, the
briefing items and the overdue alert.

Decisions where the spec was silent:
- `clients.currency` is added with no SQL default so that rows which predate the column are
  the ones holding NULL; the backfill sets exactly those to `USD` and is safe to re-run. New
  clients get `ZMW` from the ORM default.
- Silence (no communication in 21 / 45 days) only counts for clients whose status is
  `active`, and is measured from the last communication or, when there is none, from the
  day the client was created.
- "Deal in negotiation > 30 days" is measured from the deal's `updated_at`.
- Health is computed live in `GET /clients` and `GET /clients/{id}`; the stored column is
  refreshed by the CRM summary and the housekeeping agent (S7, S14).
- Communications are embedded as `source_type="crm"` and project notes as `"crm_project"`
  (the spec gives both the same type, but the `embeddings` key is `(type, id)` and the two
  tables' ids would collide).
- Bumping `last_activity_at` on a client's active projects is not written to the change log
  (bookkeeping, not a statement of Bandile's).
- Making a contact primary clears the flag on the client's previous primary contact (logged).
- `update_deal` with only a client and no deal name acts on that client's one open deal; with
  several open deals it returns them as candidates.
- `business_common.parse_day` wraps `syllabus.parse_date_text` and adds today, tomorrow,
  yesterday, weekday names, "in N days", "N days ago", "end of month" and "end of week". With
  `past=True` (communications, payments received) a date never resolves to the future.

### S7 — 5C.2 payments, recurring revenue, reconciliation, summary, briefing (done)

Shipped: `crm.record_payment`, `mark_paid`, `add_recurring`, `generate_due`,
`refresh_statuses`, `reconcile`, `refresh_health`, `housekeeping`, `summary`; endpoints
`GET/POST /payments`, `POST /payments/{id}/paid`, `POST /payments/{id}/cancel`,
`GET/POST /recurring`, `PATCH /recurring/{id}`, `GET /crm/summary`; tools `record_payment`
and `crm_summary`; briefing `_crm_items`, `business_detail`, `counts.payments_overdue` in
`attention_count`; alert `payment_overdue_14d` with `_check_payment_overdue`; the 5C
endpoint shapes in `docs/REBUILD_HANDOFF.md` §2. Tests: acceptance tests 2–5 and 8–10 in
`tests/test_phase5c_crm.py`.

Deferred: nothing from §5C.

Decisions where the spec was silent:
- The `record_payment` tool with status `paid` first looks for the client's oldest open
  payment in the same currency within 5 % of the amount and settles that one (the same rule
  as reconciliation), so "they paid" after "I invoiced them" does not create a second
  payment and a second revenue row. The result carries `settled_existing: true`.
- `refresh_statuses` and `refresh_health` are not written to the change log: both values are
  derived from dates and other rows. Generated instalments, the `next_due` move and
  reconciled payments are logged, with source `job:crm`.
- `generate_due` catches up at most 60 periods per recurring row per call, and stops at
  `ended_at`.
- A paid payment cannot be cancelled (409); the way back is Undo on the payment's change.
- `GET /payments` takes `status` as a comma-separated list (default `due,overdue`), and
  `from` / `to` filter on the due date, or the paid date when there is no due date.
- The health counts in `GET /crm/summary` cover clients that are not `closed` or `lost`.
- `business_detail.revenue_pct` is the revenue annual target's progress (what
  `progress.business` shows today), or null when no revenue target is set. The spec's
  "$10K scoreboard" wording predates the current-data fix; the legacy scoreboard is not read.
- The briefing runs `crm.housekeeping` before it builds the CRM items, so `GET
  /briefing/today` can write (generated instalments, overdue flags). A failure there is
  logged and the briefing still renders.

### S4 — 5B.2 qualification, outreach drafts, the approval gate, follow-ups (done)

Shipped: `leads.qualify` (pre-score 0–40 plus one `lead_qualify` call per lead, capped at
20) and `POST /leads/qualify`; `leads.set_follow_up` / `close_follow_ups`; `outreach.py`
(`draft`, `request_send`, `execute_send`, `cancel`; `GET /outreach`, `GET /outreach/{id}`,
`POST /outreach/{id}/send`, `POST /outreach/{id}/cancel`); `outreach_channels.py`
(WhatsApp link, email, manual); `actions.confirm_action` dispatching on the action's kind
through `_EXECUTORS` (`delete`, `send_outreach`); tools `qualify_leads`, `draft_outreach`,
`request_send`, `set_follow_up`; the prompt bullets. Tests: acceptance tests 3–6 in
`tests/test_phase5b_leads.py`; `tests/test_stage2_changes.py` passes unchanged.

Deferred to S5: replies, conversion, stats, briefing items, the stalled-outreach alert.

Decisions where the spec was silent:
- The "confirmation must come in a later message" rule applies to the assistant's
  `confirm_action` tool. The dock's Confirm tap (`POST /actions/{id}/confirm`) passes
  `human=True`, because a tap is itself the approval; without that a send requested in
  conversation could never be confirmed from the dock.
- A qualification score changes a lead's status only while the lead is `new`, `qualified`
  or `disqualified`; a lead already contacted keeps its status and just gets the new score.
- With no AI key, qualification reports the pre-score and stores nothing.
- Revising a draft cancels the previous version only if it was still a draft; a version
  that was already sent is left as it is. Cancelling a draft also closes its open
  pending action.
- A successful send moves the lead to `contacted` only from `new`, `qualified` or
  `disqualified`, and does not create a second follow-up if one is already open.
- A failed send leaves the lead untouched and creates no follow-up; the pending action
  that triggered it is closed as `confirmed` (the approval was given; the channel failed).
- `business_common.parse_day` is used for `set_follow_up` (it wraps
  `syllabus.parse_date_text` and adds weekday names and "in 3 days").
- `EmailChannel` already calls `integrations_email` when that module exists and reports
  itself configured; until S19 it returns "email sending is not configured".

Known limits:
- A WhatsApp link is built from the stored digits exactly as the spec says. A local number
  kept as typed (`0977…`) therefore produces `wa.me/0977…`, which WhatsApp does not accept;
  only numbers stored with a country code (`+260…`) open correctly. See follow-ups.
- In live voice the spoken "yes" counts as a later message only once its transcript line
  has been saved; if the model confirms before that, it is told to wait, and the dock tap
  works regardless.

### S5 — 5B.3 replies, conversion, stats, briefing and alert (done)

Shipped: `leads.log_reply` and `leads.convert` with `POST /leads/{id}/replied` and
`POST /leads/{id}/convert`; `outreach.stats` and `GET /outreach/stats`; tools
`log_lead_reply` and `convert_lead`; briefing `_outreach_items` and
`counts.drafts_waiting`; alert `outreach_stalled` with `_check_no_outreach_sent`; the prompt
bullets; every `/leads` and `/outreach` endpoint shape in `docs/REBUILD_HANDOFF.md` §2.
Tests: acceptance tests 7–10 in `tests/test_phase5b_leads.py`.

Deferred: nothing from §5B. Real email sending arrives with S19.

Decisions where the spec was silent:
- Stats: a lead counts as contacted when a message to it was sent inside the window;
  replied when its status is `replied` or `converted` or it has an inbound message;
  converted when its status is `converted`. `leads` per segment is every lead in the
  segment, whenever it was added.
- A reply logged with no summary stores the text "Replied". The reply's channel is the one
  given, else the channel of the last outbound message, else WhatsApp.
- A reply does not move a lead that is already `converted` or `lost` back to `replied`.
- A converted client is named after the lead (`name`, `company`), is `active`, in ZMW, and
  takes `service` from the brief of the last sent message. The lead's contact details become
  the client's primary contact when it has an email or a number.
- A draft whose send request is older than 24 hours is left out of the briefing (the
  request has expired; the draft is still on the Pipeline tab).
- Quiet leads in the briefing use target type `lead`.

## Environment variables added so far

| Var | Default | Session | Purpose |
|---|---|---|---|
| `RESEARCH_MODEL` | the assistant model | S1 | Model for the market-intelligence roles |

## VERIFY items (from the specs)

Collected here as the sessions that carry them are built; each is checked against the live
service after deployment.

- 5A: the web adapter still posts to `https://html.duckduckgo.com/html/` with no key. Not
  checked in this build (tests never reach the network). If it blocks, the adapter returns
  `[]` and a run proceeds on War Room and vault evidence only.

## Follow-ups noticed (not done here)

- WhatsApp links for local numbers: add a default country code (for example an env var
  `WHATSAPP_DEFAULT_COUNTRY_CODE=260`) applied only when building the `wa.me` link for a
  number that starts with a single `0`. Today those links do not open a chat.

- `tests/test_market_intel.py` uses the deprecated `Query.get()` in three places.
