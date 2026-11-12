# Phases 5 and 6 — progress

Branch `phase-5-6`, cut from `main` at `15bdbfd` (which already contains the assistant
current-data fix and the tool-call history sanitiser). One commit per session, pushed as it
lands. Nothing here is merged into `main`.

**State on 12 November 2026:** every session in the order below is built, committed and
pushed. After that a review round ran on the branch (the section "Review round" below):
fourteen fixes, the suite and the startup chain on PostgreSQL 16, and an independent review
of the whole diff. Still not done: any check against the live Telegram, Google, Gmail or
YouTube services. What is left is under "Review round", "Follow-ups noticed", the "Limits to
know" of each session, and `ROADMAP.md`.

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
  the ones holding NULL; the backfill sets exactly those to `DEFAULT_CURRENCY` (ZMW unless
  the variable says USD) and is safe to re-run. New clients get the same default from the
  ORM. (Corrected in the review round: the first version set old clients to USD.)
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
- (Fixed in the review round.) A WhatsApp link was built from the stored digits as typed, so
  a local number (`0977…`) produced `wa.me/0977…`, which WhatsApp does not accept. The link
  now carries `DEFAULT_COUNTRY_CODE` (260) for local numbers; the stored number is unchanged.
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

### S11 — Phase 5 UI, part 1: Business page with Pipeline and Clients (done)

Shipped (`frontend/index.html`): a Business item in the rail between Agents and War Room;
page `p-business` with four tabs (Pipeline, Clients, Portfolio, Brand — the last two are
placeholders until their sessions); the active tab is remembered in `localStorage`
(`founderOS_bizTab`). The home screen's Business tile is now a live link.
- Pipeline: a funnel line from `GET /outreach/stats`; "Drafts waiting" cards from
  `GET /outreach?status=draft` with Approve & send (opens `open_url` in a new tab), Copy and
  Dismiss; segment filter chips from `GET /leads/segments`; leads grouped by status, with
  score and last-message badges and the follow-up date; one tap per row — Replied on a
  contacted lead, Convert on a replied one; disqualified and lost leads collapsed.
- Clients: a KPI strip from `GET /crm/summary`; client rows with a health dot, outstanding
  and monthly recurring by currency; a tap opens the client (contacts, deals with stage
  chips where the next stage is a button, payments with Paid on due and overdue rows, the
  last ten conversations).
- Every tap is one request; its `changes` go to the dock's change strip with Undo, and an
  undo redraws the page. All text goes through `escHtml`.
- Design system: two new shared components, `.tabs` / `.tab` and tappable `.chip`
  (`.is-on`, `.is-dim`), added to the Design Guide page before use.
- `tests/e2e/screens.js` now seeds business data and checks three more states (Pipeline,
  Clients, one client) at all four sizes; `tests/e2e/smoke.js` gained a fifth flow (open a
  client, tap Paid, confirm the payment and its revenue row on the backend).

Checked in headless Chrome at 1280×720, 1536×864, 1920×1080 and 390×844. What the
screenshots showed: the tabs, cards, rows and badges match the other pages; no horizontal
scroll at any size; nothing is hidden under the talk bar; on the phone the draft card and
the lead rows wrap cleanly and the deal's stage chips wrap to a second line; the client
header shows the amber health badge and "K3,500 owed" for a client with an overdue payment.

Where the prompt's names no longer exist: `.ar-btn-sm` / `.sr-btn` → `.btn.btn--sm`;
`.badge b-glow|b-muted|b-suc|b-danger` → `.badge--glow|--muted|--success|--danger`;
`.btn-danger` → `.btn--danger`; `renderChangesStrip` still exists and is used as is.

Decisions where the spec was silent:
- "At most one tap per row": a lead row offers Replied only while the lead is `contacted`
  and Convert only once it has `replied`.
- For a WhatsApp draft the new tab is opened inside the tap (so the browser does not block
  it) and pointed at the `wa.me` link once the server has approved the send; if the send
  fails the tab is closed and a toast offers Copy instead.
- From `negotiation` a deal offers two next steps, Won and Lost; from `proposal`,
  Negotiation and Lost; from `lead`, Proposal only.
- The Clients KPI strip shows the summary's dollar totals (that is what
  `GET /crm/summary` returns) with the Kwacha amount beneath where the endpoint gives one.

Deferred / not built here:
- There is no screen control for `POST /leads/import` or `POST /leads/qualify` (the session
  rules allow no input beyond the dock and the segment chips). Leads arrive by
  conversation; a CSV import needs the API until a button is added. See follow-ups.

### S8 — 5E project portfolio (done)

Shipped: `ProjectUpdate` and m018; `portfolio.py` (`attention`, `recommendation`,
`health_of`, `review`, `propose_pauses`, `create_project`, `update_project`, `set_status`,
`focus`, `log_update`, `project_detail`; `GET /portfolio`, `GET/POST /projects`,
`GET/PATCH /projects/{id}`, `POST /projects/{id}/status`, `/focus`, `/updates`); suggestion
kind `project_pause` and the `project_updates` key in `_apply_parse_updates_inner` (not in
the parser's schema or prompt); tools `portfolio_review`, `update_project`,
`log_project_update`; briefing `_portfolio_items` and `progress.projects`; alert
`projects_blocked`; `project_updates` deletable; the prompt section; endpoint shapes in the
hand-off doc. Tests: `tests/test_phase5e_portfolio.py` (acceptance tests 1–7).

Deferred: nothing from §5E. The Portfolio tab is the next session.

Where the spec contradicts itself (ruling):
- Acceptance test 1 expects `focus ×3, pause ×1, close ×1` from five projects of which only
  three are active, one is paused and one was delivered 40 days ago. With the rules as
  written that cannot happen: either the stale project is `pause` and only two active
  projects are left to `focus`, or all three active projects are `focus` and nothing is
  ever `pause` (which would also make test 2 impossible). Built: recommendations are
  checked in the order close → pause → focus → maintain; a project recommended for a pause
  does not take a focus place; a project that is already paused stays `maintain`. The
  fixture therefore yields `focus ×2, pause ×1, maintain ×1, close ×1`, and the test
  asserts that. If the intent was that a paused project can be recommended `focus`
  ("resume it"), the change is one line in `portfolio.recommendation`.

Decisions where the spec was silent:
- `ranked` contains active, planned and delivered projects; `rank` is given to active
  projects only; closed and cancelled projects are not in the review.
- "Nothing due within 30 days" means no due date, or one more than 30 days away; an overdue
  project is never recommended for a pause.
- `last_activity_at` moves on every update, status change and field change of a project.
- A project that is both blocked and overdue appears in the briefing once, as blocked (62).
  The focus item (45) is shown only when the rank-1 project is not already listed as
  blocked, overdue or waiting.
- `GET /portfolio`, the `portfolio_review` tool and `GET /briefing/today` store health and
  file pause suggestions, so all three can write. Health is derived and not logged.
- The one-tap `POST /projects/{id}/status` accepts `paused`, `active`, `delivered` and
  `closed`; `planned` and `cancelled` go through `PATCH` or conversation.
- An overdue payment counts as a project's bottleneck when it is tied to the project, or to
  the project's client with no project named.

### Portfolio UI (split from S12) (done)

Shipped (`frontend/index.html`): the Portfolio tab of the Business page, from
`GET /portfolio` — a totals strip (active, blocked, overdue, active value); the ranked list
with rank, a health dot, title and client, the bottleneck (or the next action) and due
label, the recommendation badge, and the score with the six components in its `title`;
taps Focus (`POST /projects/{id}/focus`), Delivered and Pause (`POST /projects/{id}/status`),
Start on a planned project, Close on a delivered one; paused projects in a collapsed list
with Resume. A pause suggestion filed by the review shows in the existing suggestions chip
above the talk bar (labelled "Pause project"). The home screen's Projects tile is now a
live link to this tab.

Also: `showChanges()` makes sure the dock's change strip exists before a tap's changes are
rendered, so Undo is always offered (before, a tap made before any assistant reply showed
only a toast). `tests/e2e/screens.js` seeds four projects and checks the tab at all four
sizes; `tests/e2e/smoke.js` gained a sixth flow (Focus makes a project priority 1 and
demotes the previous one).

Checked in headless Chrome at the four sizes. What the screenshots showed: the totals strip
and the ranked card match the Clients tab; the blocked project is ranked first with a red
dot and "Blocked: waiting on the product photos"; the stale project carries the amber
Pause badge and the "1 suggestion" chip is lit above the talk bar; on the phone each row
wraps to title, then badge and score, then the buttons, with no horizontal scroll.

Decisions where the spec was silent:
- §0.7 asks for at most one tap per row, while this session's task lists Focus, Pause,
  Resume and Delivered. An active row offers Focus (unless it is already the priority-1
  project), Delivered and Pause; the one that matches the recommendation is the lit button.
- The rest of S12 (Brand tab, Agents page additions, docs) is built in its own sessions,
  in the order given for this run.

### S2 — 5A.2 standing objectives, dismiss, tools, briefing items (done)

Shipped: `market_intel/objectives.py` (`schedule`, `run_objective`, `due_objectives`;
`GET/POST /research/objectives`, `PATCH /research/objectives/{id}`,
`POST /research/objectives/{id}/run`); `POST /research/findings/{id}/dismiss`;
`GET /research/projects/{id}` with `cost_usd`, `llm_calls`, `objective_id`, `repeated`;
`GET` and `PATCH /research/pipeline` with `next_action` and `due_date`; tools
`schedule_research` and `dismiss_research_finding`; the research tool's summary now counts
repeats; the prompt additions; briefing `_opportunity_items` and
`counts.opportunities_new`; endpoint shapes in the hand-off doc. Tests: acceptance tests
6–7 (and the dismiss endpoint and tool for test 5) in `tests/test_phase5a_market_intel.py`.

Deferred: nothing from §5A. The Agents page controls (Dismiss, Standing objectives with
Run) come with the Agents UI session.

Test safety (found while writing these tests):
- `tests/conftest.py` now has a session-wide guard that fails any test which tries to open
  a non-loopback connection or resolve a hostname. It caught one test that predates this
  work: `test_stage1_assistant.py::test_summary_lands_in_context_message` set a fake key
  and let memory recall call the real embeddings endpoint (it got a 401 and carried on).
  That test now stubs `llm.embed_texts`. No other test tripped the guard.

Decisions where the spec was silent:
- `POST /research/objectives/{id}/run` answers 503 when no AI key is configured, instead of
  running a research pass that cannot think (it would still query the web and find nothing).
- `repeated` on a project is read back from the change log: the "Seen again" entries
  written while that run was in progress. No new column.
- An objective must be at least ten characters (the same floor as a research run).
  Rescheduling re-enables an objective that had been switched off.
- A pipeline entry is "stale" when its `updated_at` is 14 or more days old; the overdue
  rule (48) takes precedence over the stale rule (30).

### S13 — 6B.1 agent runner, registry, schedules, events, cost attribution, jobs tick (done)

Shipped: `backend/agents/` — `base.py` (`Schedule`, `Permissions`, `AgentSpec`,
`AgentContext` with the allow-listed `run_tool`, `notify`, `emit`, `log`, `AgentResult`),
`schedule.py` (`due`, `effective`, `describe`, `next_due`, the user's zone), `runner.py`
(`run_agent` steps 1–6, `tick` steps 1–5, the permission audit, `update_agent`, `due_now`),
`__init__.py` (`REGISTRY`, `register`, `ordered_specs`, `emit`, `load_all`); models `Agent`,
`AgentRun`, `AgentEvent`, `JobTick` and `llm_usage.agent_run_id` (m020, RLS);
`llm.set_run_context` / `clear_run_context` / `current_run_id` / `BudgetExceeded`, with
`log_usage(..., agent_run_id=)` and the call cap in `chat_completion` and `embed_texts`;
`POST /jobs/tick` (its own token, exempt from the login) and `GET /jobs/status`; a startup
warning for a short `JOBS_TOKEN`; the new env vars in `CLAUDE.md`. Tests:
`tests/test_phase6b_agents.py` (acceptance tests 1–8 and 11).

Deferred to S14: the shipped agents, `agents/api.py`, the three tools, the in-process tick
job and the workflow file.

Decisions where the spec was silent:
- Test 8 needs a logged toggle before the `PATCH /agents/{key}` endpoint exists (S14), so
  the toggle lives in a primitive, `runner.update_agent`, which the endpoint and the tool
  will call. `interval_minutes=0`, `at_time=""` and `weekdays=[]` clear an override.
- A schedule override of one kind replaces the spec's kind: giving an agent an `at_time`
  makes it daily; giving it an interval (with no `at_time` override) makes it an interval
  agent.
- The first due agent of a tick always runs; the budget is checked before each later one.
  `JOBS_TICK_BUDGET=0` therefore runs exactly one agent per tick.
- `agent_runs.trigger` is `schedule` for the external tick, `in_process` for the in-process
  one, `manual` for a manual tick or Run now, and `event` when only an event made it due.
- An event stays unconsumed until an agent that subscribes to it actually runs; after 24
  hours it is marked consumed and `dropped`. (Corrected in the review round: with two
  subscribers the event was consumed when the first one ran, so a subscriber the tick
  deferred never got it. Each hand-over is now a row in `agent_event_deliveries`, and an
  event is consumed once every enabled subscriber has one.)
- A failed run rolls back whatever it had not committed; what it had committed stays and is
  undoable from the run's batch. The audit runs for failed runs as well.
- `chat_text` goes through `chat_completion`, so one call counts once against the cap.
- The runner and the tick catch a failure to open a database session too (found by a test),
  so neither can raise into the HTTP handler or the scheduler thread.
- `FOUNDER_OS_TZ` falls back to a fixed UTC+2 when the machine has no time-zone database
  and the zone is the default; `tzdata` is already pinned in `requirements.txt`.

### S14 — 6B.2 shipped agents, agents API and tools, in-process tick, workflow (done)

Shipped: six agents in `backend/agents/` (`crm_housekeeper`, `lead_qualifier`,
`follow_up_sentinel`, `portfolio_reviewer`, `brand_sync`, `market_intel`) with the default
schedules, permissions and caps from the §6B table; `agents/api.py` (`GET /agents`,
`GET /agents/runs`, `GET/PATCH /agents/{key}`, `POST /agents/{key}/run`,
`GET /agents/{key}/runs`, `POST /agents/runs/{id}/undo`); tools `agent_status`, `run_agent`,
`set_agent_schedule` and the "AGENTS" prompt section; the in-process tick job (every 15
minutes, `JOBS_IN_PROCESS`); `.github/workflows/jobs-tick.yml`; the ADR-0001 amendment;
endpoint shapes in the hand-off doc. Tests: acceptance tests 9–10 and the API, tool,
workflow and in-process tests in `tests/test_phase6b_agents.py`.

Hosting decision for this run (recorded in ADR-0001): the workflow ships with
`workflow_dispatch` only and its `schedule` block commented out, because a call every 15
minutes would keep the free Render instance awake and use up its free hours. Agents run
from the in-process tick while the app is open.

To turn the external schedule on later:
1. On Render, set `JOBS_TOKEN` to a random value of 32 or more characters.
2. In the GitHub repository, Settings → Secrets and variables → Actions, add
   `FOUNDER_OS_URL` (the backend's base URL, no trailing slash) and `JOBS_TOKEN` (the same
   value as on Render).
3. In `.github/workflows/jobs-tick.yml`, uncomment the two `schedule` lines and push.
Before step 3 the workflow can be run by hand from the Actions tab once steps 1–2 are done.

Decisions where the spec was silent:
- `brand_sync` is registered now and reports `skipped` ("brand not built") until S9 adds
  the module; it syncs accounts whose `sync_mode` is `api`.
- `lead_qualifier` only takes leads that have never been scored, so a lead that stays `new`
  after a low score is not re-scored (and re-billed) every three hours.
- `follow_up_sentinel` emits `lead.stale` only when it adds a follow-up, so a quiet lead is
  flagged once, not daily.
- `crm_housekeeper`'s once-a-day rule for `payment.overdue` is read from `agent_events`
  (any event for that payment in the last 24 hours, consumed or not).
- `market_intel` (the agent) emits `research.completed` with the project id.
- The briefing's "research due" item (score 12) is shown only while the `market_intel`
  agent cannot run objectives itself (it is disabled, or there is no AI key).
- `GET /agents` adds `cost_usd_month`, `events` and `schedule.overridden` to the spec's
  shape (the Agents page needs the monthly cost).
- Run now ignores the Enabled toggle and the schedule: it is an explicit instruction.
- The `run_agent` tool commits the assistant's session first, because the agent works in
  its own session and must see what the turn has already written.
- `agents.load_all()` runs when `main` is imported, so the registry is complete before the
  first request.

Known limit:
- Embedding calls count against an agent's model-call cap. A research run that surfaces
  three new findings makes about nine role calls and up to six embedding calls; past the
  cap of 12 the later embedding calls are refused and the run falls back to keyword
  dedup for those findings (the run still completes).

### Note — a red smoke run after S14, and its fix

The S14 commit (`8752e5a`) was pushed while the smoke test showed one failed check
("seeded a client with a due payment"). The pytest suite and the startup chain were green.
The failure was in the test script, not the product: `tests/e2e/smoke.js` took "today" from
`toISOString()`, which is the UTC date, and the run happened at 00:05 local time (UTC+2),
so the payment it seeded as due today was a day old and correctly came back `overdue`.
Both e2e scripts now use the local date; the fix is its own commit straight after S14. The
check script used during this build now exits non-zero on any red, and every commit is
chained behind it, so a red tree cannot be committed again.

Also seen at the same moment: one pytest run that started at 23:59:50 and finished after
midnight failed a single date assertion (a test's module-level "today" against the code's
"today"). It passed on the rerun. Tests that hold `date.today()` at import can do this for
a few seconds a day; not changed.

### Agents UI (split from S12 and S22) (done)

Shipped (`frontend/index.html`): the Agents page rebuilt on the registry.
- A Scheduler card from `GET /jobs/status`: when the last tick ran and from where, how many
  agents are due now, ticks in 24 hours, whether the in-process tick is on and whether a
  scheduler token is set. When the last tick is more than an hour old it says so and
  explains that agents run while the app is open.
- The registry from `GET /agents`: name, phase badge, effective schedule, the last run's
  summary and status badge, model cost this month, an On/Off toggle
  (`PATCH /agents/{key}`, shown in the change strip with Undo) and Run
  (`POST /agents/{key}/run`, with a running state and a toast of the run's summary).
- A tap opens the agent: description, schedule, the events it reacts to, its model-call cap,
  the tables it may write, and its recent runs with duration, cost, the first three changes
  and Undo per run (`POST /agents/runs/{id}/undo`).
- Market intelligence keeps its detail under its agent: four KPIs, a Standing objectives
  card from `GET /research/objectives` (On/Off toggle, Run with a running state), research
  projects, the pipeline and the lesson memory. Surfaced findings now have Dismiss next to
  Promote, and show their recommendation and how often they have been seen.
- The home screen's Agents tile is now a live link.
- `tests/e2e/screens.js` seeds an objective, a research project with a finding and three
  agent runs, and checks the agent detail state; `tests/e2e/smoke.js` gained a seventh
  flow (the registry lists the agents, the toggle switches one off, Run runs one, a tap
  opens its run log).

Checked in headless Chrome at the four sizes. What the screenshots showed: the scheduler
card reads "Not run yet / No tick yet / 1 due now" on a fresh database with its
explanation line; six agent rows with status badges, toggles and Run buttons aligned in a
column on the desktop and wrapped under the name on the phone; the market-intelligence
detail shows the due objective with its toggle and Run, and an opened project shows the
finding with its score, badges and axes; nothing sits under the talk bar and nothing
scrolls sideways.

A backend bug found here and fixed in the same commit: the page asks for `GET /agents` and
`GET /jobs/status` together, and on a database with no `agents` rows both requests tried to
create the same row; the second failed on the unique key and left its session unusable
(seen in the smoke run's server log as `UNIQUE constraint failed: agents.key`).
`runner.agent_row` now rolls back and reads the row the other request wrote. Covered by
`test_agent_row_survives_two_requests_creating_it_at_once`.

Where the prompt's names no longer exist: `renderAgents`, `openAgentDetail`,
`closeAgentDetail` and `renderAgentDetail` keep their names; `renderAgentDetail` now draws
only the market-intelligence part of an agent's detail, and `openAgentDetail(key)` takes
the agent's key (default `market_intel`).

Decisions where the spec was silent:
- The warning for a tick older than 60 minutes does not say "GitHub Actions not seen",
  because the external schedule ships switched off; it says the scheduler is quiet and
  why.
- Server timestamps are naive UTC; the page reads them as UTC when showing "12 min ago".

### S20 — 6A.1 applications: schema, requirements import, endpoints, tools (done)

Shipped: `Application`, `ApplicationRequirement`, `ApplicationGap` (m019, RLS);
`applications.py` (`find_application`, `create`, `import_requirements`, `add_requirement`,
`advance`, `set_status`, `update_requirement`, `make_todo`, `timeline`, `set_gap_status`;
`GET/POST /applications`, `GET/PATCH /applications/{id}`,
`POST /applications/{id}/requirements/import`, `POST /applications/{id}/requirements`,
`PATCH /requirements/{id}`, `POST /requirements/{id}/advance`, `POST /requirements/{id}/todo`,
`GET /applications/{id}/gaps` (stored open gaps for now), `PATCH /gaps/{id}`); tools
`add_application`, `import_requirements`, `update_requirement`, `application_status`; the
"APPLICATIONS" prompt section; `applications` and `application_requirements` deletable.
Tests: `tests/test_phase6a_applications.py` (acceptance tests 1, 2, 3, 7).

Deferred to S21: `review_gaps`, the briefing items, the `application_watch` agent and the
`application_at_risk` alert.

Decisions where the spec was silent:
- The guard against invented dates is stricter than "the phrase must not be empty": the
  phrase the model gives for a date must actually occur in the pasted text (compared on
  letters and digits only). A date whose phrase is not there is dropped and counted in
  `dates_dropped`. The same applies to the application's deadline and decision date.
- A requirement date Bandile set himself (`due_source: user`) is not overwritten by a later
  import, by the same rule as the application's deadline.
- The first successful import moves an application from `researching` to `preparing`.
- `advance` from `not_required` goes to `missing`.
- `make_todo` with no requirement date and no application deadline creates an undated task.
- Tool results always carry `deadline`, `deadline_source` and a `deadline_note` sentence.
  The note is what the model reads when the deadline is unknown (empty values are stripped
  from what the model sees), and it says not to state a date from general knowledge.
- `add_application` with a name that matches an existing application updates it.
- Requirement details are embedded as `source_type="application"`; gap text will use
  `"application_gap"` (two tables cannot share one `(type, id)` key).

### S21 — 6A.2 gap review, briefing, deadline watch, alert (done)

Shipped: `applications.review_gaps` (cached, one `application_gaps` call, supersedes the
previous review) behind `GET /applications/{id}/gaps?refresh=`; tool `application_gaps`
and its prompt bullet; briefing `_application_items` and `counts.applications_at_risk` in
`attention_count`; agent `application_watch` (daily at 05:45); alert `application_at_risk`
with `_check_application_at_risk`; the `/applications` endpoint shapes in the hand-off doc.
Tests: acceptance tests 4–6 (and the briefing clause of test 3) in
`tests/test_phase6a_applications.py`.

Deferred: nothing from §6A. The Applications page is the next session.

Decisions where the spec was silent:
- The review's context is assembled with no model call: notes and memories that mention
  the application are found by keyword, not embeddings, so a review is exactly one call
  and the agent's cap of two calls a run means two reviews.
- Gap text is embedded for search only outside an agent run (inside one, an embedding
  would use up the run's call cap).
- With no AI key, `GET …/gaps` returns the stored gaps marked `stale` when the requirements
  have changed; `refresh=1` answers 503.
- `application_watch` treats a mark as reached when the deadline is at or inside it and no
  event exists yet for that application, mark and deadline. A missed day therefore does
  not skip a mark, and moving a deadline starts its marks again. The marks come from
  `notification_settings.deadline_days_json` once S16 adds it, else 30, 14, 7, 3, 1.
- It only reviews applications that have at least one requirement.
- Briefing: an application with no requirements yet says "No requirements listed yet";
  a passed deadline scores 90; a requirement item is capped at 80 and reads "Work the
  task" once it has a linked task. Applications past `preparing` produce no items.
- `applications_at_risk` counts applications still in `researching` or `preparing`.

### Applications UI (split from S22) (done)

Shipped (`frontend/index.html`): an Applications item in the rail under Annual Targets and
page `p-applications`. One card per application from `GET /applications` and
`GET /applications/{id}`: status badge and an "At risk" badge; a Deadline tile with the
countdown, the date and where the date came from ("you gave this date" / "from the
requirements you pasted"), or "Unknown" with a prompt to supply it; a Ready tile with a
progress bar; requirement rows with a status dot, kind, details and due date, where a tap
on the row advances it (`POST /requirements/{id}/advance`) and Make it a task creates its
dated task (`POST /requirements/{id}/todo`); open gaps with severity, evidence, the
suggested next step and Addressed / Accept risk (`PATCH /gaps/{id}`); and Refresh review
(`GET …/gaps?refresh=1`, with a loading state and a clear message when there is no AI
key). Every tap's change goes to the dock's change strip with Undo. The home screen's
Applications tile is now a live link.

Also: `_bizTap` takes the page's own renderer, so other pages reuse the one-tap helper;
`.kpi-sub.wrap` lets a KPI's caption wrap. `tests/e2e/screens.js` seeds two applications
(one with a deadline, requirements in every state, a linked task and two gaps; one empty)
and checks the page at all four sizes; `tests/e2e/smoke.js` gained an eighth flow (an
application due today with a missing requirement shows "At risk"; a tap moves the
requirement to in progress; Make it a task creates the task and leaves the status alone).

Checked in headless Chrome at the four sizes. What the screenshots showed: the Wharton card
reads "12 days · 24 Nov 2026 · you gave this date" with the red At risk badge and 2 of 4
ready; the four requirement rows carry the red, amber, blue and green dots of the legend
above them; the row with a task shows "Has a task" in place of the button; on the phone the
two tiles sit side by side and each row wraps to title, status and button. First pass on
the phone truncated the "Requirements ready" label and the deadline's caption; the label
is now "Ready" and the caption wraps.

Decisions where the spec was silent:
- A requirement row is tappable as a whole (as on the Syllabus page) rather than carrying a
  separate Advance button; Make it a task is the row's one button, and it is replaced by
  "Has a task" once a task is linked.
- The countdown tile says where the deadline came from, because the page must never make a
  model-supplied date look like a fact.

### S9 — 5D.1 brand schema, YouTube adapter, export importer, sync (done)

Shipped: `SocialAccount`, `ContentPost`, `ContentMetric`, `AccountMetric`, `ContentIdea`,
`BrandInsight` (m017, unique and plain indexes, RLS); `brand/adapters.py`
(`PlatformAdapter`, `YouTubeAdapter`, `ExportAdapter`, `LinkedInExportAdapter`);
`brand/sync.py` (`sync`, `import_export`); `brand/__init__.py` with `GET/POST
/brand/accounts`, `POST /brand/accounts/{id}/sync`, `DELETE /brand/accounts/{id}`,
`POST /brand/import`; the `brand_sync` agent now has a module to run; `YOUTUBE_API_KEY` in
`CLAUDE.md`. Tests: `tests/test_phase5d_brand.py` (acceptance tests 1–4), fixture
`tests/fixtures/linkedin_content_sample.csv`.

Deferred to S10: performance analytics, the weekly insight, ideas and the calendar, the
three tools, the briefing items.

Not built: `XExportAdapter`. The spec says to build it only if X's analytics export can be
verified as available, and that cannot be checked from here. `POST /brand/import` answers
400 for `platform=x`.

Decisions where the spec was silent:
- A metric the platform does not supply is stored as NULL: YouTube gives no shares, saves,
  impressions or watch time through the public API, and a channel that hides its subscriber
  count has `followers: null`. A post with no numbers at all gets no snapshot row.
- LinkedIn's own "Engagement rate" column is not stored; the rate is always computed from
  the counts so every platform is measured the same way (S10).
- The export's header row is the first row naming at least two known columns, so the title
  lines LinkedIn puts above the table are skipped.
- Connecting the same YouTube channel twice returns the existing account (reconnected).
- `DELETE /brand/accounts/{id}` is change-logged, so a disconnect can be undone.
- A sync that fails leaves stored posts and numbers alone and puts the adapter's message in
  `last_error`; the next good sync clears it.

### S10 — 5D.2 performance, insight, ideas and calendar, tools, briefing (done)

Shipped: `brand/analytics.py` (`performance`, `insight`, `progress`, `posts_last_30d`);
endpoints `GET /brand/performance`, `GET /brand/posts`, `PATCH /brand/posts/{id}`,
`GET/POST /brand/ideas`, `PATCH /brand/ideas/{id}`, `POST /brand/ideas/{id}/advance`,
`GET /brand/calendar`, `GET /brand/insight`; tools `brand_performance`, `add_content_idea`,
`mark_posted`; the "PERSONAL BRAND" prompt section; briefing `_content_items`,
`progress.brand` and `brand_target_posts_30d`; `content_ideas` deletable; all `/brand`
endpoint shapes in the hand-off doc. Tests: acceptance tests 5–9 in
`tests/test_phase5d_brand.py`.

Deferred: nothing from §5D except the X importer (see S9). The Brand tab is the next
session.

Decisions where the spec was silent:
- `engagement` sums only the components the platform reported; with none reported it is
  null. `engagement_rate` is null unless both an engagement and a reach figure (views, else
  impressions) were measured. Top posts leave out posts with neither a rate nor a view
  count.
- The weekly insight is not generated when there is no key or no post in the window; it
  returns `text: null`. `GET /brand/performance` only ever shows the cached text.
- `platform` omitted means all platforms and is cached under the key `all`.
- The calendar places a post on its local day (`FOUNDER_OS_TZ`), not its UTC day; a test
  run just after local midnight caught the difference. It also returns the unscheduled
  backlog (ideas and drafts with no date), which the Brand tab needs.
- A date on an idea schedules it (`idea` or `drafted` → `scheduled`); `score` is clamped to
  1–5.
- `mark_posted` links the idea and the post both ways when the URL matches a stored post.
- The tool result carries a sentence telling the model that absent values were not
  measured and must not be estimated (empty values are stripped from what the model sees).
- Briefing: an account in `error` shows the error item and not the cadence-gap item.

### Brand UI (split from S12) (done)

Shipped (`frontend/index.html`): the Brand tab of the Business page.
- The cached weekly insight above everything, when there is one.
- Accounts from `GET /brand/accounts`: a tile per account with followers (a dash when the
  platform did not report them), the 30-day change when both readings exist, posts stored,
  when it was last synced or imported, a Connected or Error badge (the error tile shows
  `last_error`), and Sync (`POST /brand/accounts/{id}/sync`) on API accounts. With no
  YouTube account there is a "Not connected" tile with one handle field and Connect
  (`POST /brand/accounts`). Below: a platform select and Import CSV (`POST /brand/import`).
- "What is working" from `GET /brand/performance`: posts a week, days since the last post,
  posts this month against the target, top posts with reach and engagement rate, and
  medians by format. Every unmeasured value is a dash, never 0.
- "Next two weeks" from `GET /brand/calendar`: today and any day with something on it, each
  idea with one action (Mark drafted, Schedule, Mark posted →
  `POST /brand/ideas/{id}/advance`), then the unscheduled backlog. When scheduling needs a
  date (409) the button becomes a date picker and a Schedule button that sends
  `PATCH /brand/ideas/{id}`.
- With no account at all: "No account is connected, so there are no numbers to show.
  Nothing here is ever estimated."
- The home screen's Content tile is now a live link to this tab. All four Business tabs
  and all six home tiles are live.
- `tests/e2e/screens.js` imports the LinkedIn fixture, adds a YouTube account in the error
  state, an insight and ideas, and checks the tab at the four sizes; `tests/e2e/smoke.js`
  gained a ninth flow (the not-connected state; Advance moves an idea to drafted; the
  date picker schedules it).

Checked in headless Chrome at the four sizes, plus full-height captures at 1280 and 390
wide. What they showed: LinkedIn reads "— · Followers not reported · 6 posts" with
Connected and From export; the YouTube tile shows the handle field (or, in the error
state, "YOUTUBE_API_KEY not set" in red with Sync); top posts list impressions and rates
from 4.0 % down to 1.7 %; "By format" reads "Post · 6 · — median views · 2.9%" because
LinkedIn reports impressions, not views; the calendar shows today lit with its idea and
"Mark posted". On the phone the two account tiles sit side by side and each list row wraps.
First pass showed three faults, all fixed: platform names read "Linkedin" and "Youtube";
fourteen day-rows of dashes drowned the two scheduled ideas (now only today and days with
something are listed, with a closing line); and the idea buttons read like statuses
("Posted") rather than actions.

A fixture fault the screenshot exposed: one row of
`tests/fixtures/linkedin_content_sample.csv` had an unquoted "2,105", which the CSV reader
split into 2 impressions and 105 reactions and the page showed as a 6300 % engagement
rate. The cell is now quoted.

Decisions where the spec was silent:
- After a 409 the date picker sends one `PATCH scheduled_for`, which schedules the idea;
  the spec's "then retry the advance" would move it on a second step, to posted.
- There is no Disconnect button on the tab (the session's task does not list one; the
  endpoint exists and is change-logged).
- Only LinkedIn is offered in the import select, because it is the only importer built.

### S15 — Phase 6C.1: integrations table, redaction, Telegram pairing

Shipped: `integrations`, `calendar_events`, `email_messages` and `outreach_messages.replied_at`
(migration `m021`, RLS on the three new tables); `backend/integrations.py` (`get_or_create`,
`redact`, `set_error`, `serialize`, `listing`, `GET /integrations`); `backend/integrations_telegram.py`
(`pair_code`, `is_paired`, `chat_id`, `pair`, `unpair`, `POST /integrations/telegram/set-webhook`,
`POST /integrations/telegram/unpair`); 22 tests in `tests/test_phase6c_integrations.py`.

Decisions where the spec was silent:
- `redact()` also covers `TELEGRAM_WEBHOOK_SECRET`, `OPENAI_API_KEY`, `FOUNDER_OS_SECRET` and
  `FOUNDER_OS_PASSWORD`, beyond the five the spec lists. Values shorter than six characters are
  not treated as secrets (they would blank ordinary words).
- `set-webhook` answers 503 when `TELEGRAM_WEBHOOK_SECRET` is unset as well, not only the bot
  token and the API URL: a webhook registered without a secret would accept anyone's updates.
- The paired chat id lives in the telegram row's `config_json` (it is not a credential).
- `GET /integrations` computes each status from the row and the environment: Telegram and email
  show "pending" when their env vars are set but nothing is paired or synced yet; YouTube reads
  the Brand tab's `social_accounts` row.
- (Changed in the review round.) The pairing code was derived from the server secret, which
  without `FOUNDER_OS_SECRET` is derived from the password, and it never changed. It is now six
  random characters kept on the telegram row, spent when used: pairing and Disconnect both
  discard it. S17 limits wrong attempts per chat.

VERIFY: Telegram `setWebhook` parameter names (`url`, `secret_token`, `allowed_updates`).

### S16 — Phase 6D.1: notifications queue, Telegram channel, couriers

Shipped: `notifications` and `notification_settings` (migration `m022`, which creates settings
row 1; RLS on both); `backend/notify.py` (`settings`, `update_settings`, `is_quiet`,
`quiet_ends`, `sent_today`, `queue`, `dispatch`, `split_message`, `TelegramChannel`,
`render_briefing`, `render_evening`, `suppressed_overnight`, `briefing_text`, `delivery`,
`send_now`, `send_test`, `brief_now`; `GET`/`PATCH /notifications/settings`, `GET /notifications`,
`POST /notifications/test`, `/notifications/brief-now`, `/notifications/{id}/cancel`); agents
`briefing_courier`, `alert_courier`, `reminder_courier`; tools `set_reminder` and
`notification_settings`; `business_common.parse_when`; prompt section "REMINDERS AND
NOTIFICATIONS"; `build_briefing` returns `delivery`; 37 tests in `tests/test_phase6d_notify.py`
(spec tests 1, 2, 3, 6, 7, 8, 10, 11).

Decisions where the spec was silent:
- Reminders are sent at the moment asked, inside quiet hours and past the daily cap, because
  Bandile chose the time himself. A mute (`muted_until`) still holds them. Cost if wrong: a
  reminder set for 23:30 buzzes at 23:30.
- `test` messages are treated like replies: they ignore quiet hours and the cap and do not
  count towards it. With notifications switched off (`enabled` false) only replies and test
  messages still go out.
- Not paired gives `suppressed(not_paired)`; paired but the channel switched off or
  `TELEGRAM_BOT_TOKEN` unset gives `suppressed(channel_disabled)`.
- The briefing and alert couriers skip their run ("Telegram is not paired") instead of
  queueing rows that could only be suppressed. Events they receive in that state are consumed
  and not notified; overdue payments are re-emitted daily, so they notify once paired.
- `dispatch` takes `only=<id>`: Send test, Brief now and (in S17) replies dispatch just their
  own row, so a forced send never drags other queued rows through quiet hours. A process lock
  stops two dispatches sending the same row.
- A failed send keeps the row queued with `attempt N/3: …` in `error`; the third failure marks
  it failed.
- Briefing text: ATTENTION lists items scoring 40 or more that are not non-negotiables, exams,
  calendar events, syllabus priorities, opportunities or content, and its number is how many
  such items there are (six are shown). TODAY lists calendar events (from S18) and the
  non-negotiables line. OVERNIGHT lists what the cap or quiet hours suppressed in the last 24
  hours, five at most.
- The evening review lists tasks that are overdue or due today and still open, then asks
  "What did you get done? Reply here."
- `parse_when`: a bare hour from 1 to 12 with no am/pm means the next time the clock shows it
  today ("at 5" said at 14:00 is 17:00); on another day 1–6 means the afternoon; a day with no
  time means 09:00; a time that has passed means tomorrow.
- `schedule.due` allows 30 seconds of slack on interval schedules, so a timer firing a few
  milliseconds early does not push an every-tick agent to the following tick.
- The couriers pass the local clock to `build_briefing`, so the Telegram briefing is for the
  local day.

Limits to know:
- Reminders and briefings go out on the tick, so up to 15 minutes late, and not at all while
  the instance is asleep and the external schedule is off.

VERIFY: Telegram `sendMessage` parameter names and the 4,096-character limit (messages are
split at 4,000).

### S17 — Phase 6D.2: two-way Telegram into the orchestrator

Shipped: `orchestrator._turn_events` (the one implementation of a turn), `orchestrator_stream`
re-built on it (the existing orchestrator tests pass unchanged), `orchestrator.run_turn`;
`backend/telegram_inbound.py` (`POST /integrations/telegram/webhook`, pairing, `/brief`,
`/undo`, `/yes`, `/no`, `/mute`, `/start`, `/help`, plain text → `run_turn`); the webhook is
exempt from the session check and verified by the secret header; 11 more tests in
`tests/test_phase6d_notify.py` (spec tests 4, 5 and 9); REBUILD_HANDOFF §2 sections for
Integrations and Notifications.

Decisions where the spec was silent:
- The webhook answers `200 {}` at once and runs the update after the response, in its own
  session, one update at a time (a process lock). A slow assistant turn therefore cannot make
  Telegram retry. Cost if wrong: an update is lost if the process dies mid-turn.
- Repeated updates are dropped by an in-memory list of the last 200 `update_id`s. It is lost
  on a restart; a `telegram_updates` table replaces it if Telegram's retries prove common.
- Only private chats are served. Nothing is ever said to a chat that is not paired, except the
  confirmation when the right code arrives. `/start <code>` pairs too (Telegram's Start button
  sends `/start`). Five wrong codes from one chat lock that chat out for an hour (in memory).
- `/yes` and `/no` act on the newest pending action whose `thread_id` is `telegram:<chat>`;
  the inbound turn stamps that on the actions it proposed. A proposal made on the dock is not
  Telegram's to confirm. `/yes` counts as Bandile's own approval (`human=True`), so it may
  confirm an outreach send.
- `/yes`, `/no` and `/undo` are saved in the conversation thread with their outcome, so the
  assistant knows they happened and `/undo` can reverse a delete confirmed by `/yes`.
- `/mute` takes hours or minutes (`2h`, `30m`), up to 72 hours; `/mute off` ends it. Replies
  still come through a mute.
- `run_turn` also returns `batch_id`. Its `context` argument is ignored, as on
  `POST /orchestrator`.

Limits to know:
- If a Telegram turn asks to delete a row that already has an open proposal from the dock,
  the reply has no `/yes` hint and `/yes` does not find it; confirm it in the app.
- Voice notes and photos get "Text only for now."

VERIFY: the header name `X-Telegram-Bot-Api-Secret-Token`; Telegram's webhook timeout and
retry behaviour.

### S18 — Phase 6C.2: Google Calendar

Shipped: `backend/integrations_google.py` (`start_url`, `new_state`, `check_state`, `exchange`,
`_token` with refresh, `sync`, `create_event`, `delete_remote`, `today`, `week`, `disconnect`;
`GET /integrations/google/start`, `GET /integrations/google/callback`, `POST
/integrations/google/sync`, `POST /integrations/google/disconnect`, `GET /calendar/today`,
`GET /calendar/week`, `POST /calendar/events`); the callback is exempt from the session check;
`change_log._after_undo_hook` deletes the remote event when a created calendar event is undone;
agent `calendar_sync` (every 30 minutes); tools `calendar_today` and `calendar_add_event`;
prompt section "CALENDAR AND EMAIL" (calendar bullets); the briefing's `event` items and its
`calendar`, `today_events` and `integrations` keys; alert rule `integration_error`; 16 more
tests in `tests/test_phase6c_integrations.py` (spec tests 1–4, 8 and 9).

Decisions where the spec was silent, and one correction:
- The spec redirects the browser to `{FOUNDER_OS_FRONTEND_URL}/#settings?…`. There is one host
  now, so the callback redirects to the relative path `/app/#settings?connected=google` (or
  `?error=google_denied | google_state | google_exchange`). No `FOUNDER_OS_FRONTEND_URL`
  variable exists.
- The state is `nonce.timestamp.signature`, good for ten minutes and for one use; the nonce is
  kept in the row's `config_json.pending_state` and cleared when it is used.
- A successful callback runs one sync straight away, so the calendar is not empty until the
  next tick.
- A 401 from the Calendar API marks the integration in error with "Reconnect Google Calendar",
  and so does `invalid_grant` on a refresh. (Corrected in the review round: a network failure
  or a 5xx used to mark it in error as well, after which the agent stopped syncing. Those are
  now left alone and retried on the next sync.)
- Cancelled events are not cached. An event that reappears upstream is revived.
- `sync` emits `calendar.changed` itself, so it does so from the endpoint as well as the agent.
- Undo calls Google's delete only for a change-log entry that created the event; whatever
  Google answers, the cached row is removed.
- The briefing's calendar section uses the user's wall clock: the caller's `now` when given,
  else the real time in `FOUNDER_OS_TZ`. An event under way scores 70.
- `GET /calendar/week` takes `?from=YYYY-MM-DD`. `POST /calendar/events` takes `start` as an
  ISO local date-time or as text with a clock time.
- `parse_when` gained `default_hour`; with `None` a day with no clock time is refused, which
  is how `calendar_add_event` answers "need a time".
- The ICS read-only fallback (`GOOGLE_CALENDAR_ICS_URL`) is not built; it is only needed if
  Google blocks the unverified app (see VERIFY).

External setup — Google Calendar (not done; nothing here was tested against Google):
1. In the Google Cloud console create a project (any name).
2. APIs & Services → Library → enable "Google Calendar API".
3. APIs & Services → OAuth consent screen: user type External; fill the app name and your
   email; add the scope `https://www.googleapis.com/auth/calendar.events`; then publish the app
   ("In production"). Leaving it in Testing makes the sign-in expire after seven days.
4. APIs & Services → Credentials → Create credentials → OAuth client ID → Web application. Add
   the authorised redirect URI `https://<your Render host>/integrations/google/callback`.
5. On Render set `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` and `FOUNDER_OS_API_URL`
   (`https://<your Render host>`, no trailing slash). `GOOGLE_CALENDAR_ID` is optional
   (default `primary`).
6. In the app: Settings → Integrations → Google Calendar → Connect, and accept Google's
   "unverified app" warning (Advanced → continue).

VERIFY (6C): that an unverified app in production with the `calendar.events` scope lets the
owner through the warning screen; the token endpoint's fields (`access_token`,
`refresh_token`, `expires_in`, the `invalid_grant` error); the events fields
(`start.dateTime`, `start.date`, `end`, `summary`, `status`, `htmlLink`, `updated`,
`nextPageToken`); the revoke endpoint.

### S19 — Phase 6C.3: email over IMAP and SMTP

Shipped: `backend/integrations_email.py` (`configured`, `send`, `fetch`, `list_messages`;
`POST /integrations/email/sync`, `POST /integrations/email/test`, `GET /email/messages`); the
outreach `EmailChannel` now sends through it (it was already written against this interface);
agent `email_sync` (every 30 minutes); tool `email_lookup`; the email bullets of the prompt
section "CALENDAR AND EMAIL"; 9 more tests in `tests/test_phase6c_integrations.py` (spec tests
5, 6 and the email half of 7) with fake IMAP and SMTP classes; the 5B email test now clears the
email variables itself.

Decisions where the spec was silent:
- Clients have no email column, so "a client email" means the email of one of the client's
  contacts. A sender is known when the address is a lead's or a contact's. (Tightened in the
  review round: a known address in the From line is not enough. A message only moves a lead
  when the mail server's own `Authentication-Results` header says DMARC passed, or an aligned
  DKIM or SPF did. Anything else is stored with `auth_result` fail or none, changes nothing,
  and is offered as a "Mark replied" suggestion.)
- `CONTENT-TYPE` and `CONTENT-TRANSFER-ENCODING` are fetched with the listed headers, so the
  snippet can be decoded (HTML, multipart, base64, quoted-printable). Quoted lines and
  everything after "On … wrote:" are dropped from the snippet. A body that cannot be decoded
  from its first 2,000 bytes gives an empty snippet.
- `X-GM-THRID` is only requested when the IMAP host is Gmail; other servers reject it.
- The inbox is opened read-only, so syncing never marks mail as read. (Changed in the review
  round: a sync used to read the newest 200 messages of a fixed three-day window, so mail
  older than three days at the next sync was never seen. It now reads from the last
  successful sync less a day, at most 200 messages a call, oldest first, and remembers the
  UID it reached, so a backlog is worked through over the following syncs.)
- `email_messages` rows and `outreach_messages.replied_at` are not change-logged (they are
  facts from the mailbox). The communication, the lead's move to `replied` and the closed
  follow-up are logged, in a batch with source `email_sync`.
- A message with no `Message-ID` gets a stable one made from its sender, date and subject.
- `send` strips line breaks from the subject and refuses anything that is not one address.
  An SMTP sign-in failure marks the integration in error; other send failures only fail that
  message.
- `POST /integrations/email/test` sends to Bandile's own address and stores nothing.
- `email_lookup` matches a lead, a client or a contact by name and returns the last five
  snippets; it says "Email is not connected" when the variables are unset.

External setup — Gmail (not done; nothing here was tested against Gmail):
1. On the Google account turn on 2-Step Verification (Google Account → Security).
2. Create an App Password (Google Account → Security → 2-Step Verification → App passwords),
   name it "Founder OS". Copy the 16 characters.
3. In Gmail → Settings → Forwarding and POP/IMAP, make sure IMAP is enabled (if the toggle is
   still shown).
4. On Render set `EMAIL_ADDRESS` (the Gmail address) and `EMAIL_APP_PASSWORD` (the 16
   characters). Optional: `EMAIL_FROM_NAME`, `EMAIL_IMAP_HOST`, `EMAIL_SMTP_HOST`,
   `EMAIL_SMTP_PORT`.
5. In the app: Settings → Integrations → Email → Sync. Status should turn to connected.

VERIFY (6C): that App Passwords are still offered on the account and IMAP is enabled; the
`X-GM-THRID` fetch item; the IMAP fetch item syntax `BODY.PEEK[TEXT]<0.2000>`; `UID SEARCH
SINCE` and `UID FETCH` (added in the review round; standard IMAP, not checked against Gmail).

### S22 (rest) — Phase 6 UI: Settings cards, Command Centre indicators, docs

(The Agents page and the Applications page were built in their own sessions; this is the
remainder.)

Shipped: Settings → Integrations card (`GET /integrations`: status badge, account, last sync,
a button per action — Connect, Sync, Disconnect behind a confirm, Pair, Set webhook — and a
"Pair your phone" row with the code and the bot link while Telegram is configured but not
paired); Settings → Notifications card (the on/off toggle, morning time, evening time, quiet
hours, least serious alert, daily cap, Send test, Brief now, the last ten notifications);
returning from Google lands on Settings with a message; Command Centre: one line under the
greeting with today's timed events, "Sent to Telegram at HH:MM" and a link to Settings when an
integration is in error or Telegram is not paired; the Design Guide shows the new `.code`
chip; docs (ROADMAP, README, CLAUDE.md, REBUILD_HANDOFF migrations and environment variables,
the Netlify mentions in ADR-0002 and both phase specs).

Decisions where the spec was silent:
- The home indicators are one line that never wraps (longer content is cut with an ellipsis
  and shown in full on hover), so the home grid keeps its proportions.
- Every notification control saves on change (one request each) and shows the change with
  Undo. The daily cap is a select (4, 6, 8, 12, 16, 24 and the current value).
- The frontend names that the build order used and that no longer exist: `renderChangesStrip`
  is reached through `showChanges`; list rows are `.row`, not `.list-row`.
- `GET /integrations` shows the YouTube row from the Brand tab's account, including that
  account's own error and last sync (one small backend change in this session, with a test).
- `tests/e2e/screens.js` seeds a paired phone, a connected calendar with events today and
  three notifications, so the contact sheet shows the populated states.

Checked in a browser (headless Chrome, 1280×720 and 390×844): Settings with a bot configured
but not paired (pair row, code, Open bot), Settings with the calendar connected and Telegram
paired, and the home screen with two events, an all-day event and a sent briefing. The layout
checks pass at all four sizes.

Limits to know:
- Nothing in the UI was exercised against the real Telegram, Google or Gmail services.

## Review round (12 November 2026)

One commit per fix, each with a test written first. Items 5 to 14 came from a same-session
review and were unverified: each was reproduced by a failing test (or by an existing test
that asserted the faulty behaviour) before it was changed. All ten reproduced.

### Part 1 — fixes

| # | What was wrong | What changed |
|---|---|---|
| 1 | m016 set every client that predates `clients.currency` to USD | Old clients get `DEFAULT_CURRENCY` (ZMW unless the variable says USD), as new ones do. Payments backfilled from revenue keep each revenue row's own currency |
| 2 | A local number (`0977…`) gave `wa.me/0977…`, which WhatsApp rejects | `outreach_channels.whatsapp_digits`: `0977…`, `977…`, `+260977…`, `260977…` all give `260977…`. `DEFAULT_COUNTRY_CODE` (260). The stored number is not changed |
| 3 | "Today" was the server's day (UTC on Render): from 00:00 to 02:00 in Lusaka everything was yesterday's | `user_clock.py` (`today()`, `now()` in `FOUNDER_OS_TZ`) replaces every `date.today()` / `datetime.now()` in app code; a guard test fails if one comes back. Tested at 00:30 Lusaka time |
| 4 | The conversation-history test failed in the first five minutes after 00:00 UTC | The fixture seeded "today's" messages five minutes back. The class runs on a fixed clock at 00:02 UTC and seeds seconds back |
| 5 | A timeout or 5xx from Google put the calendar in error; the sync agent skips an integration in error, so syncing stopped until a manual Sync | Only `invalid_grant`, a missing refresh token or a 401 mean error. Anything else leaves status and tokens alone and is retried on the next sync |
| 6 | A multi-day event was only listed on the day it starts | The day view takes every event that overlaps the local day ("until HH:MM" on its last day, "all day" in between) |
| 7 | The m016 payment backfill ran on every startup, claimed revenue rows logged later, and `crm.reconcile` then left the invoice open | The backfill claims a key in `migration_marks` (`_helpers.claim_once`) and runs once per database |
| 8 | `/yes` on a WhatsApp send answered "Done" with no link, so the message was marked sent and never left | The reply carries the `wa.me` link to tap |
| 9 | Undo turned a sent message back into a draft | Changes recorded for an email or WhatsApp send have `change_log.undoable = FALSE` (m023). Undoing one is 409; undoing a batch reverses the rest and reports the send as kept. Telegram, the assistant and the change strip say so |
| 10 | Email sync always read the last three days, newest 200 messages | It reads from the last successful sync less a day, 200 messages a call, oldest first, and remembers the UID it reached |
| 11 | A message was tied to a lead by its From line alone and moved the lead to replied | The mail server's own `Authentication-Results` must show DMARC, or an aligned DKIM or SPF, passing. Otherwise the message is stored (`auth_result`), nothing changes, and a "Mark replied" suggestion is filed |
| 12 | Overlapping ticks and Run now could start the same agent several times (four in the test) | A run claims its agent with one atomic UPDATE on `agents.claimed_at` and releases it at the end; a tick looks again once it holds the claim |
| 13 | An event with two subscribers was consumed when the first ran | `agent_event_deliveries`: each subscriber is handed an event once, and the event is consumed when every enabled subscriber has had it |
| 14 | `notify.queue` rolled back the caller's whole session on a duplicate key | The insert runs in a savepoint. Same change in `integrations.get_or_create`, `notify.settings` and `runner.agent_row` |

Decisions made in this round:
- `DEFAULT_CURRENCY` accepts `ZMW` or `USD`; anything else reads as `ZMW`.
- WhatsApp: a number with no `+`, no leading zero and ten digits or fewer is taken as local.
  An eleven-digit number with no `+` is assumed to carry its country code already.
- The test suite and both e2e scripts run the app in the machine's own time zone, so fixtures
  built from the machine's date agree with the app at any hour. The suite was run with the
  machine set to UTC-11, UTC+2 and UTC+14.
- Google: a refusal that is neither transient nor about the grant (a wrong calendar id, a
  wrong client secret) also leaves the status alone, as instructed. It shows in the sync
  result and the server log, not as an error badge.
- Undo: a manual channel (LinkedIn, call) and a failed send stay undoable, because nothing
  left the app. For a real send the follow-up task and the communication are kept as well.
- Email: with a non-Gmail IMAP host no sender counts as verified until `EMAIL_AUTHSERV_ID`
  names the server whose header is trusted. An unverified message from a client's contact
  is stored and nothing else (no communication is logged).
- Agents: a claim older than 30 minutes is treated as left by a dead process.
- `migration_marks`: a database that ran an earlier build of this branch has no mark yet, so
  its next startup runs the payment backfill one last time. Production has not run the branch.

New environment variables: `DEFAULT_CURRENCY`, `DEFAULT_COUNTRY_CODE`, `EMAIL_AUTHSERV_ID`.
New migration: `m023` (`agent_event_deliveries`, `change_log.undoable`,
`email_messages.auth_result`, `agents.claimed_at`); `migration_marks` is created by m016.

After Part 1: 762 backend tests, the startup chain twice on a fresh SQLite file, and the
browser smoke tests, all green.

### Part 2 — PostgreSQL 16

PostgreSQL 16.15 (the `postgresql-16` package was already in the container; the cluster was
started and a non-superuser role created). Three throwaway databases.

a) **Fresh database.** The startup chain ran twice with no error; `Schema OK` both times.

b) **Upgrade from main.** A database was built by main's own startup chain, RLS was turned on
   as in production (m008), and it was seeded with 5 clients, 10 revenue rows (ZMW, USD, one
   lower-case `zmw`, one with no currency, two with no client), 4 todos and a chat history
   with tool calls. Then the branch's startup chain ran on it. Result:
   - all 5 clients are `ZMW`; none became USD;
   - 8 payments for the 8 revenue rows that name a client, each `paid`, titled `Imported`,
     with the revenue row's amount, client, day and currency (6 ZMW-or-USD as stored, the
     lower-case one as `ZMW`, the one with no currency as `USD`, which is what m012 on main
     already makes of it); the two revenue rows with no client got none;
   - one row in `migration_marks`; a second startup added nothing;
   - the clients' original columns, and every revenue, todo and chat row, are unchanged;
   - 78 tables, 31 of them new on the branch, row-level security on every one;
   - the app answers on the upgraded data (`/clients`, `/crm/summary`, `/briefing/today`,
     `/conversation/history`, `/agents`, `/integrations`, `/portfolio` and more);
   - an invoice plus a later revenue row, then a restart: the backfill did not touch the new
     row, and reconciliation marked the invoice paid and linked it.
   The check is repeatable: `backend/tests/postgres/` (`seed_from_main.py`,
   `check_upgrade.py`, `README.md`).

c) **The suite on Postgres.** 762 passed with `TEST_DATABASE_URL` (about ten minutes: every
   test drops and recreates 78 tables).

What failed and was fixed: only the test harness. `conftest.py` terminated every other
backend in the test database before each test, including the server's own (an autovacuum
worker), which a role that is not a superuser may not do; 30 tests errored in setup. It now
terminates only this role's own client connections. No product code failed on Postgres.

Noticed, not changed: on a brand-new database the 33 tables that predate the rebuild have no
row-level security until `migrations/m008_enable_rls.py` is run by hand (it is not in the
startup chain, on main either). Production already has it. Every table from m010 onwards
turns it on itself.

After Part 2: 762 tests on SQLite and on PostgreSQL, the startup chain on both, the smoke
tests green.

## External setup — Telegram and YouTube

(The Google Calendar steps are under S18 and the Gmail steps under S19. None of these has
been done, and nothing was tested against the live services.)

Telegram:
1. In Telegram, message @BotFather, send `/newbot`, choose a name and a username. Copy the
   token it gives you.
2. On Render set `TELEGRAM_BOT_TOKEN` (the token), `TELEGRAM_BOT_USERNAME` (the bot's
   username), `TELEGRAM_WEBHOOK_SECRET` (any random string of 32 or more letters and digits)
   and `FOUNDER_OS_API_URL` (`https://<your Render host>`, no trailing slash). Make sure
   `FOUNDER_OS_PASSWORD` is set too, or the Settings page that shows the pairing code is open
   to anyone. The webhook secret must be at least 16 characters; shorter, and the webhook
   refuses every update.
3. In the app: Settings → Integrations → Telegram → Set webhook (once).
4. Tap Open bot, press Start in Telegram, and send the six-character code shown in Settings.
   The bot answers "Paired."
5. Settings → Notifications → Send test.

YouTube:
1. In the Google Cloud console (the same project as the calendar will do), enable "YouTube
   Data API v3" and create an API key (APIs & Services → Credentials → Create credentials →
   API key).
2. On Render set `YOUTUBE_API_KEY`.
3. In the app: Business → Brand, type the channel's handle (`@handle`) and connect, then Sync.

## Environment variables added so far

| Var | Default | Session | Purpose |
|---|---|---|---|
| `RESEARCH_MODEL` | the assistant model | S1 | Model for the market-intelligence roles |
| `JOBS_TOKEN` | unset | S13 | Secret for `POST /jobs/tick`; unset means 503. 32+ random characters |
| `FOUNDER_OS_TZ` | `Africa/Lusaka` | S13 | Zone for agent schedules and notification times |
| `JOBS_TICK_BUDGET` | `80` | S13 | Seconds a tick may spend starting agents |
| `JOBS_IN_PROCESS` | `1` | S13 (used from S14) | `0` turns off the in-process 15-minute tick |
| `FOUNDER_OS_API_URL` | unset | S15 | The backend's public base URL (no trailing slash); Telegram webhook and Google redirect |
| `TELEGRAM_BOT_TOKEN` | unset | S15 | Bot token from @BotFather |
| `TELEGRAM_BOT_USERNAME` | unset | S15 | Bot username, for the pairing link in Settings |
| `TELEGRAM_WEBHOOK_SECRET` | unset | S15 | Random string Telegram sends back with every update |
| `GOOGLE_CLIENT_ID` | unset | S18 | Google OAuth client id for the calendar |
| `GOOGLE_CLIENT_SECRET` | unset | S18 | Google OAuth client secret |
| `GOOGLE_CALENDAR_ID` | `primary` | S18 | Which calendar to read and write |
| `EMAIL_ADDRESS` | unset | S19 | The Gmail address mail is read from and sent as |
| `EMAIL_APP_PASSWORD` | unset | S19 | That account's App Password (a secret) |
| `EMAIL_IMAP_HOST` | `imap.gmail.com` | S19 | IMAP server |
| `EMAIL_SMTP_HOST` | `smtp.gmail.com` | S19 | SMTP server |
| `EMAIL_SMTP_PORT` | `465` | S19 | SMTP port (SSL) |
| `EMAIL_FROM_NAME` | `Bandile Masocha` | S19 | The name on sent mail |
| `YOUTUBE_API_KEY` | unset | S9 | YouTube Data API v3 key for the Brand tab |

## VERIFY items (from the specs)

Collected here as the sessions that carry them are built; each is checked against the live
service after deployment.

- 5A: the web adapter still posts to `https://html.duckduckgo.com/html/` with no key. Not
  checked in this build (tests never reach the network). If it blocks, the adapter returns
  `[]` and a run proceeds on War Room and vault evidence only.
- 5D YouTube: the `forHandle` parameter of `channels.list`; the shape of the `search`
  fallback (`items[0].id.channelId`); `snippet.customUrl` as the handle; that Shorts are
  not flagged by the API (a duration of 60 seconds or less is the heuristic); quota cost
  (about one unit per list call, five calls for 55 videos). Each is marked `# VERIFY` in
  `backend/brand/adapters.py`. Not checked against the live API.
- 5D LinkedIn: that the creator analytics export exists for the account type, and its
  column names (`Post URL`, `Post publish date`, `Post title`, `Impressions`, `Reactions`,
  `Comments`, `Reposts`). The parser accepts the synonyms listed in `LinkedInExportAdapter`.
  Not checked against a real export.
- 5D X: whether an analytics export is available on the account's tier. Not checked; the
  adapter was not built.
- 6B GitHub Actions: minimum schedule interval, delay under load, and the 60-day
  inactivity rule. Moot while the schedule is switched off; check before switching it on.
- 6B Render: the free tier's HTTP request timeout (the tick budget of 80 seconds assumes at
  least 100) and current spin-down behaviour. Not checked.
- 6D Telegram: `sendMessage` and its 4,096-character limit (messages are split at 4,000);
  the `setWebhook` parameters `url`, `secret_token` and `allowed_updates`; the header
  `X-Telegram-Bot-Api-Secret-Token`; how long Telegram waits for the webhook to answer and
  whether it retries (the webhook answers at once and drops repeated `update_id`s). Not
  checked against the live Bot API.
- 6C Google OAuth: that an unverified app "In production" with the `calendar.events` scope
  lets its owner through the warning screen, and that its refresh tokens do not expire as
  they do in Testing; the token endpoint's fields and the `invalid_grant` error; the revoke
  endpoint. Not checked. Fallback if Google blocks it: a read-only sync from the calendar's
  private ICS address, which is not built.
- 6C Google Calendar API v3: `start.dateTime`, `start.date` for all-day events, `end`,
  `summary`, `status`, `htmlLink`, `updated`, `nextPageToken`. Marked `# VERIFY` in
  `backend/integrations_google.py`. Not checked.
- 6C Gmail: that App Passwords are still offered with 2-Step Verification, that IMAP is
  enabled, the `X-GM-THRID` fetch item and the `BODY.PEEK[TEXT]<0.2000>` syntax. Not
  checked.

## Follow-ups noticed

- Brand tab: a Disconnect control behind a confirm dialog (`DELETE /brand/accounts/{id}`
  exists), and a way to drop an idea from the screen.
- `tests/e2e/smoke.js` has a `KEEP=1` option that does not keep the server alive on
  Windows (the child process ends with the script).

- Pipeline tab: add an "Import CSV" button (file picker only) for `POST /leads/import`, and
  a "Qualify" tap on a segment's unqualified leads for `POST /leads/qualify`. Both
  endpoints exist and are tested; neither has a control on screen yet.
- The Clients KPI strip is in dollars because `GET /crm/summary` reports `*_usd`. Bandile
  works in Kwacha; consider adding ZMW totals to the summary and leading with them.

- `tests/test_market_intel.py` uses the deprecated `Query.get()` in three places.
