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

- `tests/test_market_intel.py` uses the deprecated `Query.get()` in three places.
