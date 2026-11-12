# Founder OS — Rebuild hand-off (Stages 1–4)

Branch: `claude/rebuild-spec-review-vfrg4o`. One commit per stage. Suite: 391 tests, all
passing with **no `OPENAI_API_KEY` set**, under Python 3.12 with the pinned requirements.

The spec's "done when" for each stage holds. Stage 5 (real-time voice) follows in a
separate commit and has its own addendum at the end.

---

## 1. What was built, per stage

### Stage 1 — reliable assistant
- **`llm.py`** is the single gateway to OpenAI. Lazy client: the app boots, migrations
  run, every non-AI endpoint works with no key. AI endpoints return **503** with a clear
  message. All calls log to `llm_usage`.
- **`ASSISTANT_MODEL`** (default `gpt-5.4-mini`) drives the assistant, parser, summaries
  and briefing. `ORCHESTRATOR_MODEL` still works as a legacy fallback. The hardcoded
  `gpt-5.4` default is gone.
- **Per-tool error handling.** A tool that raises returns `{"error": ...}` to the model;
  the stream finishes, the reply is saved, and the SSE `tool_result` event carries
  `"error": true`.
- **Weekly-review crash fixed** (`RoadmapTask.title` → `task_id`).
- **Server-side conversation.** One thread (`default`) for every device. The browser's
  `session_id` is still accepted and stored on each row, never used to partition history.
  m010 merged all existing rows into the default thread.
- **Tool calls and results persist** (`chat_messages.tool_calls_json`, role `tool` rows with
  `tool_call_id`) and are replayed to the model; tool results are truncated to 1,500 chars on
  replay.
- **History budget.** Last `ASSISTANT_HISTORY_TURNS` (default 20) user turns verbatim, plus
  a rolling summary of older turns stored in `conversation_summaries`. The summary is
  refreshed only once `ASSISTANT_SUMMARY_BATCH` (default 10) messages have fallen out of
  the window, so it is not an LLM call every turn.
- **Long-term memory.** `remember` tool (fact / decision / preference) on `ai_memory`; the
  top 5 relevant memories are injected each turn as a **second** system message, so the main
  prompt and tool definitions stay byte-stable for prompt caching.
- Graceful failures in `/chat`, `/parse`, `/input`, `/proactive-brief`, `/weekly-ai-review`
  and the voice endpoints. A parser that returns invalid JSON raises instead of silently
  returning an empty payload.

### Stage 2 — change log, undo, confirmation, intent
- `change_log` records every write by the assistant, parser or tools with before/after
  snapshots. One assistant turn = one batch (the batch lives on the SQLAlchemy session,
  because Starlette drives SSE generators one step per worker thread and context variables do
  not survive between steps).
- Undo endpoints restore the before-state; a create is removed; a delete is re-inserted with
  its original id. Idempotent. Undoing a KPI snapshot reloads the in-memory KPI state.
- The orchestrator's final SSE event and the saved assistant message carry `changes`.
- **Deletes need confirmation**: `propose_delete` → pending action, deletes nothing;
  `confirm_action` only after an explicit yes; `cancel_action`. One-tap endpoints too.
- **Intent classification** in the parser (`intent`, `confidence` per `todos_add` item).
  Commitments → tasks; ideas → the `Inbox` note; discussion → nothing. Below
  `INTENT_CONFIDENCE_MIN` (default 0.7) → `suggestions`, written only on accept.
- `apply_parse_updates` is now **atomic**: one commit, full rollback on any failure, in-memory
  KPI state updated only after the commit.

### Stage 3 — brain-dump rework, notes, retrieval
- `todos_complete` is applied with fuzzy matching; ambiguity becomes `todo_complete`
  suggestions (one per candidate), no match is reported as `todos_complete_unmatched`.
- **Currency.** `revenue.currency` (USD / ZMW). The parser keeps the stated currency; "Got
  K500" stores 500 ZMW. `/revenue` and the $10K scoreboard convert with `ZMW_PER_USD`.
- **Trading removed** from the parser, the tools and the alert rules. `/trading/*` endpoints
  and tables untouched; no data deleted.
- **`todos.due_date`** (real DATE) added and backfilled in Python from the free-text `due`.
  Both columns are written going forward. Overdue is computed server-side.
- **Notes** (`notes` table + endpoints + tools). Reading progress appends dated entries to the
  book's note (created if missing); Bible readings append to the `Bible` note. Books and the
  Bible log were copied once into notes (idempotent; originals untouched).
- **Meaning-based retrieval.** One shared `embeddings` table (`text-embedding-3-small`, JSON
  vectors, cosine in Python, keyed by content hash so unchanged text is never re-embedded;
  vault re-index just moves pointers). Vault, notes and memory share it. Keyword fallback
  with no key. Vault sync embeds new/changed chunks after indexing.
- New tools: `complete_task`, `notes_search`, `notes_read`, `notes_append`, `notes_create`,
  `get_deadlines`, `set_exam_date`. Prompt: vault / notes / memory are searched whenever
  relevant, not only on command.

### Stage 4 — syllabus and briefing
- `syllabus_points` with four states; CSV importer; grouped GET; one-tap advance; PATCH.
- `update_syllabus` tool; `surface_weakest_topics` (status, staleness, notes, exam proximity).
  The assistant no longer reads `subjects/topics/subtopics` (tables kept; `/subjects/*`
  endpoints untouched for the old UI).
- `exam_dates` keyed on (subject_code, paper) with fallback to `subjects.exam_date`.
- `GET /briefing/today`: deterministic ranked items, progress, alert count, greeting line;
  optional LLM summary cached once per day.
- Alerts: `backtest_stalled` removed; `habit_drift_7d` now checks active `non_negotiables`;
  `zero_mastery_high_weight` replaced by `syllabus_weak_before_exam`. Alerts are inside the
  briefing (`alerts`, `alert_count`, and `category: "alert"` items).

### Not finished / deliberately left
- The market-intelligence agent still hardcodes `gpt-4o-mini` (agent management is out of
  scope). It uses the lazy client, so it no longer breaks boot.
- Legacy `/input` keyword router and `/chat` kept as-is apart from graceful failures.
- Embeddings for notes/memory are written when a key exists at write time; run
  `retrieval.index_notes(db)` / `index_memories(db)` once after setting the key to backfill
  (a `POST /vault/sync` embeds the vault). No endpoint for that backfill yet.

---

## 2. Endpoints — new or changed

All responses are JSON. Dates are ISO strings. `changes` entries have the shape
`{id, summary, table, action, record_id, batch_id, undone}`.

### Assistant
- `POST /orchestrator` `{text, session_id, context?}` → SSE `data:` events:
  - `{"type":"reasoning","content"}`
  - `{"type":"tool_call","tool","args"}`
  - `{"type":"tool_result","tool","summary","error": bool}`
  - `{"type":"error","content"}` (only when the model call fails; still followed by `final`)
  - `{"type":"final","content","changes":[...]}`
- `POST /chat`, `POST /parse`, `POST /input` unchanged shapes; 503 when AI unconfigured,
  502 on upstream failure (`detail` explains). `/parse` and `/input` responses now include
  `updates.changes`, `updates.batch_id`, `updates.suggestions`, `updates.ideas_inboxed`,
  `updates.discussion_skipped`, `updates.todos_completed`, `updates.todos_complete_unmatched`,
  `updates.reading_notes_appended`; `revenue_logged[]` items include `currency`.
- `GET /health` → `{ok, ai_configured, assistant_model}`.

### Changes / undo
- `GET /changes?limit=50&batch_id=` → `[{...change, source, created_at}]`
- `POST /changes/{id}/undo` → `{...change, already_undone: bool}`
- `POST /changes/batch/{batch_id}/undo` → `{batch_id, undone, already_undone, changes:[...]}`

### Pending actions (deletes)
- `GET /actions/pending` → `[{id, kind, table, record_id, summary, status, created_at, expires_at, requires_confirmation}]`
- `POST /actions/{id}/confirm` → `{...pending, executed: bool, change?|reason?}`
- `POST /actions/{id}/cancel` → `{...pending}`

### Suggestions
- `GET /suggestions?status=pending|accepted|dismissed|all` →
  `[{id, kind, payload, confidence, reason, status, created_at, source_text, text}]`
  kinds: `todo_add` (payload `{text, priority, category, due, roadmap_id}`), `inbox_add`
  (`{text}`), `todo_complete` (`{todo_id, text, fragment, score}`)
- `POST /suggestions/{id}/accept` → `{...suggestion, applied: bool, updates, changes}`
- `POST /suggestions/{id}/dismiss` → `{...suggestion}`

### Notes
- `GET /notes?kind=&q=&limit=` → `[{id, title, kind, tags[], created_at, updated_at, preview, score?}]`
- `GET /notes/{id}` → `{id, title, kind, tags[], body, created_at, updated_at}`
- `POST /notes` `{title, kind?, body?, tags?[]}` → 201 note
- `PATCH /notes/{id}` `{title?, kind?, body?, tags?[]}` → note
- `POST /notes/{id}/append` `{text, date?}` → note

### Revenue / scoreboard
- `GET /revenue` → `{total (raw sum, legacy), total_usd, by_currency{USD,ZMW}, zmw_per_usd, records[{..., currency}]}`
- `POST /revenue` accepts `currency` (default USD) → `{id, amount, currency}`
- `GET /scoreboard/ten-k` → adds `zmw_per_usd`; `ai_revenue_usd` is converted.
- `GET /todos` rows now include `due_date`; `POST /todos` sets it from `due`.

### Syllabus
- `GET /syllabus` →
  ```
  {"subjects":[{"subject_code","subject_name","level","exam_date",
                "papers":[{"paper","exam_date","progress":{total,pct,bulletproof_pct,counts{...}},
                           "points":[{id, subject_code, subject_name, level, paper, topic_code, title,
                                      status, colour, past_paper_score, last_revised, weakness_notes, updated_at}]}],
                "progress":{...}}],
   "progress":{...}}
  ```
- `POST /syllabus/import` multipart: `file`, `subject_code`, `subject_name`, `level?`,
  `papers?` ("P1,S1") → `{created, updated, skipped, total_rows, papers[]}`
- `POST /syllabus/{id}/advance` → point (status moves to the next state, wrapping)
- `PATCH /syllabus/{id}` `{status?, weakness_notes?, past_paper_score?, last_revised?, title?}` → point
- `GET /syllabus/weakest?limit=5` → `[{...point, score, reason, exam_days_away}]`

### Briefing
- `GET /briefing/today?refresh=0|1` →
  ```
  {"date","weekday","badge":"EXECUTION DAY|BUILD DAY|REVIEW DAY",
   "greeting":"Good morning|afternoon|evening",
   "greeting_line": "You're 26% through your November targets. Two items need your attention.",
   "summary": "3–5 sentences" | null, "summary_source":"llm|deterministic", "summary_cached": bool,
   "items":[{"id","title","category":"task|deadline|non_negotiable|exam|syllabus|target|alert",
             "due_label":"Today|Tomorrow|Due 7 Nov|2d overdue|19 days|CRITICAL",
             "due_date", "reason", "suggested_action", "target_type":"todo|habit|exam|syllabus_point|annual_target|alert",
             "target_id", "completable": bool, "score", ...}],
   "attention_count", "alert_count", "alerts":[{rule_id, severity, message}],
   "progress":{"academics": int|null, "business": int, "targets": int|null, "habits": int},
   "counts":{overdue, today, upcoming, undated, items, non_negotiables_done, non_negotiables_total, exams_upcoming},
   "exams":[{subject_code, subject, paper, exam_date, days_away}],
   "non_negotiables":[{key, label, done, habit_id}],
   "weakness_notes":[{syllabus_point_id, subject, paper, topic, note, date, status}]}
  ```
  A checkbox on a `completable` item calls the assistant's `complete_task` with
  `todo_id = target_id` (or `POST /todos/{id}/toggle`).

### Usage
- `GET /usage/summary?days=90` → `{days, total{calls,input_tokens,cached_input_tokens,output_tokens,estimated_cost_usd}, by_day[], by_month[], by_model[]}`

### Market intelligence v2 (Phase 5A — `backend/market_intel/`)

A finding now also carries `recommendation` (`pursue` at 7.5 and above, `watch` at 6.0 and
above, else `park`), `times_seen`, `dismissed_reason` and `reviewed_at`. `overall` is a
weighted mean (weights in `context/core/opportunity_weights.json`).

| Method & path | Body / query | Returns |
|---|---|---|
| `POST /research/findings/{id}/dismiss` | `{reason?}` | the finding + `change`; a repeat returns `already_dismissed: true` and `change: null`; 404 for an unknown id |
| `GET /research/objectives` | | `[{id, objective, cadence_days, enabled, max_opportunities, last_run_at, last_project_id, due}]` |
| `POST /research/objectives` | `{objective, cadence_days?, max_opportunities?}` | 201 objective + `created`, `change`. Text that restates an existing objective updates that one (`created: false`) |
| `PATCH /research/objectives/{id}` | `{objective?, cadence_days?, enabled?, max_opportunities?}` | objective + `change` |
| `POST /research/objectives/{id}/run` | | the research run's result (`project_id, title, opportunities[], rejected_count, lessons_updated, repeated[], cost_usd, llm_calls`) plus `objective_id`, `objective`, `changes`; 503 with no AI key. Slow: a full run |
| `GET /research/projects/{id}` | | existing fields plus `cost_usd`, `llm_calls`, `objective_id`, `repeated: [{finding_id, problem}]` |
| `GET /research/pipeline` | | existing fields plus `next_action`, `due_date`; each `finding` carries `recommendation` |
| `PATCH /research/pipeline/{id}` | existing `stage?`, `notes?` plus `next_action?`, `due_date?` | `{id, stage, notes, next_action, due_date, change}` |
| `POST /research/promote` | unchanged | existing fields plus `changes` |

`due` is `enabled and (never run, or last run at least cadence_days ago)`. A research
run's result lists findings it came across again under `repeated` instead of surfacing them
twice; the existing finding's `times_seen` goes up and it gains the new evidence.

Briefing additions: items with category `opportunity` — a `pursue` finding surfaced in the
last 14 days and not yet promoted or dismissed (target type `research_finding`, score 35,
`due_label` its score out of ten, at most three); a pipeline entry in `discovered` or
`researching` that is past its `due_date` (48) or untouched for 14 days (30) (target type
`opportunity_pipeline`); a standing objective that is due (target type
`research_objective`, 12). `counts.opportunities_new` is the number of such findings; it is
not part of `attention_count`.

### Leads and outreach (Phase 5B — `backend/leads.py`, `backend/outreach.py`)

A lead: `{id, name, company, role, email, phone, whatsapp, linkedin_url, website, location,
segment, source, status, qualification_score, qualification, opportunity_id, client_id,
notes, last_contact_at, next_follow_up_at, created_at, updated_at, open_follow_up,
last_message}`. `status` is `new → qualified | disqualified → contacted → replied →
converted | lost`. `qualification` is `{fit, reasons[], disqualifiers[], suggested_angle,
model, scored_at}` or null. `open_follow_up` is `{todo_id, due_date}` or null;
`last_message` is `{id, channel, status, created_at}` or null. Emails are stored
lower-cased; phone and WhatsApp numbers as digits with an optional leading `+`.

A message: `{id, lead_id, lead_name, channel, direction, subject, body, status, version,
supersedes_id, brief, pending_action_id, approved_at, sent_at, external_id, error,
created_by, created_at, updated_at}`. `channel` is `whatsapp | email | linkedin | call |
other`; `status` is `draft → approved → sent | failed`, or `cancelled`; inbound rows are
`direction: in`, `status: received`.

| Method & path | Body / query | Returns |
|---|---|---|
| `GET /leads` | `status?`, `segment?`, `q?` (contains, on name or company), `limit=100` | `[lead]` |
| `GET /leads/segments` | | `[{segment, total, by_status{}}]` |
| `GET /leads/{id}` | | lead + `messages[]` + `follow_ups[]` (`{todo_id, text, due_date}`, open only) |
| `POST /leads` | `{name, company?, role?, email?, phone?, whatsapp?, linkedin_url?, website?, location?, segment?, notes?}` | 201 lead + `change`, `deduped: false`; when the details match an existing lead, 200 with that lead updated and `deduped: true` |
| `PATCH /leads/{id}` | any lead field, `status` | lead + `change` (400 for an unknown status) |
| `POST /leads/import` | multipart `file` (CSV), `segment?`, `mapping?` (JSON `{csv header: column}`) | `{import_id, created, updated, skipped, total_rows, segment, columns_mapped{}, batch_id}` |
| `POST /leads/qualify` | `{ids: [..≤20]}` or `{segment}` (the first 20 `new` leads of it) | `{results: [{lead_id, name, pre_score, score, status, fit, reasons, disqualifiers, suggested_angle, outcome?, error?, skipped_llm?}], changes}`; 400 above 20 |
| `POST /leads/{id}/replied` | `{summary?}` | lead + `change`, `changes`, `message_id` |
| `POST /leads/{id}/convert` | | `{lead, client: {id, name, company, status, service, lead_id}, changes}` (+ `already_converted: true` on a repeat) |
| `GET /outreach` | `status?` (comma list, e.g. `draft`), `lead_id?` | `[message]` newest first |
| `GET /outreach/stats` | `days=90` | `{days, by_segment: [{segment, leads, contacted, replied, converted, reply_rate, conversion_rate}], by_channel: [{channel, sent, contacted, replied, converted, reply_rate, conversion_rate}], totals: {leads, sent, contacted, replied, converted, reply_rate, conversion_rate}}` |
| `GET /outreach/{id}` | | message |
| `POST /outreach/{id}/send` | | `{message, changes, open_url?, follow_up_todo_id?}`; 409 unless the message is `draft` or `approved`. A failed send returns 200 with `message.status: failed` and `message.error` |
| `POST /outreach/{id}/cancel` | | message + `change`; 409 unless `draft` or `approved` |

`POST /outreach/{id}/send` is the Approve & send tap: the tap is the approval, so it sends
in one step. For WhatsApp the response carries `open_url` (a `wa.me` link the UI opens in a
new tab) and the message is marked sent. A successful send also moves the lead to
`contacted`, creates the follow-up todo three days out (`category` and `source`
`outreach`, `lead_id` set) and logs a communication. The assistant cannot send: its
`request_send` tool only creates a pending action of kind `send_outreach`, and its
`confirm_action` is refused with `{"error": "confirmation must come in a later message"}`
until Bandile has written again. `POST /actions/{id}/confirm` (the dock tap) executes it.

Briefing additions: items with category `outreach` — a draft awaiting approval (target type
`outreach_message`, score 38, at most three) and a contacted lead with no reply for more
than seven days and no open follow-up (target type `lead`, score 28, at most three);
`counts.drafts_waiting`. Alert `outreach_stalled` (warn): at least three qualified leads
and nothing sent in seven days.

### CRM (Phase 5C — `backend/crm.py`)

Money is always `amount` + `currency` (`USD` | `ZMW`); serialised money rows also carry
`amount_usd` (converted at `ZMW_PER_USD`). Totals are `{by_currency: {ZMW: n, USD: n},
total_usd: n}`. Every write returns `change` (one entry) and, where more than one row was
written, `changes`.

| Method & path | Body / query | Returns |
|---|---|---|
| `GET /clients` | `status?`, `q?` | the existing fields plus `currency`, `health` (`green\|amber\|red`, computed live), `tags[]`, `lead_id`, `contacts_count`, `open_deals`, `outstanding{…}`, `mrr{…}`, `last_contact_at` |
| `GET /clients/{id}` | | the list row plus `contacts[]`, `deals[]`, `projects[]`, `payments[]`, `recurring[]`, `communications[]` (last 50) |
| `PATCH /clients/{id}` | existing `status?`, `value?`, `notes?`, `service?` plus `currency?`, `tags?: [str]` | `{id, status, currency, tags, change}`; status must be `prospect\|active\|paused\|closed\|lost` (400 otherwise) |
| `POST /clients/{id}/contacts` | `{name, role?, email?, phone?, whatsapp?, is_primary?}` | 201 contact + `change`, `changes` |
| `PATCH /contacts/{id}` | any contact field | contact + `change` |
| `GET /deals` | `stage?`, `client_id?` | `[deal]` with `client_name`, `amount_usd` |
| `POST /deals` | `{client_id, title, amount?, currency?, stage?, expected_close?}` | 201 deal + `change`, `changes` (+ `project` when created as won) |
| `PATCH /deals/{id}` | `{stage?, amount?, currency?, probability?, expected_close?, lost_reason?, notes?}` | deal + `change`, `changes` (+ `project` the first time the stage becomes `won`) |
| `GET /payments` | `status?` (comma list, default `due,overdue`; `all` for everything), `client_id?`, `from?`, `to?` | `[payment]` with `client_name`, `amount_usd` |
| `POST /payments` | `{client_id, amount, currency?, status?: due\|paid, title?, due_date?, deal_id?, project_id?}` | 201 payment + `change`, `changes` (a `paid` payment also creates its `revenue` row) |
| `POST /payments/{id}/paid` | `{paid_at?}` | payment with `revenue_id` + `changes`; a second call returns `already_paid: true` and writes nothing; 409 for a cancelled payment |
| `POST /payments/{id}/cancel` | | payment + `change`; 409 when it is already paid |
| `GET /recurring` | | `[recurring]` with `client_name`, `monthly_amount` |
| `POST /recurring` | `{client_id, title, amount, currency?, interval?: monthly\|quarterly\|annual, day_of_month?, start?}` | 201 + `change` |
| `PATCH /recurring/{id}` | `{active?, amount?, currency?, interval?, day_of_month?, ended_at?}` | + `change` |
| `GET /communications` | `client_id?`, `lead_id?`, `limit=50` | `[communication]` newest first |
| `POST /communications` | `{client_id?, lead_id?, contact_id?, channel, direction?, summary, occurred_at?}` | 201 + `change` |
| `GET /crm/summary` | | `{clients:{active, paused, total}, deals:{by_stage:{stage:{count, amount_usd}}, weighted_pipeline_usd}, payments:{due_usd, overdue_usd, overdue_count, paid_30d_usd}, mrr:{by_currency, total_usd}, revenue_30d_usd, health:{green, amber, red}}` |

`GET /crm/summary` and `GET /briefing/today` first run `crm.housekeeping` (create the due
payment for each recurring period that has arrived, flip `due` to `overdue`, link stray
revenue rows to open payments, store client health), so both can write. Deal stages:
`lead → proposal → negotiation → won | lost`; a stage change sets the default probability
(10 / 40 / 70 / 100 / 0) unless one is given. `GET /revenue` is unchanged.

Briefing additions: items with category `payment` (overdue: score 72 + min(days, 14);
due within 7 days: 42), `deal` (40) and `client` (26, at most three); `counts.payments_overdue`
(included in `attention_count`); a new top-level `business_detail: {revenue_pct, mrr_usd,
outstanding_usd, overdue_count}`. `progress.business` is unchanged.

### Project portfolio (Phase 5E — `backend/portfolio.py`)

A project: `{id, title, kind (client|internal), client_id, client_name, deal_id, status,
priority (1 high … 5 low), expected_value, currency, expected_value_usd, effort_hours_week,
start_date, due_date, delivered_at, next_action, blocked_reason, last_activity_at, health,
notes, created_at, updated_at}`. `status` is `planned | active | paused | delivered |
closed | cancelled`.

`attention`: `{score, components: {value 0–3, urgency 0–4, staleness 0–2, blocked 0|2,
priority 0.4–2, waiting 0|1}, bottleneck, overdue_days, idle_days, waiting_days}`. The
bottleneck is the first that applies of: `blocked: <reason>`, `overdue by N days`,
`waiting on client for N days`, `no activity for N days`, `payment overdue (K…)`, or null.

| Method & path | Body / query | Returns |
|---|---|---|
| `GET /portfolio` | `include_paused=1` | `{ranked: [entry], paused: [entry], pause_suggestions: [suggestion], bottlenecks: [{project_id, title, bottleneck}], totals: {active, paused, blocked, overdue, value_usd_active}}`; an entry is `{project, attention, bottleneck, recommendation, rank}` |
| `GET /projects` | `status?`, `kind?`, `client_id?` | `[project + attention]` |
| `GET /projects/{id}` | | project + `attention`, `updates[]`, `payments[]`, `communications[]` (the client's last 20) |
| `POST /projects` | `{title, kind?, client_id?, priority?, expected_value?, currency?, due_date?, effort_hours_week?, next_action?}` | 201 project + `change`, `changes` |
| `PATCH /projects/{id}` | any of the above plus `status?`, `blocked_reason?`, `notes?` | project + `change`, `changes`. An empty `blocked_reason` clears the block and writes an `unblocked` update; a new reason writes a `blocker` update |
| `POST /projects/{id}/status` | `{status}`: `paused`, `active`, `delivered` or `closed` | project + `change`, `changes` (the project and a `status` update row); 400 for any other status |
| `POST /projects/{id}/focus` | | project (now priority 1) + `changes`; every other active priority-1 project drops to 2, all in one batch |
| `POST /projects/{id}/updates` | `{text, kind?: note\|milestone\|blocker\|unblocked, occurred_at?}` | 201 `{id, project_id, project, kind, text, occurred_at, blocked_reason, health, change, changes}` |

`ranked` holds active, planned and delivered projects, highest score first; `rank` is set
for active projects only. `recommendation` is checked in this order: `close` (delivered
more than 30 days ago), `pause` (active, no activity for more than 14 days, nothing due
within 30 days, value under a third of the $5,000 cap), `focus` (one of the three
highest-scoring active projects that are not recommended for a pause), else `maintain`.
`GET /portfolio` stores each project's `health` and files one `project_pause` suggestion
per pause recommendation (none while one is already open), so it can write. Accepting the
suggestion (`POST /suggestions/{id}/accept`) pauses the project; nothing is paused
otherwise.

Briefing additions: items with category `project` — blocked (62), overdue (70 + min(days,
9)), waiting on the client for more than seven days (36) and the rank-1 focus project (45,
`due_label` `FOCUS`); `progress.projects` (delivered or closed in 90 days ÷ that plus
active, null with no projects). Alert `projects_blocked` (warn) at two blocked active
projects.

### Personal brand (Phase 5D — `backend/brand/`)

Real platform data only. A number the platform did not give is `null`, never 0; nothing is
estimated and no metric can be typed in.

An account: `{id, platform, handle, external_id, access (api_key|export|oauth), status
(connected|error|disconnected), last_synced_at, last_error, created_at, updated_at,
followers, followers_delta_30d, following, posts_count, total_views, metrics_captured_at,
posts_stored}`. A post: `{id, account_id, platform, external_id, url, title, kind
(video|short|post|article|thread), posted_at, idea_id, topic_tags[], views, likes, comments,
shares, saves, impressions, watch_time_s, engagement, engagement_rate, captured_at}` where
`engagement` is likes + comments + shares + saves over the values that were measured and
`engagement_rate` is engagement ÷ views (or impressions when there are no views). An idea:
`{id, title, angle, platform, format, status (idea|drafted|scheduled|posted|dropped),
scheduled_for, posted_post_id, source, score, created_at, updated_at}`.

| Method & path | Body / query | Returns |
|---|---|---|
| `GET /brand/accounts` | | `[account]` |
| `POST /brand/accounts` | `{platform, handle?, external_id?}` | 201 account + `change`. YouTube resolves the handle to the channel id now; a failure is 400 with the adapter's message (`YOUTUBE_API_KEY not set`, `no YouTube channel found for @x`). LinkedIn is connected by importing its export |
| `POST /brand/accounts/{id}/sync` | | `{ok, posts_new, posts_updated, metrics_rows, account_rows, status, last_error, account, changes}`; a failed sync is 200 with `ok: false`, `status: error` |
| `DELETE /brand/accounts/{id}` | | account (`status: disconnected`) + `change`; nothing is deleted |
| `POST /brand/import` | multipart `file` (CSV saved from the platform's export), `platform` (`linkedin`) | `{account_id, platform, account_created, rows, posts_new, posts_updated, metrics_rows, batch_id}`; 400 when the file has none of the expected columns or the platform has no importer |
| `GET /brand/performance` | `platform?`, `days=90` | `{connected, platform, days, accounts[], top_posts: [{post, views, impressions, engagement, engagement_rate, posted_at}], by_format: {kind: {count, median_views, median_engagement_rate}}, cadence: {posts_in_window, per_week, gap_days_since_last}, trend: [{week_start, posts, views, engagement}] (12 weeks), insight, brand_target_posts_30d, posts_last_30d}` |
| `GET /brand/posts` | `platform?`, `limit=50` | `[post]` newest first |
| `PATCH /brand/posts/{id}` | `{topic_tags?: [str], idea_id?}` | post + `change` |
| `GET /brand/ideas` | `status?` (comma list) | `[idea]` |
| `POST /brand/ideas` | `{title, angle?, platform?, format?, scheduled_for?}` | 201 idea + `change` (a date makes it `scheduled`) |
| `PATCH /brand/ideas/{id}` | `{title?, angle?, platform?, format?, status?, scheduled_for?, score?, posted_post_id?}` | idea + `change` |
| `POST /brand/ideas/{id}/advance` | | idea + `change` (idea → drafted → scheduled → posted); 409 `scheduling needs a date` when the next step is `scheduled` and there is no date; 400 once it is posted or dropped |
| `GET /brand/calendar` | `from?`, `to?` (default this week and next) | `{from, to, days: [{date, is_today, ideas[], posted[]}], backlog[]}` |
| `GET /brand/insight` | `platform?`, `refresh=0\|1` | `{week_start, platform, text \| null, cached}` |

Snapshots are append-only and written only when a number changed. The weekly insight is at
most one model call per platform per ISO week; `GET /brand/performance` returns the cached
text and never generates it. Briefing additions: items with category `content` — an idea
scheduled for today (58) or overdue (66), an account in `error` (20), no post for more than
14 days on a connected account (22); `progress.brand` (posts in 30 days against
`brand_target_posts_30d: 8`, `null` with no account).

### Agents and the scheduler (Phase 6B — `backend/agents/`)

An agent in the registry: `{key, name, description, phase, enabled, schedule: {interval_minutes,
at_time, weekdays, effective, overridden}, events[], permissions: {tools[], tables_write[],
may_notify, may_emit[], max_llm_calls, max_seconds}, last_run: {at, status, summary,
cost_usd} | null, runs_count, cost_usd_total, cost_usd_month, next_due_estimate}`.
`schedule.effective` is a phrase: `every 60 min`, `daily at 06:00`, `Sun at 07:00`, or
`on events only`.

A run: `{id, agent_key, agent_name, trigger (schedule|event|manual|in_process), tick_id,
started_at, finished_at, duration_ms, status (running|completed|skipped|failed|violation),
summary, result, error, log, llm_calls, cost_usd, changes_count, batch_id, over_time,
changes[]}` (`changes` — at most ten change entries — on the listing endpoints).

| Method & path | Body / query | Returns |
|---|---|---|
| `GET /agents` | | `[agent]` in run order (couriers last) |
| `GET /agents/{key}` | | agent + `config` + `runs` (the last 20) |
| `PATCH /agents/{key}` | `{enabled?, interval_minutes?, at_time?, weekdays?, config?}` | agent + `change` (the toggle is change-logged, so it can be undone). `interval_minutes: 0`, `at_time: ""` and `weekdays: []` clear an override; 400 for a bad time or weekday |
| `POST /agents/{key}/run` | | runs now (`trigger: manual`), whatever the schedule or the toggle say → the run |
| `GET /agents/{key}/runs` | `limit=20` | `[run]` |
| `GET /agents/runs` | `limit=50`, `status?` | the activity log across agents, newest first |
| `POST /agents/runs/{id}/undo` | | undoes the run's whole batch; 409 when the run changed nothing |
| `GET /jobs/status` | | `{last_tick: {id, trigger, started_at, finished_at, agents_run, agents_skipped, events_consumed, errors, duration_ms} \| null, ticks_24h, agents_due_now: [keys], token_configured, in_process_scheduler}` |
| `POST /jobs/tick` | header `X-Jobs-Token` | `{tick_id, trigger, ran: [{key, status, summary}], skipped: [{key, reason}], deferred: [keys], events_consumed, events_dropped, errors, duration_ms}`; 401 for a wrong or missing token; 503 while `JOBS_TOKEN` is unset. Exempt from the session login |

Shipped agents and their defaults: `crm_housekeeper` (every 60 min), `lead_qualifier`
(every 180 min, at most 20 model calls), `follow_up_sentinel` (daily at 05:30, and on
`lead.replied`), `portfolio_reviewer` (daily at 05:40), `brand_sync` (daily at 04:00),
`market_intel` (every 60 min; one due objective per run, at most 12 model calls). Times are
in `FOUNDER_OS_TZ`. An agent whose module is not built reports `skipped`.

How agents run: the in-process scheduler calls the tick every 15 minutes while the process
is awake (`JOBS_IN_PROCESS`, default on); `POST /jobs/tick` does the same when called.
`.github/workflows/jobs-tick.yml` calls that endpoint but ships with its schedule commented
out (see ADR-0001's amendment). A write by an agent outside its `tables_write` undoes the
run's whole batch and marks the run `violation`.

### Applications (Phase 6A — `backend/applications.py`)

An application: `{id, name, institution, programme, kind, deadline, deadline_source
(user|document|unknown), decision_date, status, priority, portal_url, notes,
requirements_updated_at, gaps_reviewed_at, created_at, updated_at, timeline, gaps_open}`.
`status` is `researching → preparing → submitted → interview → accepted | rejected |
withdrawn`. `timeline` is `{days_to_deadline | null, requirements: {missing, in_progress,
ready, submitted}, requirements_total, next_due: {requirement_id, title, due_date, days,
from_deadline} | null, at_risk}`.

A requirement: `{id, application_id, title, kind, status (missing|in_progress|ready|
submitted|not_required), due_date, due_source (user|document|null), details, todo_id,
note_id, sort_order, created_at, updated_at}`. A gap: `{id, application_id, gap, severity
(high|medium|low), evidence, suggested_action, status (open|addressed|accepted_risk|
superseded), review_batch, created_at, updated_at}`.

| Method & path | Body / query | Returns |
|---|---|---|
| `GET /applications` | `status?` | `[application]` sorted by deadline (undated last) |
| `GET /applications/{id}` | | application + `requirements[]` + `gaps[]` (open, high first) |
| `POST /applications` | `{name, institution?, programme?, kind?, deadline?, priority?, portal_url?}` | 201 application + `change`. A deadline given here is `deadline_source: user` |
| `PATCH /applications/{id}` | any field incl. `status`, `deadline` (sets the source to `user`), `decision_date`, `notes` | application + `change`; 400 for an unknown status |
| `POST /applications/{id}/requirements/import` | `{text}` | `{application_id, application, created, updated, deadline_set, deadline, deadline_source, dates_dropped, changes, batch_id}`; 400 when the text is too short or the answer unreadable; 503 with no AI key |
| `POST /applications/{id}/requirements` | `{title, kind?, due_date?, details?}` | 201 requirement + `change` |
| `PATCH /requirements/{id}` | `{title?, kind?, status?, due_date?, details?}` | requirement + `change` |
| `POST /requirements/{id}/advance` | | requirement + `change` (missing → in_progress → ready → submitted → missing) |
| `POST /requirements/{id}/todo` | | `{todo: {id, text, due_date, done, category}, change, changes, existing}`; while that task is open a repeat returns it with `existing: true` |
| `GET /applications/{id}/gaps` | `refresh=0\|1` | `{application_id, application, gaps_reviewed_at, cached, stale, gaps[]}`; 503 for `refresh=1` with no AI key; 502 when the review cannot be read |
| `PATCH /gaps/{id}` | `{status: open\|addressed\|accepted_risk}` | gap + `change` |

Dates: the import keeps a date only when the model also gives the phrase it came from and
that phrase is in the pasted text; anything else is dropped and counted in
`dates_dropped`. A deadline or due date Bandile set himself is never overwritten by an
import. The gap review is one model call and is cached: `GET …/gaps` makes no call while
the last review is at least as new as the last change to the requirements. A new review
marks the previous open gaps `superseded`.

Briefing additions: items with category `application` — an application in preparation
whose deadline is within 60 days (target type `application`, score `max(10, 82 − days)`,
90 once the deadline has passed); a missing or in-progress requirement with its own due
date within 14 days (target type `application_requirement`, `60 + (14 − days)`, capped at
80); open high gaps when the deadline is within 90 days (target type `application_gap`,
24, at most two). `counts.applications_at_risk` is included in `attention_count`. Alert
`application_at_risk` (critical): a deadline inside 14 days with requirements not ready.
Agent `application_watch` (daily at 05:45) emits `application.deadline_near` once per
application, mark (30, 14, 7, 3, 1 days) and deadline, and refreshes stale gap reviews (at
most two a run).

### Integrations (Phase 6C — `backend/integrations.py`, `backend/integrations_telegram.py`)

An integration: `{provider (google_calendar|email|telegram|youtube), label, status
(disconnected|pending|connected|error), account_label, connected_at, last_sync_at,
last_error (only while failing), configured (are its env vars set), actions[]}`. `actions`
is the list of buttons the Settings card may show: `connect`, `sync`, `disconnect`, `pair`,
`set_webhook`. Tokens live in `integrations.secret_json`, which no endpoint returns; every
stored error and log line goes through `integrations.redact()`.

| Method & path | Body / query | Returns |
|---|---|---|
| `GET /integrations` | | `[integration]`, always the four providers in the order above |
| `POST /integrations/telegram/set-webhook` | | `{ok, description, url}`; 503 naming the missing env var (`TELEGRAM_BOT_TOKEN`, `FOUNDER_OS_API_URL`, `TELEGRAM_WEBHOOK_SECRET`) |
| `POST /integrations/telegram/unpair` | | the telegram integration |

### Google Calendar (Phase 6C — `backend/integrations_google.py`)

An event: `{id, title, start_local ("HH:MM" | "all day"), end_local, all_day, date, start_at,
end_at (naive UTC), location, html_link, created_by_us}`.

| Method & path | Body / query | Returns |
|---|---|---|
| `GET /integrations/google/start` | | `{url}` — send the browser there; 503 until `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` and `FOUNDER_OS_API_URL` are set |
| `GET /integrations/google/callback` | `code`, `state` (from Google; no session) | 302 to `/app/#settings?connected=google` or `/app/#settings?error=google_denied|google_state|google_exchange` |
| `POST /integrations/google/sync` | | `{ok, created, updated, deleted, unchanged, error}` |
| `POST /integrations/google/disconnect` | | the google_calendar integration |
| `GET /calendar/today` | `date?` (YYYY-MM-DD) | `{connected, events: [event]}` |
| `GET /calendar/week` | `from?` (YYYY-MM-DD) | `{connected, days: [{date, events}]}` — seven days |
| `POST /calendar/events` | `{title, start, duration_minutes?, location?, notes?}` | event + `change`; 422 without a date and time; 503 when not connected; 502 when Google refuses |

`GET /briefing/today` now carries `calendar: {connected, events_today, all_day}`,
`today_events` (all-day titles) and `integrations: {email, telegram, google_calendar}`; timed
events today are items of category `event` (target type `calendar_event`).

### Email (Phase 6C — `backend/integrations_email.py`)

A stored message: `{id, message_id, thread_key, direction (in|out), from_addr, to_addr,
subject, snippet (at most 500 characters), received_at, lead_id, client_id, contact_id,
outreach_message_id}`. Bodies are never stored; mail from unknown senders is never stored.

| Method & path | Body / query | Returns |
|---|---|---|
| `POST /integrations/email/sync` | | `{ok, fetched, stored, matched_leads, matched_clients, ignored, error}`; 503 until `EMAIL_ADDRESS` and `EMAIL_APP_PASSWORD` are set |
| `POST /integrations/email/test` | | `{ok, error}` — sends a test message to `EMAIL_ADDRESS` |
| `GET /email/messages` | `lead_id?`, `client_id?`, `limit=50` | `[message]`, newest first |

An outreach message on the `email` channel is now really sent when it is approved
(`POST /outreach/{id}/send`); `external_id` is the `Message-ID`, and a reply to it sets
`replied_at` on the outreach message.

### Notifications (Phase 6D — `backend/notify.py`, `backend/telegram_inbound.py`)

A notification: `{id, created_at, channel, kind (briefing|evening|alert|deadline|reminder|
reply|test), title, body, dedupe_key, send_at, status (queued|sent|failed|suppressed|
cancelled), sent_at, error, source, agent_run_id, suppressed_reason (quiet_hours|daily_cap|
channel_disabled|not_paired)}`. Settings: `{enabled, morning_time, evening_time | null,
quiet_start, quiet_end, alert_min_severity (info|warn|critical), daily_cap, channels,
deadline_days, muted_until, updated_at, paired, pair_code, bot_link | null,
channel_configured}`. Times are local `HH:MM`.

| Method & path | Body / query | Returns |
|---|---|---|
| `GET /notifications/settings` | | settings |
| `PATCH /notifications/settings` | any of `enabled`, `morning_time`, `evening_time` (`"off"` or null turns it off), `quiet_start`, `quiet_end`, `alert_min_severity`, `daily_cap` | settings + `change`; 422 for a bad value |
| `GET /notifications` | `limit=50`, `status?`, `kind?` | `[notification]`, newest first |
| `POST /notifications/test` | | `{status, error}` — sends "Founder OS connected" |
| `POST /notifications/brief-now` | | `{status, error}` — sends today's briefing now |
| `POST /notifications/{id}/cancel` | | notification; 409 unless it is queued |
| `POST /integrations/telegram/webhook` | a Telegram `Update`; header `X-Telegram-Bot-Api-Secret-Token` | `{}`; 403 unless the header equals `TELEGRAM_WEBHOOK_SECRET`. No session needed |

`GET /briefing/today` now carries `delivery: {last_sent_at, channel, paired}`.

Telegram commands: the six-character code (or `/start <code>`) pairs; then `/brief`,
`/undo`, `/yes`, `/no`, `/mute 2h` (`/mute off`), `/help`. Any other text is an assistant
turn (`orchestrator.run_turn`), saved in the one thread with `session_id =
telegram:<chat_id>`; the reply ends with `Changed: …` and `/undo to reverse` when something
changed, and `Reply /yes to confirm or /no.` when a delete or a send was proposed.

### Tool registry (for the UI's reference)
`route_brain_dump, get_dashboard_state, detect_off_track, synthesize_weekly_review,
surface_weakest_topics, query_war_room, add_todos, query_cold_archive,
run_market_research_agent, promote_research_opportunity, remember, propose_delete,
confirm_action, cancel_action, complete_task, notes_search, notes_read, notes_append,
notes_create, get_deadlines, set_exam_date, update_syllabus`

---

## 3. Migrations

Run automatically at startup, in order, before the schema check. All re-runnable; none drops
anything. RLS enabled on every new table on Postgres (m008 convention).

| Migration | Adds |
|---|---|
| m010 | `chat_messages.thread_id/tool_calls_json/tool_call_id/changes_json` (+ merge into `default`), `ai_memory.kind/tags`, `conversation_summaries`, `llm_usage` |
| m011 | `change_log`, `pending_actions`, `suggestions`, `notes` |
| m012 | `revenue.currency` (backfilled USD), `todos.due_date` (Python backfill), `embeddings`, `exam_dates`; one-off copy of books + Bible log into notes |
| m013 | `syllabus_points`, `briefing_summaries` |
| m014 | Phase 5A: `research_objectives`; columns on `research_projects`, `research_findings`, `opportunity_pipeline`; a Python backfill of `recommendation` |
| m015 | Phase 5B: `leads`, `lead_imports`, `outreach_messages`; `todos.lead_id`, `todos.client_id` |
| m016 | Phase 5C: `contacts`, `deals`, `projects`, `payments`, `recurring_revenue`, `communications`; `clients.lead_id/currency/tags/health`. **Backfills**: existing clients become `USD`; every revenue row with a client gets one paid payment (matched by `revenue_id`, so re-running adds nothing). Back up before the first production run |
| m017 | Phase 5D: `social_accounts`, `content_posts`, `content_metrics`, `account_metrics`, `content_ideas`, `brand_insights` |
| m018 | Phase 5E: `project_updates` |
| m019 | Phase 6A: `applications`, `application_requirements`, `application_gaps` |
| m020 | Phase 6B: `agents`, `agent_runs`, `agent_events`, `job_ticks`; `llm_usage.agent_run_id` |
| m021 | Phase 6C: `integrations`, `calendar_events`, `email_messages`; `outreach_messages.replied_at` |
| m022 | Phase 6D: `notifications`, `notification_settings` (row 1 created with the defaults) |

**Postgres notes**
- All ADD COLUMN use `IF NOT EXISTS`; all tables `CREATE TABLE IF NOT EXISTS`.
- `todos.due_date` backfill is row-by-row in Python; no SQL cast, so non-ISO strings cannot
  fail the migration (they stay NULL).
- `syllabus_points` unique index uses `COALESCE(paper, '')` because Postgres treats NULLs as
  distinct.
- Fuzzy matching uses `lower()`/Python, not `ILIKE`, so SQLite and Postgres behave the same.
- Boolean literals are handled via the ORM, not raw SQL.
- Existing Postgres rows: `revenue.currency` is set to `USD` for every existing row. If any
  historic rows were actually Kwacha logged as a bare number, fix them by hand.

---

## 4. Env vars to set on Render

| Var | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | unset | Without it everything non-AI works; AI endpoints 503 |
| `ASSISTANT_MODEL` | `gpt-5.4-mini` | Assistant, parser, summaries, briefing summary |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | Vault / notes / memory embeddings |
| `ASSISTANT_HISTORY_TURNS` | `20` | Verbatim history window |
| `ASSISTANT_SUMMARY_BATCH` | `10` | Messages that must fall out of the window before the summary refreshes |
| `INTENT_CONFIDENCE_MIN` | `0.7` | Below this an extracted item becomes a suggestion |
| `ZMW_PER_USD` | `27.0` | Kwacha→dollar rate for the $10K scoreboard. **Set this.** |
| `DEFAULT_CURRENCY` | `ZMW` | Currency of a client when none is given (`ZMW` or `USD`) |
| `LLM_PRICING_JSON` | built-in table | Override per-model prices for `llm_usage` cost estimates |
| `ORCHESTRATOR_MODEL` | — | Legacy; still honoured if `ASSISTANT_MODEL` is unset |

Phases 5 and 6 add (all optional; each feature shows "not connected" without its variables):

| Var | Default | Purpose |
|---|---|---|
| `RESEARCH_MODEL` | the assistant model | Model for the market-intelligence roles |
| `JOBS_TOKEN` | unset | Secret for `POST /jobs/tick`; 32+ random characters. Unset: the endpoint answers 503 |
| `FOUNDER_OS_TZ` | `Africa/Lusaka` | The zone schedules and notification times are read in |
| `JOBS_TICK_BUDGET` | `80` | Seconds a tick may spend starting agents |
| `JOBS_IN_PROCESS` | `1` | `0` turns off the in-process 15-minute tick |
| `YOUTUBE_API_KEY` | unset | YouTube Data API v3 key for the Brand tab |
| `FOUNDER_OS_API_URL` | unset | The service's public URL, no trailing slash. Needed for the Telegram webhook and the Google redirect |
| `TELEGRAM_BOT_TOKEN` | unset | Secret. The bot's token from @BotFather |
| `TELEGRAM_BOT_USERNAME` | unset | The bot's username, for the link in Settings |
| `TELEGRAM_WEBHOOK_SECRET` | unset | Secret. A random string Telegram sends back with each update |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | unset | The Google OAuth client (the secret is a secret) |
| `GOOGLE_CALENDAR_ID` | `primary` | Which calendar to read and write |
| `EMAIL_ADDRESS` / `EMAIL_APP_PASSWORD` | unset | The Gmail address and its App Password (a secret) |
| `EMAIL_IMAP_HOST` / `EMAIL_SMTP_HOST` / `EMAIL_SMTP_PORT` / `EMAIL_FROM_NAME` | Gmail's, `465`, `Bandile Masocha` | Mail servers and the sender name |

---

## 5. Known issues

- **Pricing table is an estimate.** `llm.py` ships default prices for gpt-5.4-mini / gpt-5.4 /
  gpt-4o-mini / embeddings; check against OpenAI's current pricing and override with
  `LLM_PRICING_JSON` if they differ. Costs are "estimated", never billed figures.
- **Streaming usage** relies on `stream_options={"include_usage": true}`; if the SDK/model
  doesn't return it, the call is logged with zero tokens (still counted as a call).
- **Test harness never fires startup.** `TestClient(app)` without a context manager does not
  run `on_event("startup")`, so the suite exercises migrations only through
  `test_startup_handler_runs_with_no_key`, which calls `main.startup()` directly against
  the local SQLite file. I also ran the startup chain by hand after each stage.
- **Requirements pins vs Python 3.13.** The pinned `pydantic==2.7.1` / `psycopg2-binary`
  will not build on 3.13; use 3.12 (as the spec says).
- `get_dashboard_state` no longer reports the trading gate; the old UI's dashboard reads the
  `/trading/*` endpoints directly, which are unchanged.
- The default thread means the frontend's `founderOS_session` id is informational only.

---

## 6. Stage 5 addendum — real-time voice

Committed separately so Stage 4 is never at risk.

### What was built
- **`POST /voice/realtime/session`** mints a short-lived client secret through OpenAI's
  `/v1/realtime/client_secrets` (TTL 600 s). The browser never sees the API key. The session
  config sent upstream carries the orchestrator prompt plus a voice addendum, the **same tool
  registry** (22 tools, converted to the realtime API's flat tool shape), server VAD with
  `interrupt_response`, input transcription, and the output voice.
  Response: `{client_secret:{value, expires_at}, model, voice, voice_session_id, calls_url,
  silence_timeout_seconds, thread_id}`. 503 with no key; 502 with OpenAI's own message if the
  session is rejected (e.g. an unknown model id).
- **`POST /voice/realtime/tool`** `{name, arguments, call_id?, voice_session_id?}` runs one
  function call through `orchestrator_tools.run_tool` inside a change batch — same change
  log, same delete-confirmation rules — and persists the assistant tool-call row and the
  tool-result row into the default thread. Returns `{call_id, name, result, summary, error,
  changes, output}`; `output` is the JSON string to hand back to the model.
- **`POST /voice/realtime/transcript`** `{role, text, voice_session_id?, changes?}` saves a
  transcript line into the default thread (so typed and spoken history are one history).
- **Frontend**: a `Live` button next to the mic. WebRTC peer connection, mic track with
  echo cancellation, remote audio element, data channel `oai-events`. Visible states:
  connecting / listening / thinking / speaking / acting, a running transcript under the
  command box, barge-in handled by server VAD (`speech_started` drops the partial line and
  returns to listening), tool calls bridged server-side then `function_call_output` +
  `response.create`, auto-stop after **2 minutes** of silence, and a Stop button. The old
  record → transcribe → speak loop on the mic button is untouched as the fallback.

### Env vars
| Var | Default | Purpose |
|---|---|---|
| `REALTIME_MODEL` | `gpt-realtime-mini` | Realtime model id — **see the caveat below** |
| `REALTIME_VOICE` | `marin` | Output voice |
| `REALTIME_TRANSCRIBE_MODEL` | `gpt-4o-mini-transcribe` | Input transcription model |
| `OPENAI_REALTIME_BASE` | `https://api.openai.com/v1/realtime` | Override for proxies |

### Caveat — not verified against OpenAI's docs
This build environment's network policy blocks `developers.openai.com` and
`platform.openai.com`, so the spec's instruction to "check the exact model ID in OpenAI's
docs" could not be carried out from here. Web search results (secondary sources) indicate:
- `gpt-realtime-mini` exists but is reported deprecated with shutdown on 20 Jan 2027;
- Azure lists `gpt-realtime-2.1-mini` as the current mini realtime model.
The default stays on `gpt-realtime-mini`; set `REALTIME_MODEL=gpt-realtime-2.1-mini` on
Render once you have confirmed it on OpenAI's model page. If the id is wrong, the session
endpoint returns 502 with OpenAI's message and the UI shows it in a toast — nothing else
breaks.

The same applies to the GA request/event shapes used (nested `session.audio.input.*`,
`/v1/realtime/calls` SDP exchange, `response.function_call_arguments.done`,
`conversation.item.input_audio_transcription.completed`,
`response.output_audio_transcript.*`). They follow OpenAI's GA Realtime API as documented
up to mid-2026; the frontend also accepts the older `response.audio_transcript.*` names.
Expect to do one live smoke test with a real key before relying on it.

### Not done
- No browser-level automated test of the WebRTC flow (no network, no mic in CI). The suite
  covers the three endpoints and asserts the frontend wiring.
- No "choose a voice by ear" UI; `REALTIME_VOICE` is an env var.
- Tool results from voice are not truncated before being sent back to the model (same
  as the typed path's live turn; only replay is truncated).

---

## 7. Stage 6 addendum — UI, security, clean-up

One commit per item (6.1 … 6.7), all on the same branch.

### 6.1 Login
- `FOUNDER_OS_PASSWORD` gates every route except `/health`, `/ping`, `/auth/*`, CORS
  preflights and the static frontend. Unset → the API is open (local dev) and startup logs a
  warning. **Set it on Render.**
- `POST /auth/login {password}` → `{auth_required, token, expires_at}`; `GET /auth/status` →
  `{auth_required, authenticated}`. Tokens are HMAC-signed and expire after
  `FOUNDER_OS_SESSION_DAYS` (30). Secret = `FOUNDER_OS_SECRET` or a hash of the password, so
  changing the password invalidates every session.
- Clients send `Authorization: Bearer <token>` (or `X-Founder-Token`). The frontend wraps
  `fetch` to add it and reopens the login gate on 401. Sign out lives in Settings.
- CORS: `FOUNDER_OS_ORIGINS` (comma-separated), default local dev origins only. Production is
  one host (Render serves the app at `/app/` and the API on the same origin), so it stays unset.

### 6.2 Home screen (Command Centre)
Built on `GET /briefing/today` (shape in section 2). Checkboxes call the new
`POST /todos/{id}/complete` → `{id, done, completed_at, change}` (logged, undoable). Header
bell shows `alert_count` and lists `alerts` plus overdue tasks; the AI pill reads `/health`.
Only the Academics tile exists; it opens the syllabus page.

### 6.3 Changes and suggestions in the dock
The final SSE event's `changes` render with Undo / Undo all (`POST /changes/{id}/undo`,
`POST /changes/batch/{batch_id}/undo`). Pending `GET /suggestions` render with Accept
(`POST /suggestions/{id}/accept`) and Dismiss.

### 6.4 Syllabus tracker
`GET /syllabus` grouped rows, four colours, tap → `POST /syllabus/{id}/advance`. Import CSV
modal → `POST /syllabus/import` (multipart: file, subject_code, subject_name, level, papers).

### 6.5 Removed from the UI
Trading Desk, Aether Command (Business tab), Reading Queue, Bible Plan, the $10K September
hero band and countdown strip, hardcoded exam/regionals dates, browser notifications, the
hidden agent-alerts panel and client-side urgent flags, the header drift badge. **No backend
tables or endpoints were removed**; `/trading/*`, `/books`, `/reading-plans`, `/clients`,
`/revenue`, `/scoreboard/ten-k` still answer.

### 6.6 Escaping and cold starts
Every user-text interpolation into `innerHTML` goes through `escHtml`; `mdHtml` escapes raw
HTML before markdown and strips non-http link schemes. The 10-second timeout is gone: sync
and ping wait up to 90 s with a "waking backend" header state after 2.5 s.

### 6.7 Docs
`CLAUDE.md` rewritten; `ROADMAP.md` added with six phases (1–4 core built).

### Env vars added in Stage 6
| Var | Default | Purpose |
|---|---|---|
| `FOUNDER_OS_PASSWORD` | unset (open) | Login password — set in production |
| `FOUNDER_OS_SECRET` | derived from password | Token signing secret (set to keep sessions across password changes) |
| `FOUNDER_OS_SESSION_DAYS` | `30` | Token lifetime |
| `FOUNDER_OS_ORIGINS` | local dev origins | CORS allow-list |

### Known issues after Stage 6
- The command dock is slightly wider than a 390 px phone viewport (the Live button is
  clipped); it was like that before the Live button and needs a responsive pass.
- The old Dashboard page is kept as a second page; it still renders KPIs, todos and the
  radar from local state. Decide whether it stays once the Command Centre covers it.
- The Academic Roadmap (legacy subject/topic/subtopic) page is still in the nav; the
  assistant no longer reads those tables.
- No automated browser tests; the UI was checked with headless Chromium screenshots and
  click-throughs (home, suggestions accept → undo, syllabus tap-to-advance) during the build.
- Login has no rate limiting.

---

## 8. Stage 7 addendum — verification, review, gaps, smoke tests, runbook

### 7.1 Postgres verification (PostgreSQL 16 in the build container)
- **Fresh database**: the startup chain ran three times (create_all → m002…m013 → schema
  check), idempotent; every new endpoint was exercised against it (import, re-import,
  advance, parse with intent/currency/notes, complete, undo, batch undo, suggestion accept,
  propose/confirm delete, deadlines, exam date, update_syllabus, briefing).
- **Production mirror**: a database built with `main`'s pre-rebuild code and seeded with
  legacy rows (two browser sessions of chat, todos with ISO / dd-mm-yyyy / junk / null due
  text, revenue, a book, a Bible log entry, a legacy memory row). The branch's startup
  migrated it correctly: chat rows merged into `default` with session ids preserved,
  `due_date` backfilled for the two parseable rows and left NULL for the rest, revenue set
  to USD, the book and the Bible log copied into notes, the ten new tables created with
  sequences and `timestamp` columns. Second run: no-op.
- **Full suite on Postgres** (`TEST_DATABASE_URL`): first run 417 passed, 1 error — a lock
  timeout caused by a test leaking an open session (invisible on SQLite). Fixed the test and
  made the fixture terminate stale connections; see the final run result at the end.
- **Bugs found and fixed**:
  1. **RLS was not enabled on any of the ten new tables on Postgres.** The migrations only
     enabled it on tables they created, but `create_all` at import creates them first. Now
     every migration enables RLS on all its tables on every run (verified: 10/10).
  2. The migration DDL used SQLite types (`INTEGER PRIMARY KEY AUTOINCREMENT`, `DATETIME`);
     on Postgres it is now rewritten to `SERIAL` / `TIMESTAMP`, so a table created by the
     migration rather than `create_all` still gets a sequence.

### 7.2 Branch review (fresh-reviewer pass over the diff vs main)
Checked: token signing (HMAC-SHA256 over the payload, `compare_digest` on both the password
and the signature, expiry enforced, secret derived from the password unless
`FOUNDER_OS_SECRET` is set so a password change invalidates sessions), route protection
(everything except `/health`, `/ping`, `/auth/*`, OPTIONS, `/`, `/index.html`, `/app/`),
undo, and the migrations for data loss (all statements are additive; backfills only fill
NULLs; the notes copy only inserts).
Fixed:
- Undo is now **all-or-nothing per batch** and returns 409 (instead of a 500 or a half-undone
  batch) when a record is gone or, on Postgres, still referenced by a foreign key.
- The static-frontend exemption matched any path starting with `/app` (e.g. `/apples`);
  it now matches `/app` and `/app/…` only.
Not reviewed in this pass: the market-intelligence agent, vault git handling, the health and
timetable pages' JS, and the legacy `/input` keyword router.

### 7.3 Known gaps
- Login rate limiting: 5 failures per 5 minutes per address (X-Forwarded-For aware) locks the
  address for 15 minutes; 429 with `Retry-After`. In-memory, resets on restart.
  Env: `LOGIN_MAX_FAILURES`, `LOGIN_WINDOW_SECONDS`, `LOGIN_LOCK_SECONDS`.
- Dock no longer clips at phone width (badge hidden, controls shrink, bar fits 390 px).
- Dashboard and the legacy Academic Roadmap are out of the nav (pages still exist in the
  file; nothing links to them).

### 7.4 Smoke tests
`node tests/e2e/smoke.js` boots the backend on a throwaway SQLite database with a password,
seeds a task and the syllabus fixture through the API, then drives headless Chromium:
login gate (401 → gate → wrong password → in), brief checkbox complete → change log → undo
→ reopened, syllabus tap-to-advance → backend state. It found and fixed one bug: a 401
body was being parsed as sync data (`fetchWithTimeout` now rejects non-OK responses).

### 7.5 Realtime (docs reached with curl)
OpenAI's models index lists `gpt-realtime-2.1-mini` as the current mini realtime model
(and `gpt-realtime-2.1`); `gpt-realtime-mini` still has a page. **Default changed to
`gpt-realtime-2.1-mini`.** The WebRTC guide confirms the Stage 5 shapes: client secret via
`POST /v1/realtime/client_secrets` with `{"session": {"type": "realtime", "model", "audio":
{"output": {"voice"}}}}`, response `value`; browser posts the SDP offer to
`/v1/realtime/calls` with `Authorization: Bearer <ephemeral>` and
`Content-Type: application/sdp` (no query string — removed ours); data channel
`oai-events`; `function_call_output` + `response.create`; `input_audio_buffer.speech_*`;
`response.output_audio_transcript.*`; `turn_detection.interrupt_response`. The frontend also
treats `session.started` as ready. Not confirmed (API reference page returned 403):
`expires_after` on the client-secret request — if OpenAI rejects it, the 502 says so and the
field can be dropped.

### 7.6 Deploy runbook
`docs/DEPLOY.md`: env vars in order, merge steps, post-deploy checks, Render rollback.

### Postgres suite — final run
After the fixture fix the full suite was re-run on PostgreSQL 16 (`TEST_DATABASE_URL`):
**421 passed, 0 failed, 0 errors** (2 min 34 s). The same 421 pass on SQLite.

---

## 9. Stage 8 addendum — command centre visual pass, brief, stage and voice

### 8.1 Visual pass (vs `docs/design/command-centre.png`)
Two-column hero: greeting block and the brief/progress cards on the left, the assistant's orb
on the right. Deeper glass cards with cyan edge glow, larger type scale, Academics /
Business / Targets gradient bars, header with logo mark, short date and live time. The orb
is pure CSS (sphere with dot grid, glowing eyes, halo platform, speech bubble, waveform) and
reflects `idle | listening | thinking | speaking | acting` from both typed turns and voice.
Checked side by side with the mock-up at 1440 px and 390 px (headless Chromium).

### 8.2 Today's Brief
At most five ranked items; "Show all" expands. All non-negotiables collapse into one
`Non-negotiables x/y` row (ranked where the first one was, pinned into the top five while
any are open) that expands on tap; each can be ticked (`POST /habits/toggle`).

### 8.4 `show_view` — the orb opens views
Tool `show_view(view, params)` with a fixed whitelist:

| view | params | data |
|---|---|---|
| `syllabus` | `subject` (name/code → code), `paper` (normalised) | `GET /syllabus`, filtered client-side |
| `brief` | — | current briefing items |
| `deadlines` | `days` (1–90) | **new** `GET /deadlines?days=` |
| `note` | `title` → resolved to `note_id` server-side | `GET /notes/{id}` |
| `changes` | `limit` (1–100) | `GET /changes` with Undo |
| `progress` | — | briefing progress + per-subject syllabus |
| `history` (aliases conversations, chat_history) | `q` (search words) | `GET /conversation/history` |
| `close` (aliases home) | — | closes the stage |

The orchestrator stream emits `{"type":"ui","directive":{"view","params"}}` after the tool
result; the voice tool bridge returns `ui_directive` in its response. The model never sends
HTML. Frontend: `#stage` full-screen overlay; the orb moves into the stage, the view renders
on the right, the dock stays usable on top; Esc / Close / a `close` directive return home.
Tested with OpenAI mocked (`tests/test_stage8_show_view.py`, 10 tests) and in the smoke run.

### 8.5 Live voice full-screen
Starting a live session opens the stage in voice mode (orb centred, transcript beneath, bubble
shows the last spoken line). A view opened by voice appears beside the orb. Esc or Close
clears the view first, then ends the session; stopping the session closes the stage.

### Known gaps after Stage 8
- The orb's listening/speaking states in a live session come from server VAD events; with no
  WebRTC available in CI they were exercised by feeding the same events to the handler.
- The stage views are read-mostly: the syllabus view supports tap-to-advance and the brief
  view supports ticking tasks; notes are read-only there.
- The mock-up's extra tiles (Business, Projects, Content, Applications, Agents) remain out by
  design (phases 5–6).

## 10. Stage 9 addendum — fidelity pass (pixel likeness to the mock-up)

### Assets (`frontend/assets/`)
- `orb.png` (430×367): sphere + platform cropped from `docs/design/command-centre.png`,
  luminance alpha with the sphere disc forced opaque; the mock-up's baked bubble and waveform
  were inpainted out before cropping.
- `scene.png` (712×866): the right-hand background scene, faded to the base colour over its
  left 260 px and dimmed under the tile band and the talk bar.
- Orb states (`idle | listening | thinking | speaking | acting`) are CSS overlays on the image
  (`.orb-eyes`, `.orb-wave`, `.orb-bubble`, drop-shadow filters), not separate images.

### 9A Home (`#p-home`)
*Superseded by section 11: the scaled pixel canvas below was replaced by a responsive grid.*
The home page is a 1582×866 pixel canvas (`.cc-canvas`) positioned at the mock-up's
coordinates (content origin = 90 px rail, 75 px header) and scaled down by `layoutHome()`
when the viewport is narrower; below 1024 px it becomes a normal stacked flow. Checked item by
item against the mock-up side by side at 1672×941 and at 390 px:
1. background scene behind the right half, dimmed on the left;
2. orb image on the right with bubble (last assistant line) and waveform, nothing crossing it;
3. Today's Brief (3 items, "Show all" expands) and Progress side by side, equal height;
4. centred glowing pill talk bar: mic on the left (tap = live voice, lit while a session is
   on), waveform / send on the right, no LIVE button or READY chip. The dock is reparented
   into the canvas on home (`placeDock()`) and back to its fixed position elsewhere;
5. suggestions as a small chip above the pill that expands on tap (`toggleSuggestions`);
6. six tiles in one row (Business, Academics, Projects, Content, Applications, Agents); only
   Academics is live, the others are dimmed "Coming soon";
7. sidebar rail 90 px and header 75 px with the mock-up's type sizes.

### 9B Stage view
The stage panel follows the Business Metrics layout: back arrow, icon + title, tabs, body.
Syllabus view: tabs (Overview + one per paper), four KPI cards (overall progress with bar,
topics bulletproof, topics not started, days to the next exam), the topic list
(tap-to-advance), then "Next best actions" (the weakest topics from
`GET /syllabus/weakest`, filtered to the shown subject/paper, tap to advance) beside an
"Insights" panel. Other views keep their Stage 8 renderers inside the new frame.
Layout rules: the panel always ends 140 px above the bottom (110 px on small screens) so the
dock never overlaps it; in view mode the orb has a 400 px column (bubble and waveform hidden,
image edges masked over the blurred backdrop); below 1000 px the stage is one column and the
Close button gives way to the back arrow. Voice-only mode is unchanged: the orb is centred
and full-size with the transcript beneath it; the two-column layout appears only while a
view is open and closing the view returns to the centred orb.

### Verification
`python -m pytest -q` → 431 passed (OpenAI mocked, no key). `node tests/e2e/smoke.js` → all
checks pass (the orb-home check now looks for `#cc-canvas .orb-stage`). Screenshots were taken
at 1672×941, 1280×800 and 390×844 in headless Chromium.

### Known gaps after Stage 9
- Pixel likeness was judged by eye from side-by-side renders, not by an automated image diff.
- The five "Coming soon" tiles are placeholders (phases 5–6).
- The syllabus KPIs and insights are computed client-side from `GET /syllabus` and
  `GET /syllabus/weakest`; there is no dedicated endpoint.

## 11. UI consistency pass (branch `ui-consistency`)

One commit per part. The frontend now has one layout system instead of two: the home page is no
longer a scaled 1672×941 frame, and every page shares the same shell and components.

### Part 1 — design system and shell
- Tokens in one `:root` block: fluid `clamp()` type scale `--fs-2xs` … `--fs-display` (body is
  `--fs-base`, 14–17px), spacing `--sp-1` … `--sp-10`, radii `--r-sm` … `--r-pill`,
  `--control-h`, `--row-h`, and the shell geometry `--header-h`, `--rail-w`, `--page-pad`,
  `--content-max`, `--dock-h`, `--dock-clear`.
- Components in the command centre's glass style: page head, card, list row, check, field,
  input/select/textarea, button, toggle, segmented control, badge, bar/meter, KPI tile, chip,
  table, modal, toast. The Design Guide page renders the live token values and every component.
- `layoutHome()`, `#frame` and `#cc-bleed` are gone. `#content` is the only scroll container and
  every page pads its bottom by `--dock-clear`.
- The rail is collapsed by default; the menu button opens it over the content with a scrim and
  it closes after navigating, on Esc or on a scrim tap.

### Part 2 — home
A CSS grid with the mock-up's composition. The scene is anchored top-right at the height under
the header (`--scene-h`); the orb overlay is placed in % of that box at the scene's own
coordinates, its bubble and wave sized in container units. Rows are spaced in the same unit, so
1280×720 and 1920×1080 both read as the mock-up with no scrolling. A blurred cover copy of the
scene fills the page; the mock card baked into the scene's left edge is masked out. Below 1024px
or in portrait windows the home stacks and the talk bar returns to the viewport bottom.

### Part 3 — every other page
Timetable, Health, Syllabus, War Room, Annual Targets, Agents, Settings, the stage views, the
login gate, alerts panel and import dialog use only the shared components. Every legacy style
block is deleted; no inline styles remain except runtime values. The Dashboard and Academic
Roadmap pages and their frontend code were deleted (with the hidden log page and the dead
brain-dump/chat/radar code); their backend tables and endpoints are unchanged.

### Part 4 — phone
Below 768px the rail is a drawer behind the header hamburger, every page is one column, the talk
bar spans the width, and inputs are 16px so iOS does not zoom.

### Part 5 — app icon and one host
`frontend/icons/` (triangle mark on the dark base: `icon.svg` source, 192/512 PNGs, a maskable
512, `apple-touch-icon.png`, `favicon-32.png`) and `frontend/manifest.json` (`display:
standalone`, start URL and scope `./` = `/app/`). The page's API base is now its own origin
whenever it is served over http(s), so Render at `/app/` is the only host; `netlify.toml` is
deleted and the docs describe one host.

### Verification
- `python -m pytest -q` → 447 passed (no OpenAI key).
- `node tests/e2e/smoke.js` → all checks pass (selectors follow the renamed home elements:
  `img.home-scene-img`, `.check`, `#home-orb .orb-stage`).
- `node tests/e2e/screens.js` (new) → screenshots of every page plus a stage view and the open
  menu at 1280×720, 1536×864, 1920×1080 and 390×844, combined into
  `tests/e2e/out/contact-sheet.jpg`, and passes these checks: computed body font size, header
  height and rail width identical on every page at each size (14.52px / 63.59px / 76.72px at
  1280×720; 17px / 76.39px / 92.08px at 1920×1080; 14px / 56px / drawer at 390×844); no
  horizontal overflow anywhere; every interactive control scrolled to the middle of the page
  sits above the talk bar; the menu opens and closes after navigating; no page errors.
- Both scripts accept `PW_CHANNEL=msedge` (or `chrome`) to run on an installed browser.

### Known gaps
- Likeness to the mock-up is judged by eye from the contact sheet, not by an image diff.
- On very tall, narrow desktop windows (portrait) the home uses the stacked layout rather than
  the mock-up's composition.

## 12. Assistant reads current data only (branch `assistant-current-data`)

Asked "What do you know?", the assistant used to quote old subject mastery (Maths 94.5%), raw
habit booleans, `roadmap_tasks` ("Founder OS 7/7") and the $10K scoreboard. The sources were:
the frontend's `context` object (legacy KPI store overwritten from `/subjects/progress`, a raw
`{key: true/false}` habit map, annual-target triples) injected as `[Live system context]`;
`get_dashboard_state` (`_kpi_state`, raw habits, `ten_k_progress`); `synthesize_weekly_review`
(every done `roadmap_tasks` row, mixed-currency total labelled USD); `route_brain_dump`
(`roadmap_tasks` ids into the parser); the parser schema (`roadmap_complete`, `kpi_updates`, a
hard-coded habit list); the briefing's business % of $10K; and the prompt's $10,000 milestone.

### Now
- `backend/assistant_state.py` — academics from `syllabus_points`, routine from
  `non_negotiables` (ticks in `habits`, read only here), targets from `annual_targets`, revenue
  in Kwacha (USD × `ZMW_PER_USD`). Everything phrased in labels and words.
- Tools: `get_dashboard_state` returns plain-language sections (academics, non-negotiables
  today, annual targets, revenue, tasks, upcoming exams, alerts); `synthesize_weekly_review`
  ships completed tasks, revised syllabus topics and non-negotiable completion, with revenue in
  Kwacha; `surface_weakest_topics`, `get_deadlines` and `detect_off_track` return sentences
  (`detect_off_track()` and `get_deadlines()` keep their structured shapes for the briefing and
  `GET /deadlines`; the tool registry calls `off_track_report` / `deadlines_report`).
- `orchestrator_tools.for_model(name, result)` is what the model sees from every tool, typed and
  voice: per-tool phrasing, then yes/no for booleans, words for status codes, no nulls, no
  underscores in keys, no `ui_directive`.
- Parser: told the active non-negotiables (key = label); `roadmap_complete`, `kpi_updates` and
  `roadmap_id` are gone from the schema (`apply_parse_updates` still tolerates them). Ticks go
  through `assistant_state.mark_non_negotiable_done`, which creates the day's row when missing —
  before, logging only matched rows seeded on the day the server started.
- Briefing: `progress.business` is the revenue annual target's progress, or `null`.
- The frontend no longer sends `context`; the server ignores it from older clients.
- Prompt: revenue tracked in Kwacha; status questions must call `get_dashboard_state` and never
  quote numbers from history; `show_view history` for past conversations.

### Conversations
- `GET /conversation/history?q=&before_id=&limit=200&tz_offset=` → `{query, days:[{date, label,
  messages:[{id, role, text, time, voice, changes}]}], count, has_more, next_before_id}`. User and
  assistant text only (no tool rows or empty tool-call turns), grouped by the viewer's local day
  (`tz_offset` is JavaScript's `getTimezoneOffset()`), newest day first, messages in order within
  a day; `q` is case-insensitive; `before_id` pages back.
- Frontend: a Conversations page on the rail and the `history` stage view share `historyView()`:
  search box (250 ms debounce), a card per day, matches highlighted, "Load older messages". New
  component `.msg` (shown in the Design Guide).

### Verification
- `tests/test_assistant_current_data.py` (21 tests): an SQL listener runs every registered tool,
  the briefing and the context build against seeded legacy data and fails on any read of a legacy
  table, any `habits` read outside `assistant_state` and any `subjects` read outside `syllabus`
  (a self-test proves the guard fires); every tool's model text has no booleans, nulls, codes or
  snake_case keys; Kwacha conversion; ticking on a day with no rows; client context ignored;
  history grouping, search, paging, time zones, auth and the `history` view.
- Backend suite 468 passed; smoke tests pass; `screens.js` (now including Conversations) passes
  at 1280×720, 1536×864, 1920×1080 and 390×844.

### Known gaps
- Old messages and the rolling summary still contain the numbers quoted before this change; the
  prompt tells the model not to repeat figures it did not get from a tool this turn.
- Legacy endpoints (`/subjects/progress`, `/kpis`, `/roadmap/*`, `/radar`, `/scoreboard/ten-k`) still
  exist for compatibility; nothing the assistant reads calls them.
