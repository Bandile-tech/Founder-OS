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
  hours it is marked consumed and `dropped`.
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

## Environment variables added so far

| Var | Default | Session | Purpose |
|---|---|---|---|
| `RESEARCH_MODEL` | the assistant model | S1 | Model for the market-intelligence roles |
| `JOBS_TOKEN` | unset | S13 | Secret for `POST /jobs/tick`; unset means 503. 32+ random characters |
| `FOUNDER_OS_TZ` | `Africa/Lusaka` | S13 | Zone for agent schedules and notification times |
| `JOBS_TICK_BUDGET` | `80` | S13 | Seconds a tick may spend starting agents |
| `JOBS_IN_PROCESS` | `1` | S13 (used from S14) | `0` turns off the in-process 15-minute tick |

## VERIFY items (from the specs)

Collected here as the sessions that carry them are built; each is checked against the live
service after deployment.

- 5A: the web adapter still posts to `https://html.duckduckgo.com/html/` with no key. Not
  checked in this build (tests never reach the network). If it blocks, the adapter returns
  `[]` and a run proceeds on War Room and vault evidence only.

## Follow-ups noticed (not done here)

- Pipeline tab: add an "Import CSV" button (file picker only) for `POST /leads/import`, and
  a "Qualify" tap on a segment's unqualified leads for `POST /leads/qualify`. Both
  endpoints exist and are tested; neither has a control on screen yet.
- The Clients KPI strip is in dollars because `GET /crm/summary` reports `*_usd`. Bandile
  works in Kwacha; consider adding ZMW totals to the summary and leading with them.

- WhatsApp links for local numbers: add a default country code (for example an env var
  `WHATSAPP_DEFAULT_COUNTRY_CODE=260`) applied only when building the `wa.me` link for a
  number that starts with a single `0`. Today those links do not open a chat.

- `tests/test_market_intel.py` uses the deprecated `Query.get()` in three places.
