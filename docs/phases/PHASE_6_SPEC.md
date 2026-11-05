# Phase 6 — Long-term goals, agents, integrations, proactive communication

Status: specification, ready to build after Phase 5 (6B and 6D can start once 5B/5C
exist; 6A and 6C have no Phase 5 dependency beyond the shared conventions). Builder:
Opus-sized sessions following `docs/phases/BUILD_ORDER.md`. Patterns are defined in
`docs/CONVENTIONS.md`; decisions are recorded in `docs/adr/`. Anything that depends on a
third party is marked **VERIFY** with its fallback.

Modules, in build order:

- **6B** Agents — registry, permissions, scheduled and event-triggered runs, activity log,
  and the one mechanism that lets anything run while the app is closed (ADR-0001).
- **6D** Proactive communication — scheduled briefings and alerts delivered to the phone
  over Telegram, with two-way replies into the orchestrator (ADR-0002).
- **6C** Integrations — Google Calendar (OAuth), email (IMAP/SMTP with an app password),
  Telegram pairing, and the Settings → Integrations card. Social platforms are 5D's
  adapters and ADR-0004.
- **6A** Applications — Wharton and anything like it: requirements, missing components,
  deadlines, profile gaps.

Why 6B first: the morning briefing, alerts, calendar sync, email sync, brand sync,
recurring-payment generation and scheduled research are all agents. Without the runner
and the external tick nothing runs when the Render process is asleep.

---

## 0. Decisions that apply to the whole phase

### 0.1 Time
All schedules are in the user's zone, `FOUNDER_OS_TZ` (default `Africa/Lusaka`, UTC+2,
no DST), via `zoneinfo`. Stored timestamps stay naive UTC as everywhere else; conversion
happens only in `agents.schedule` and in notification text.

### 0.2 Outbound actions
Anything that leaves the system (a message to a human, a remote calendar write) is one
of two kinds:
- **Reversible → act, log, undo.** Creating a calendar event is reversible (the undo hook
  deletes it). Notifications to the user themself are not gated.
- **Irreversible to a third party → pending action.** Sending outreach (5B) stays behind
  `pending_actions`. Agents never confirm pending actions; only the user does.

### 0.3 Secrets
Third-party tokens live in `integrations.secret_json` as plain text in the database
(Postgres, RLS on, backend-only connection). Decision: no application-level encryption
in this phase (it would add the `cryptography` dependency and a key-management story for
one user). Mitigations that **are** required: never return `secret_json` from any
endpoint; pass every log line that could contain a token through
`integrations.redact()` (same shape as `vault_sync.redact_credentials`); bot and API
tokens that are static stay in Render env vars, not the database.

### 0.4 What runs where
| Need | Mechanism |
|---|---|
| Something must run even when the app is closed and Render is asleep | GitHub Actions cron → `POST /jobs/tick` (ADR-0001) |
| Something should also run promptly while the process is awake | the existing in-process APScheduler calls the same `tick` (best effort) |
| Something must happen on an event (a reply arrived, a payment went overdue) | the module `emit`s an `agent_events` row; the next tick runs the subscribed agents |
| Something must happen *now* on user request | `POST /agents/{key}/run` or the `run_agent` tool |

Latency floor: **15 minutes** (the cron interval). Nothing in Phase 6 promises faster than
that while the app is closed, and the specs are written so that is acceptable.

### 0.5 Running cost baseline
Hosting stays free (Render free web service, Supabase free, Netlify free, GitHub Actions
free for a public repository). Telegram Bot API is free. Google Calendar API and the Gmail
IMAP/SMTP path are free. Phase 6's own LLM spend is small (table at the end); the agents
it schedules spend what Phase 5 budgeted.

---

## 6B. Agents

### Purpose
One registry of everything that runs on its own: what it is allowed to touch, when it
runs, what it did, what it cost. The market-intelligence agent becomes the first entry;
Phase 5's housekeeping (recurring payments, overdue statuses, brand sync, lead
qualification, portfolio review) and Phase 6's couriers (briefing, alerts, calendar and
email sync) are the rest. Agents are **plain Python modules run sequentially by one
runner** — no queue, no worker process, no framework (ADR-0003).

### Friction
- Agents are discovered, not configured: each ships with a default schedule and
  permissions in code. The Agents page shows them with one tap **Enable/Disable** and
  one tap **Run now**. Schedule overrides come from conversation ("run the portfolio
  review on Sundays at 7") → `set_agent_schedule`.
- Everything an agent writes shows up where the user already looks: briefing items,
  suggestions, changes with Undo (grouped under the agent's batch), notifications.
- Agents never ask questions. If an agent cannot decide, it writes a suggestion.

### Architecture — `backend/agents/`

```
agents/
  __init__.py      REGISTRY: dict[str, AgentSpec]; emit(); import of every agent module
  base.py          AgentSpec, AgentContext, AgentResult, Schedule, Permissions
  runner.py        tick(), run_agent(), permission enforcement, cost attribution
  schedule.py      due(spec_or_row, now_local) → bool
  market_intel.py  run due research_objectives (5A)
  crm_housekeeper.py  generate_due, refresh_statuses, reconcile, client_health (5C); emits payment.overdue
  lead_qualifier.py   qualify up to 20 'new' leads with contact details (5B)
  follow_up_sentinel.py  contacted-no-reply > 7d → ensure follow-up todo; emits lead.stale
  portfolio_reviewer.py  portfolio.review → pause suggestions, health (5E)
  brand_sync.py       sync every connected social account (5D)
  calendar_sync.py    (6C)  email_sync.py (6C)
  briefing_courier.py alert_courier.py reminder_courier.py (6D)
  application_watch.py (6A)
```

```python
@dataclass
class Schedule:
    interval_minutes: int | None = None      # run when now - last_run >= interval
    at_time: str | None = None               # "06:00" local; run once per day after this time
    weekdays: list[int] | None = None        # 0=Mon … 6=Sun; None = every day
    min_gap_minutes: int = 10                # never run twice inside this gap (double ticks)

@dataclass
class Permissions:
    tools: list[str]                 # orchestrator tool names the agent may call via ctx.run_tool (read tools mostly)
    tables_write: list[str]          # tables the agent's change_log entries may touch
    may_notify: bool = False         # may call ctx.notify (6D)
    may_emit: list[str] = field(default_factory=list)   # event kinds it may emit
    max_llm_calls: int = 20          # hard cap per run, enforced by llm.log_usage via ctx
    max_seconds: int = 60

@dataclass
class AgentSpec:
    key: str; name: str; description: str
    schedule: Schedule
    events: list[str]                # event kinds that trigger it
    permissions: Permissions
    run: Callable[[AgentContext], AgentResult]
    enabled_by_default: bool = True
    phase: str = "5"                 # for display

@dataclass
class AgentContext:
    db: Session; now: datetime; now_local: datetime; today: date
    run: models.AgentRun; spec: AgentSpec; row: models.Agent
    events: list[models.AgentEvent]  # the events that triggered this run (empty for schedule)
    config: dict                     # row.config_json merged over spec defaults
    def run_tool(self, name, args) -> tuple[dict, str]   # allow-listed, never raises
    def notify(self, *, kind, title, body, dedupe_key, send_at=None) -> dict  # 6D; refused unless may_notify
    def emit(self, kind, payload: dict) -> None          # refused unless kind in may_emit
    def log(self, line: str) -> None                     # appended to run.log_text (≤ 20 KB)

@dataclass
class AgentResult:
    summary: str                     # one human line
    result: dict = field(default_factory=dict)
    status: str = "completed"        # completed | skipped (nothing to do)
```

**Runner — `runner.run_agent(spec, row, trigger, events)`** (each agent gets its own
`SessionLocal()`):

1. Insert `agent_runs` row (`status=running`, `trigger`, `started_at`), commit.
2. `change_log.begin_batch(db, f"agent:{spec.key}")`; `db.info["agent_run_id"] = run.id`;
   `llm.set_run_context(run.id, max_calls=spec.permissions.max_llm_calls)` (see cost
   attribution below).
3. Call `spec.run(ctx)` inside `try`. A `TimeoutError`-style soft limit: the runner
   checks elapsed time **after** the agent returns and marks `status="completed"` with
   `over_time=True` when it exceeded `max_seconds` (no thread killing; agents are written
   to bound their own work with the caps in their spec).
4. **Permission audit**: every `change_log` row in this batch whose `table_name` is not
   in `permissions.tables_write` → `undo_batch(db, batch_id)`, `status="violation"`,
   `error="wrote to <table>"`. The agent's work is reverted entirely. (This is the
   enforcement; the allow-list in `run_tool` is the first line.)
5. `end_batch`; write `finished_at`, `status`, `summary`, `result_json`, `error`,
   `llm_calls`, `cost_usd` (sum of `llm_usage` where `agent_run_id = run.id`),
   `changes_count`, `batch_id`; update `agents.last_run_at/last_status/last_summary`.
6. Any exception → `status="failed"`, `error` = `f"{type}: {msg}"[:2000]`, batch ended,
   **no undo** of partial work (partial work is logged and undoable by the user; the
   permission audit still runs first). Runner never raises.

**Tick — `runner.tick(trigger: str, *, now=None) -> dict`**:

1. Insert `job_ticks` row. Load unconsumed `agent_events`.
2. For each spec in `REGISTRY` (stable order: couriers last so they see this tick's
   results): load/create its `agents` row; skip if `not row.enabled`; decide
   `due = schedule.due(spec, row, now_local)` or `events` matching `spec.events`
   (non-empty intersection). Skip if `row.last_run_at` within `min_gap_minutes`.
3. Run due agents **sequentially**; stop starting new ones after `TICK_BUDGET_SECONDS`
   (default 80; env `JOBS_TICK_BUDGET`) so the HTTP call returns inside the caller's
   timeout; unfinished agents simply run next tick.
4. Mark the events delivered to at least one agent `consumed_at=now`; events older than
   24 h are consumed unconditionally (dropped) with `dropped=True`.
5. Finish the `job_ticks` row: `agents_run`, `agents_skipped`, `errors`, `duration_ms`.
   Return the summary dict.

**Cost attribution** — changes to `llm.py` (small, backwards compatible):
- `log_usage(..., agent_run_id=None)`: if `agent_run_id` is None it reads
  `db.info.get("agent_run_id")` when `db` is given, else `llm.current_run_id()`.
- `set_run_context(run_id, max_calls)` / `clear_run_context()` store a module-level
  `_RUN_CONTEXT` (process-wide; runs are sequential; an assistant turn served during an
  agent run would be mis-attributed — accepted, documented, cost-only).
- `chat_completion`/`chat_text`/`embed_texts` increment the context's call counter and
  raise `llm.BudgetExceeded` when `max_calls` is hit; agents catch it as a normal failure.
- `llm_usage.agent_run_id INTEGER` column (m020), index.

**Events — `agents.emit(db, kind, payload)`** inserts `agent_events(kind, payload_json,
created_at)`; modules call it inside their own transactions. Kinds in this phase:
`lead.replied` (5B/6C), `lead.stale`, `payment.overdue` (crm_housekeeper),
`research.completed` (5A), `email.received` (6C), `calendar.changed` (6C),
`application.deadline_near` (6A), `brand.sync_failed` (5D). Payload is a small dict with
ids; agents re-read the database.

**Schedule semantics — `schedule.due(spec, row, now_local)`**:
- `at_time` set: due if `now_local.time() >= at_time` and (`last_run_at` is None or
  `last_run_at` local date < today) and (`weekdays` is None or `now_local.weekday()` in it).
- else `interval_minutes` set: due if `last_run_at` is None or `now - last_run_at >=
  interval`.
- `row.interval_minutes`, `row.at_time`, `row.weekdays_json` override the spec when set.

### Agents shipped in this phase (defaults)

| key | schedule | events | tables_write | LLM cap | notify |
|---|---|---|---|---|---|
| `crm_housekeeper` | every 60 min | — | payments, recurring_revenue, clients, projects | 0 | no |
| `lead_qualifier` | every 180 min | — | leads | 20 | no |
| `follow_up_sentinel` | at 05:30 daily | `lead.replied` | todos, leads | 0 | no |
| `portfolio_reviewer` | at 05:40 daily | — | projects, suggestions | 0 | no |
| `brand_sync` | at 04:00 daily | — | social_accounts, content_posts, content_metrics, account_metrics | 0 | no |
| `market_intel` | every 60 min (runs only objectives that are `due`) | — | research_projects, research_findings, research_objectives, research_memory_notes, embeddings | 12 | no |
| `calendar_sync` (6C) | every 30 min | — | calendar_events, integrations | 0 | no |
| `email_sync` (6C) | every 30 min | — | email_messages, communications, leads, todos, integrations | 0 | no |
| `application_watch` (6A) | at 05:45 daily | — | application_gaps, applications | 2 | no |
| `briefing_courier` (6D) | every 15 min; sends once a day once `now_local >= settings.morning_time` (dedupe key) | — | notifications, briefing_summaries | 1 | yes |
| `alert_courier` (6D) | every 15 min | `payment.overdue`, `lead.replied`, `application.deadline_near`, `brand.sync_failed` | notifications | 0 | yes |
| `reminder_courier` (6D) | every 15 min | — | notifications | 0 | yes |

`market_intel` runs at most **one** objective per tick (the most overdue) so a tick never
exceeds its budget; it reports the others as `deferred`.

### Tables (migration `m020_agents.py`)

`agents`

| Column | Type | Notes |
|---|---|---|
| `id` PK; `key` VARCHAR NOT NULL UNIQUE | | matches `AgentSpec.key` |
| `enabled` BOOLEAN NOT NULL DEFAULT TRUE | | |
| `interval_minutes` INTEGER; `at_time` VARCHAR; `weekdays_json` TEXT | | overrides, nullable |
| `config_json` TEXT | | agent-specific settings |
| `last_run_at` TIMESTAMP; `last_status` VARCHAR; `last_summary` VARCHAR | | |
| `runs_count` INTEGER DEFAULT 0; `cost_usd_total` REAL DEFAULT 0 | | |
| `created_at`, `updated_at` TIMESTAMP | | |

`agent_runs`

| Column | Type |
|---|---|
| `id` PK; `agent_key` VARCHAR NOT NULL; `trigger` VARCHAR NOT NULL (`schedule|event|manual|in_process`); `tick_id` INTEGER; `started_at` TIMESTAMP NOT NULL; `finished_at` TIMESTAMP; `status` VARCHAR NOT NULL (`running|completed|skipped|failed|violation`); `summary` VARCHAR; `result_json` TEXT; `error` TEXT; `log_text` TEXT; `llm_calls` INTEGER DEFAULT 0; `cost_usd` REAL DEFAULT 0; `changes_count` INTEGER DEFAULT 0; `batch_id` VARCHAR; `over_time` BOOLEAN DEFAULT FALSE |

`agent_events`

| Column | Type |
|---|---|
| `id` PK; `kind` VARCHAR NOT NULL; `payload_json` TEXT; `created_at` TIMESTAMP NOT NULL; `consumed_at` TIMESTAMP; `dropped` BOOLEAN DEFAULT FALSE; `source` VARCHAR |

`job_ticks`

| Column | Type |
|---|---|
| `id` PK; `trigger` VARCHAR NOT NULL (`github|manual|in_process`); `started_at`, `finished_at` TIMESTAMP; `agents_run` INTEGER; `agents_skipped` INTEGER; `events_consumed` INTEGER; `errors` INTEGER; `duration_ms` INTEGER; `summary_json` TEXT |

`llm_usage` gains `agent_run_id INTEGER`. Indexes: `ix_agent_runs_key_started`,
`ix_agent_events_consumed`, `ix_llm_usage_agent_run`. RLS on the four new tables.
Backfill: `agents` rows are created lazily by the first tick (and by `GET /agents`), not by
the migration, so the registry stays the source of truth.

### Endpoints — `backend/agents/api.py` (`router`)

| Method & path | Body / query | Returns |
|---|---|---|
| `GET /agents` | | `[{key, name, description, phase, enabled, schedule:{interval_minutes, at_time, weekdays, effective:"every 60 min"/"daily at 06:00"}, permissions, last_run:{at, status, summary, cost_usd}, runs_count, cost_usd_total, next_due_estimate}]` |
| `GET /agents/{key}` | | the above + `config` + last 20 runs |
| `PATCH /agents/{key}` | `{enabled?, interval_minutes?, at_time?, weekdays?, config?}` | agent + `change` (the `agents` row is change-logged so a toggle is undoable) |
| `POST /agents/{key}/run` | | runs now (`trigger=manual`) → the `agent_runs` row serialised |
| `GET /agents/{key}/runs` | `limit=20` | runs |
| `GET /agents/runs` | `limit=50`, `status?` | activity log across agents, newest first, each with `changes` (from `change_log` by `batch_id`, max 10) |
| `POST /agents/runs/{id}/undo` | | `change_log.undo_batch(run.batch_id)` |
| `GET /jobs/status` | | `{last_tick:{...}, ticks_24h, agents_due_now:[keys], token_configured: bool, in_process_scheduler: bool}` |
| `POST /jobs/tick` | header `X-Jobs-Token` | `tick("github")` result; **401** on bad/missing token; **503** when `JOBS_TOKEN` is unset |

`/jobs/tick` is added to `auth.EXEMPT_EXACT`; its own token check is the gate. Compare
with `hmac.compare_digest`. Token must be ≥ 32 chars or startup logs a warning.

### Orchestrator tools

| Tool | Args | Behaviour |
|---|---|---|
| `agent_status` | `key?` | read-only summary of agents and last runs (what the model uses to answer "did the research run?") |
| `run_agent` | `key` (req) | explicit instruction only; runs now; summary = the run's summary |
| `set_agent_schedule` | `key` (req), `enabled?`, `interval_minutes?`, `at_time?`, `weekdays?` | explicit instruction only; PATCH semantics; logged |

Prompt section "AGENTS": agents run on their own; report what they did from
`agent_status`; change schedules only on instruction; never enable/disable on your own
judgement.

### External trigger — `.github/workflows/jobs-tick.yml`

```yaml
name: Founder OS jobs tick
on:
  schedule:
    - cron: "*/15 * * * *"
  workflow_dispatch: {}
jobs:
  tick:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - name: Tick (wakes Render if asleep)
        run: |
          for i in 1 2 3; do
            curl -fsS -m 120 -X POST "${{ secrets.FOUNDER_OS_URL }}/jobs/tick" \
              -H "X-Jobs-Token: ${{ secrets.JOBS_TOKEN }}" && exit 0
            sleep 20
          done
          exit 1
```
Repository secrets: `FOUNDER_OS_URL` (`https://founder-os-backend-f6qn.onrender.com`),
`JOBS_TOKEN` (same value as the Render env var). **VERIFY**: GitHub's minimum schedule is
5 minutes, fires can be delayed by many minutes under load, and scheduled workflows are
**disabled automatically after 60 days without repository activity** on public repos (a
commit re-enables; the Agents page shows `last_tick` age so a silent stop is visible).

In-process: `main.start_vault_scheduler` adds
`scheduler.add_job(lambda: runner.tick("in_process"), "interval", minutes=15,
id="jobs_tick")` guarded by env `JOBS_IN_PROCESS=1` (default on). `min_gap_minutes`
stops the two paths double-running.

### Frontend (Agents page rework)
- Registry list from `GET /agents`: name, phase chip, effective schedule, last run status
  chip (`completed` green / `failed` red / `violation` red / `skipped` muted), cost this
  month, **Enable** toggle (PATCH), **Run** (POST). Tap a row → runs list with summary,
  duration, cost, and **Undo** per run with changes.
- The market-intelligence detail (projects, pipeline, lessons) stays as the detail view
  of that agent.
- A "Scheduler" strip at the top: last tick time and trigger, `token_configured`,
  "GitHub Actions not seen for N h" warning when `last_tick` older than 60 min.

### Acceptance tests — `tests/test_phase6b_agents.py`
1. A fake agent registered in the test (`REGISTRY` patched) runs via `tick("manual")`:
   `agent_runs` row completed, `summary`, `batch_id`, `agents` row created with
   `last_run_at`.
2. Permission violation: a fake agent writing to `todos` with `tables_write=["notes"]` →
   run status `violation`, the todo is gone (batch undone), error names the table.
3. Schedule: `at_time="06:00"` is not due at 05:59 local, due at 06:00, not due again the
   same day after running; `interval_minutes=60` due after 61 min; `min_gap_minutes` skips
   a second tick 2 min later; a `weekdays=[6]` agent runs only on Sunday (patch `now`).
4. Events: `emit("lead.replied")` → the subscribed fake agent runs with `ctx.events` of
   length 1; event `consumed_at` set; a 25-hour-old event is dropped.
5. Cost attribution: patched `llm.chat_text` inside an agent logs a `llm_usage` row with
   `agent_run_id`; `max_llm_calls=1` → second call raises `BudgetExceeded`, run `failed`.
6. Tick budget: with `JOBS_TICK_BUDGET=0` only the first due agent runs; the second is
   reported `deferred`.
7. `/jobs/tick`: 503 without `JOBS_TOKEN`, 401 with a wrong token, 200 with the right one
   and no bearer token (auth exemption).
8. `PATCH /agents/{key}` toggles `enabled` with a change entry; undo re-enables.
9. `crm_housekeeper` run on a fixture generates due payments, flips overdue, emits
   `payment.overdue` once per payment (dedupe by payload id within 24 h).
10. `market_intel` agent runs exactly one due objective per tick (mocked agent).
11. Startup test still green with m020 registered; `agents` rows absent until first tick.

### Dependencies
Phase 5 modules for the shipped agents (each agent module imports lazily and is skipped
with `status=skipped` if its module is absent — so 6B can be built before all of Phase 5
is merged).

### Estimated running cost
Runner: none. GitHub Actions: ~2,900 runs/month × ~10 s, free on a public repository.
Agents spend their modules' budgets (Phase 5 totals ≈ $0.65/month).

### External accounts, approvals, payments
GitHub repository secrets (free). Render env vars `JOBS_TOKEN`, `FOUNDER_OS_TZ`,
`JOBS_TICK_BUDGET`, `JOBS_IN_PROCESS`. **VERIFY** the GitHub Actions caveats above and
Render free-tier request timeout (believed 100 s; the tick budget of 80 s is chosen under
it).

---

## 6D. Proactive communication

### Purpose
The system reaches out when something needs attention, while the app is closed: a
morning briefing, alerts that cross a threshold, deadline countdowns, follow-up
reminders, and reminders the user asked for. Replies go straight back into the
orchestrator. Channel: **Telegram bot** (ADR-0002); Web Push is the documented second
channel, not built here.

### Friction
- Pair once: open the bot link shown in Settings → Integrations, send the six-character
  pairing code shown there. Done.
- Everything else is automatic and governed by one settings card (morning time, evening
  review on/off, quiet hours, minimum alert severity, daily cap) — five controls, each a
  single select or time field, changed rarely. Conversation also works: "send the briefing
  at 5:45" → `notification_settings` tool.
- Reply to any message in Telegram as if typing in the dock. `/undo` reverses the last
  change the bot made. `/yes` confirms the pending action the bot just asked about.
  `/brief` sends the briefing now.

### Tables (migration `m022_notifications.py`)

`notifications`

| Column | Type | Notes |
|---|---|---|
| `id` PK; `created_at` TIMESTAMP | | |
| `channel` VARCHAR NOT NULL DEFAULT 'telegram' | | |
| `kind` VARCHAR NOT NULL | | `briefing|evening|alert|deadline|reminder|reply|test` |
| `title` VARCHAR; `body` TEXT NOT NULL | | plain text |
| `dedupe_key` VARCHAR UNIQUE | | e.g. `briefing:2026-11-05`, `alert:no_revenue_30d:2026-11-05` |
| `send_at` TIMESTAMP | | null = now |
| `status` VARCHAR NOT NULL DEFAULT 'queued' | | `queued|sent|failed|suppressed|cancelled` |
| `sent_at` TIMESTAMP; `error` TEXT; `external_id` VARCHAR | | |
| `agent_run_id` INTEGER; `source` VARCHAR | | |
| `suppressed_reason` VARCHAR | | `quiet_hours|daily_cap|channel_disabled|not_paired` |

`notification_settings` (exactly one row, `id=1`, created by the migration)

| Column | Type | Default |
|---|---|---|
| `enabled` BOOLEAN | TRUE |
| `morning_time` VARCHAR | `'06:00'` |
| `evening_time` VARCHAR | NULL (off) |
| `quiet_start` VARCHAR | `'22:00'` |
| `quiet_end` VARCHAR | `'05:00'` |
| `alert_min_severity` VARCHAR | `'warn'` (`info|warn|critical`) |
| `daily_cap` INTEGER | `12` |
| `channels_json` TEXT | `{"telegram": true}` |
| `deadline_days_json` TEXT | `[30, 14, 7, 3, 1]` |
| `updated_at` TIMESTAMP | |

Pairing state lives in `integrations` (6C) under provider `telegram`:
`config_json = {"chat_id": …, "username": …}`. RLS on both tables.

### Core — `backend/notify.py`

```python
def settings(db) -> models.NotificationSettings          # get-or-create row 1
def is_quiet(now_local, s) -> bool                        # handles ranges crossing midnight
def sent_today(db, today) -> int
def queue(db, *, kind, body, title=None, dedupe_key=None, send_at=None, source=None, agent_run_id=None) -> dict
    # inserts queued (or returns existing by dedupe_key with "deduped": True); never sends directly
def dispatch(db, now=None, *, force=False) -> dict
    # for each queued row with send_at <= now:
    #   not paired / channel disabled → suppressed
    #   quiet hours (unless kind=="reply" or force) → leave queued (send_at = quiet_end today/tomorrow)
    #   daily cap reached (kinds other than briefing/reply) → suppressed (daily_cap)
    #   else channel.send → sent / failed (one retry on the next dispatch; failed after 3 attempts: attempts counter in error text)
def render_briefing(briefing: dict, *, suppressed_overnight: list[dict]) -> str   # plain text, ≤ 3,500 chars
def send_test(db) -> dict
```
Channels: `TelegramChannel.send(chat_id, text) -> {"ok", "external_id", "error"}` posts
to `https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage` with `chat_id`, `text`,
`disable_web_page_preview=True`; messages over 4,000 chars are split on blank lines.
`httpx.post` with 10 s timeout; any exception → `ok=False`. **VERIFY** the method name and
limits against the Bot API reference (`sendMessage`, 4096 chars).

Briefing text format (deterministic, from `build_briefing(with_summary=True)` which
reuses the cached daily summary — no extra LLM call):

```
Good morning. Wednesday 5 November · EXECUTION DAY
{greeting_line}

ATTENTION (3)
• 2d overdue — Email the insurance CEO
• Today — Business essay plan
• CRITICAL — No revenue logged in 30 days

TODAY
• 09:00 Further Maths P1 past paper (calendar)
• Non-negotiables: 0/5

EXAMS  Further Maths P1 in 19 days · Economics P2 in 26 days

OVERNIGHT (suppressed): • Payment from Wamu's is 15 days overdue

Reply here to log anything. /brief resend · /undo last change
```
Sections appear only when non-empty; at most 6 attention items and 5 today items.

### Couriers (agents, registered in 6B)
- `briefing_courier` — runs every 15 minutes (the time lives in `notification_settings`,
  not in the spec's static schedule); when `now_local.time() >= morning_time` it calls
  `queue(kind="briefing", dedupe_key=f"briefing:{today}")` then `dispatch` — the dedupe
  key makes the second and later runs of the day no-ops. With `evening_time` set, a second entry `evening_courier`-style run
  inside the same module: carried-over tasks + "What did you get done? Reply here."
  (`dedupe_key=f"evening:{today}"`).
- `alert_courier` — every tick and on events: new alerts from `detect_off_track` with
  severity ≥ `alert_min_severity` → `dedupe_key=f"alert:{rule_id}:{today}"`;
  `payment.overdue` events → `payment:{id}:{today}`; `lead.replied` → `lead_replied:{id}`;
  `application.deadline_near` → `deadline:application:{id}:{days}`; exams at the configured
  day marks → `deadline:exam:{code}:{paper}:{days}`; `brand.sync_failed` → one per account
  per day. All go through `queue` then `dispatch`.
- `reminder_courier` — every tick: `dispatch()` only (sends due `reminder` rows and
  anything left queued by quiet hours).

### Two-way — `backend/telegram_inbound.py`

`POST /integrations/telegram/webhook` (auth-exempt; verified by header
`X-Telegram-Bot-Api-Secret-Token == TELEGRAM_WEBHOOK_SECRET`, else 403 — **VERIFY** the
header name set by `setWebhook(secret_token=…)`). Body: Telegram `Update`.
1. Not paired and text equals the pairing code → store `chat_id` in `integrations`,
   reply "Paired. You'll get your briefing at 06:00." Pairing code =
   `hmac(FOUNDER_OS_SECRET, "telegram-pair")` → 6 uppercase base32 chars, shown in Settings.
2. Paired and `chat.id != stored` → ignore (log count only).
3. Commands: `/brief` → briefing now; `/undo` → `undo_batch` of the newest `change_log`
   batch with source `telegram` in the last 24 h, reply with what was restored; `/yes` →
   `confirm_action` on the newest pending action created by a telegram turn within 24 h
   (none → "Nothing is waiting for a yes"); `/no` → cancel it; `/mute 2h` → sets a
   temporary quiet window (`settings.muted_until`, add the column).
4. Anything else → `orchestrator.run_turn(text, db, source="telegram",
   session_id=f"telegram:{chat_id}")` — the function Phase 6D extracts from
   `orchestrator_stream`: same history, memory, batch, persistence; returns
   `{final, changes, pending, suggestions}`. Reply = `final` + (if changes) `"\nChanged: " +
   "; ".join(summaries) + "\n/undo to reverse"` + (if a pending delete/send was proposed)
   `"\nReply /yes to confirm or /no."`. Replies are sent through `queue(kind="reply")` +
   `dispatch(force=True)` (replies ignore quiet hours and the cap).
5. Respond `200 {}` to Telegram within a few seconds in all cases; the orchestrator turn
   can take 5–20 s — acceptable (**VERIFY** Telegram's webhook timeout; if it retries on
   slow responses, dedupe by `update_id` in a small `telegram_updates` table, add it).

Voice notes: ignored in v1 with a reply "Text only for now" (transcription is a later
addition using the existing `/voice/transcribe` path).

### Endpoints — `backend/notify.py` `router`

| Method & path | Body | Returns |
|---|---|---|
| `GET /notifications/settings` | | settings + `paired: bool`, `pair_code`, `bot_link` (`https://t.me/<TELEGRAM_BOT_USERNAME>`), `channel_configured: bool` |
| `PATCH /notifications/settings` | any settings field | settings + `change` |
| `GET /notifications` | `limit=50`, `status?`, `kind?` | list |
| `POST /notifications/test` | | sends "Founder OS connected" → `{status, error}` |
| `POST /notifications/brief-now` | | queues + dispatches the briefing (`dedupe_key` suffixed `:manual:{hhmm}`) |
| `POST /notifications/{id}/cancel` | | queued → cancelled |
| `POST /integrations/telegram/webhook` | Telegram update | `{}` |

### Orchestrator tools

| Tool | Args | Behaviour |
|---|---|---|
| `set_reminder` | `text` (req), `when` (req; date/time text, same parser as `set_exam_date` extended with `HH:MM`, "in 2 hours", "tomorrow 17:00") | `queue(kind="reminder", send_at=…)`; logged; summary "reminder set for 17:00" |
| `notification_settings` | `morning_time?`, `evening_time?`, `quiet_start?`, `quiet_end?`, `alert_min_severity?`, `daily_cap?`, `enabled?` | PATCH semantics; logged |

Prompt section "REMINDERS AND NOTIFICATIONS": a reminder is for a moment, a task is for a
day — "remind me at 5 to call the clinic" → `set_reminder`; "call the clinic tomorrow" →
a task. Never set reminders the user did not ask for.

### Briefing integration
The briefing is the content. Additions to `build_briefing`: `delivery: {last_sent_at,
channel, paired}` so the home screen can show "Sent to Telegram at 06:00". Suppressed
notifications from the last 24 h appear in the morning text under OVERNIGHT.

### Acceptance tests — `tests/test_phase6d_notify.py` (Telegram `httpx.post` patched)
1. `queue` dedupes by `dedupe_key`; `dispatch` sends once; second dispatch sends nothing.
2. Quiet hours 22:00–05:00: an alert queued at 23:00 is held with `send_at` = 05:00; a
   `reply` is sent immediately; `force` sends.
3. Daily cap: with `daily_cap=2`, the third alert is `suppressed(daily_cap)`; the next
   morning's briefing text lists it under OVERNIGHT.
4. Not paired → `suppressed(not_paired)`; after pairing via webhook with the right code,
   `send_test` succeeds; wrong code does not pair; a second chat id is ignored.
5. Webhook with a wrong secret header → 403; right header → 200.
6. `render_briefing` on a fixture briefing produces the documented sections, ≤ 3,500
   chars, and omits empty sections.
7. `briefing_courier` run at 06:01 local sends once (dedupe) and not again at 06:30;
   `evening_time` unset → no evening message.
8. `alert_courier`: a critical alert produces one notification per rule per day;
   `alert_min_severity=critical` suppresses warn alerts; a `payment.overdue` event
   produces a notification naming the client and amount.
9. Inbound text "finished the business essay" → `run_turn` (scripted stream patched) →
   reply contains the final text and "Changed:"; `/undo` undoes that batch; `/yes` with no
   pending → the "nothing waiting" reply; `/yes` after a proposed delete executes it.
10. `set_reminder("call the clinic", "17:00")` queues a reminder with `send_at` today
    17:00 local (converted to UTC) and `reminder_courier` sends it when due.
11. Message splitting: a 9,000-char body becomes 3 Telegram sends.

### Dependencies
6B (couriers are agents; `tick` calls `dispatch`), 6C (`integrations` row for Telegram),
`orchestrator.run_turn` extraction (done in this module's first session).

### Estimated running cost
Telegram: free. Briefing uses the existing one-per-day summary. Inbound turns cost the
same as dock turns (≈ $0.0013 each). ≈ **$0** additional.

### External accounts, approvals, payments
- Telegram bot via **@BotFather** (free, 2 minutes): token → Render `TELEGRAM_BOT_TOKEN`,
  username → `TELEGRAM_BOT_USERNAME`, a random `TELEGRAM_WEBHOOK_SECRET`. The builder adds
  `POST /integrations/telegram/set-webhook` (authenticated) that calls `setWebhook` with
  the Render URL and the secret so no manual curl is needed.
- **VERIFY**: `setWebhook` `secret_token` parameter and the header it produces; message
  length limit; whether the HTTPS endpoint must answer within a given time.
- No approval, no payment. Web Push (later) would need a VAPID key pair (free) and the
  frontend installed as a PWA on iOS.

---

## 6C. Integrations

### Purpose
Bring the three external systems the day actually runs on into the same database: the
calendar (so the briefing knows what is timed), email (so replies from prospects and
clients are seen and outreach can actually send), and the phone (Telegram pairing, used
by 6D). One `integrations` table, one Settings card, one redaction helper.

### Decisions (ADR-0002, ADR-0004 and this section)
| Provider | Method | Why not the alternative |
|---|---|---|
| Google Calendar | OAuth 2.0, scope `https://www.googleapis.com/auth/calendar.events`, consent screen **published (unverified)** | Testing mode expires refresh tokens after 7 days; the ICS secret URL is read-only. **VERIFY** that an unverified app in production with this *sensitive* (not restricted) scope lets the owner proceed through the warning screen. Fallback: read-only sync from the calendar's private ICS URL (`GOOGLE_CALENDAR_ICS_URL`), no writes. |
| Email | Gmail **IMAP + SMTP with an App Password** (stdlib `imaplib`, `smtplib`, `email`) | Gmail API scopes are *restricted*: verification plus a security assessment. App passwords need only 2-Step Verification. **VERIFY** app passwords are still offered on the account and IMAP is enabled. |
| Telegram | Bot API, webhook | see 6D |
| YouTube | API key (5D) | public data only |

### Tables (migration `m021_integrations.py`)

`integrations`

| Column | Type | Notes |
|---|---|---|
| `id` PK; `provider` VARCHAR NOT NULL UNIQUE | | `google_calendar|email|telegram|youtube` |
| `status` VARCHAR NOT NULL DEFAULT 'disconnected' | | `disconnected|connected|error|pending` |
| `account_label` VARCHAR | | e.g. the Gmail address, the calendar id, the Telegram username |
| `config_json` TEXT | | non-secret settings |
| `secret_json` TEXT | | tokens; never serialised |
| `scopes` VARCHAR | | |
| `connected_at`, `last_sync_at` TIMESTAMP; `last_error` TEXT | | |
| `created_at`, `updated_at` | | |

`calendar_events` (cache)

| Column | Type | Notes |
|---|---|---|
| `id` PK; `provider` VARCHAR DEFAULT 'google'; `calendar_id` VARCHAR; `external_id` VARCHAR NOT NULL | | unique (`provider`, `external_id`) |
| `title` VARCHAR; `description` TEXT (≤ 1,000 chars); `location` VARCHAR | | |
| `start_at`, `end_at` TIMESTAMP (UTC); `all_day` BOOLEAN DEFAULT FALSE; `start_date` DATE | | `start_date` local date for grouping |
| `status` VARCHAR DEFAULT 'confirmed'; `html_link` VARCHAR | | |
| `created_by_us` BOOLEAN DEFAULT FALSE | | events Founder OS created |
| `remote_updated_at` TIMESTAMP; `synced_at` TIMESTAMP; `deleted_at` TIMESTAMP | | soft delete when gone upstream |

`email_messages`

| Column | Type | Notes |
|---|---|---|
| `id` PK; `message_id` VARCHAR NOT NULL UNIQUE; `thread_key` VARCHAR | | `thread_key` = Gmail `X-GM-THRID` when available (**VERIFY**), else `In-Reply-To` |
| `direction` VARCHAR NOT NULL (`in|out`) | | |
| `from_addr`, `to_addr` VARCHAR; `subject` VARCHAR; `snippet` TEXT (≤ 500 chars) | | bodies are **not** stored |
| `received_at` TIMESTAMP | | |
| `lead_id`, `client_id`, `contact_id`, `outreach_message_id` INTEGER | | matched |
| `processed_at` TIMESTAMP; `created_at` | | |

Indexes: `ix_calendar_events_start_date`, `ix_email_messages_received`. RLS on all three.
`DELETABLE`: none of these (deleting cached data is pointless; disconnect instead).

### Google Calendar — `backend/integrations_google.py`

- Env: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `FOUNDER_OS_API_URL` (public backend
  URL for the redirect), `GOOGLE_CALENDAR_ID` default `primary`.
- `GET /integrations/google/start` (authenticated) → `{url}`: `https://accounts.google.com/o/oauth2/v2/auth`
  with `client_id`, `redirect_uri={FOUNDER_OS_API_URL}/integrations/google/callback`,
  `response_type=code`, `scope=https://www.googleapis.com/auth/calendar.events`,
  `access_type=offline`, `prompt=consent`, `state=<HMAC(FOUNDER_OS_SECRET, nonce:ts)>`
  (10-minute validity; nonce stored in `integrations.config_json.pending_state`).
- `GET /integrations/google/callback?code&state` (**auth-exempt**, the browser arrives
  from Google without a bearer header): verify `state`, exchange at
  `https://oauth2.googleapis.com/token`, store `{access_token, refresh_token, expiry}` in
  `secret_json`, `status=connected`, redirect `302` to `{FOUNDER_OS_FRONTEND_URL}/#settings?connected=google`.
  Errors → redirect with `?error=...`; nothing is stored.
- `_token(db)` refreshes with the refresh token when `expiry - 60 s < now`; a `400
  invalid_grant` marks `status=error`, `last_error="Reconnect Google Calendar"`.
- `sync(db, *, window_days=30)`: `GET https://www.googleapis.com/calendar/v3/calendars/{id}/events`
  with `timeMin=now-1d`, `timeMax=now+window`, `singleEvents=true`, `orderBy=startTime`,
  `maxResults=250`, paginate `nextPageToken`. Upsert cache rows by `external_id`; rows in
  the window not returned → `deleted_at`. Only user-visible changes are **not** logged
  (cache). `emit("calendar.changed")` when anything changed. **VERIFY** field names
  (`start.dateTime` / `start.date` for all-day, `updated`, `status`).
- `create_event(db, title, start_local, duration_minutes=60, location=None, notes=None)`:
  `POST .../events` with `start/end` as RFC3339 in `FOUNDER_OS_TZ`; cache row
  `created_by_us=True`; `record_create(db, row, f"Calendar: {title} {hh:mm}")`.
  `change_log._after_undo_hook` gains: `calendar_events` undo of a create → `DELETE
  .../events/{external_id}` best effort (failure logged, the cache row is still removed).
- `today(db, day=None) -> list[dict]` from the cache: `{id, title, start_local "HH:MM" |
  "all day", end_local, location, html_link, created_by_us}` sorted.
- `disconnect(db)`: revoke at `https://oauth2.googleapis.com/revoke` (best effort), clear
  `secret_json`, status `disconnected`; cache rows kept with `deleted_at`.

### Email — `backend/integrations_email.py`

- Env: `EMAIL_ADDRESS`, `EMAIL_APP_PASSWORD`, `EMAIL_IMAP_HOST` (`imap.gmail.com`),
  `EMAIL_SMTP_HOST` (`smtp.gmail.com`), `EMAIL_SMTP_PORT` (`465`), `EMAIL_FROM_NAME`
  (`Bandile Masocha`). Presence of address + password = configured; the `integrations`
  row for `email` records status and last sync (secrets stay in env).
- `send(db, *, to, subject, body, reply_to=None) -> {"ok", "message_id", "error"}`:
  `smtplib.SMTP_SSL(host, port, timeout=20)`, login, `EmailMessage` with
  `Message-ID` from `email.utils.make_msgid(domain=…)`, plain text only; stores an
  `email_messages` row `direction=out`. Used by 5B's `EmailChannel` (which also writes
  `external_id = message_id`). Never called except through the outreach approval gate or
  a future explicit tool.
- `fetch(db, since=None) -> dict`: `imaplib.IMAP4_SSL`, `SELECT INBOX`,
  `SEARCH (SINCE dd-Mon-yyyy)` for the last 3 days by default, fetch
  `(BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID IN-REPLY-TO)] BODY.PEEK[TEXT]<0.2000> X-GM-THRID)`,
  decode with `email.message_from_bytes`; skip `message_id` already stored. **Only store
  messages whose sender matches** a lead email, a contact email or a client email (lower-
  cased exact match on the address part) — everything else is counted and discarded.
  For each stored inbound message: `communications` row (`channel=email`,
  `direction=in`, `summary=f"{subject}: {snippet[:140]}"`, `source=email_sync`); if the
  lead's status is `contacted` → `replied`, follow-up todo done, `emit("lead.replied",
  {"lead_id", "email_message_id"})`; if `In-Reply-To` matches an `outreach_messages.external_id`
  → `outreach_message_id` set and the outreach row gets `replied_at` (new column in m021).
  Returns `{fetched, stored, matched_leads, matched_clients, ignored}`.
- Errors (`imaplib.IMAP4.error`, socket) → `status=error`, `last_error` with the server
  message redacted of credentials; never raises.

### Telegram pairing — `backend/integrations_telegram.py`
Row `telegram`: `config_json={"chat_id", "username"}`. `pair_code()` as in 6D.
`POST /integrations/telegram/set-webhook` → Bot API `setWebhook(url={FOUNDER_OS_API_URL}/integrations/telegram/webhook, secret_token=TELEGRAM_WEBHOOK_SECRET, allowed_updates=["message"])`
→ `{ok, description}`. `POST /integrations/telegram/unpair` clears `config_json`.

### Redaction — `integrations.redact(text) -> str`
Replaces every value present in `secret_json` of any row plus the env tokens
(`TELEGRAM_BOT_TOKEN`, `EMAIL_APP_PASSWORD`, `GOOGLE_CLIENT_SECRET`, `JOBS_TOKEN`,
`YOUTUBE_API_KEY`) with `***`; also the `bot<token>` URL path pattern. Used by every
`print` and every `last_error` write in the three provider modules and in `notify.py`.

### Endpoints — `backend/integrations.py` (`router`; provider modules import it)

| Method & path | Returns |
|---|---|
| `GET /integrations` | `[{provider, status, account_label, connected_at, last_sync_at, last_error, configured: bool, actions: ["connect"|"disconnect"|"sync"|"pair"|"set_webhook"]}]` — never `secret_json` |
| `GET /integrations/google/start` → `{url}`; `GET /integrations/google/callback` (exempt) → 302; `POST /integrations/google/sync` → sync counts; `POST /integrations/google/disconnect` |
| `GET /calendar/today?date=` → `today()`; `GET /calendar/week?from=` → 7 days grouped |
| `POST /calendar/events` `{title, start (ISO local or text), duration_minutes?, location?, notes?}` → event + `change` |
| `POST /integrations/email/sync` → `fetch()` counts; `POST /integrations/email/test` → sends a test email to `EMAIL_ADDRESS` |
| `GET /email/messages?lead_id=&client_id=&limit=` → stored messages (snippets) |
| `POST /integrations/telegram/set-webhook`, `POST /integrations/telegram/unpair` |

### Orchestrator tools

| Tool | Args | Behaviour |
|---|---|---|
| `calendar_today` | `date?` (text) | read-only list of events for the day from the cache; says "Calendar not connected" otherwise |
| `calendar_add_event` | `title` (req), `when` (req; date+time text), `duration_minutes?` (60), `location?`, `notes?` | creates on Google, logged, undoable; ambiguous time ("Friday") without a clock time → `{"error": "need a time"}`; never creates without an explicit instruction |
| `email_lookup` | `who` (lead/client/contact fragment) | last 5 stored snippets from/to that person; read-only |

Prompt section "CALENDAR AND EMAIL": `calendar_add_event` only when Bandile schedules
something with a time; prefer tasks for undated commitments; `email_lookup` when he asks
what someone wrote; never summarise email you have not fetched.

### Briefing integration
- Timed events today (not all-day, `end_at > now`) → category `event`, target_type
  `calendar_event`, due_label `HH:MM`, score **50 + max(0, 20 - hours_until*2)** (sooner =
  higher, capped at 70), reason `location or "Calendar"`, action `"Be there"`. All-day
  events → single line in a new `today_events` key, not items.
- `GET /briefing/today` gains `calendar: {connected, events_today: [...], all_day: [...]}`
  and `integrations: {email: status, telegram: paired, google_calendar: status}` so the
  Command Centre can show connection warnings.
- Alert rule: `{"id": "integration_error", "severity": "warn", "check":
  "integration_error", "params": {}, "message": "An integration is in error. Open Settings
  → Integrations."}` with evaluator `_check_integration_error`.

### Settings → Integrations card (frontend)
One row per provider: status chip, `account_label`, `last_sync_at`, one button
(`Connect` opens `start.url` in the same tab; `Sync`; `Disconnect` behind a confirm
dialog; `Pair` shows the bot link and code; `Set webhook`). Plus the notification settings
card (6D) and the YouTube handle field (5D). No other inputs.

### Acceptance tests — `tests/test_phase6c_integrations.py` (all HTTP patched)
1. OAuth start builds the right URL and stores a pending state; callback with a bad state →
   redirect with `error`, nothing stored; good state + patched token exchange → connected,
   `secret_json` set, `GET /integrations` does **not** contain the token string.
2. Token refresh when expired; `invalid_grant` → `status=error`.
3. Calendar sync upserts, soft-deletes vanished events, converts all-day and timed events,
   emits `calendar.changed` once per changed sync; a second identical sync emits nothing.
4. `calendar_add_event("Clinic demo", "Friday 14:00")` → remote `POST` called with RFC3339
   in `FOUNDER_OS_TZ`, cache row, change entry; undo → remote `DELETE` called, row gone.
5. Email `fetch` with a fake IMAP (patch `imaplib.IMAP4_SSL`) returning three messages:
   one from a lead (stored, lead → `replied`, event emitted, follow-up done), one from a
   client contact (stored + communication), one unknown (ignored, counted); re-run stores
   nothing new.
6. `send` via patched `smtplib.SMTP_SSL` stores an `out` row and returns a message id;
   5B's `EmailChannel` now sends and marks `sent`.
7. Redaction: a `last_error` containing the app password or the bot token is stored and
   logged as `***`.
8. `/integrations/google/callback` is reachable without a bearer token; `/integrations`
   is not.
9. Briefing with two timed events: items at 50–70 with `HH:MM` labels; `calendar.connected`
   false when disconnected; `integration_error` fires when a row is in error.
10. Startup test green with m021.

### Dependencies
6B for the sync agents (the sync functions also work from the endpoints without 6B).
5B for lead matching, 5C for contacts/communications (feature-detected like 5B does).

### Estimated running cost
Google Calendar API: free (quota far above a sync every 30 min). IMAP/SMTP: free.
No LLM calls. ≈ **$0**.

### External accounts, approvals, payments
- **Google Cloud project**: enable Google Calendar API; create an OAuth client (Web
  application) with the Render callback URL; configure the consent screen (External),
  add the scope, **publish** the app (unverified). **VERIFY** the publish-without-
  verification behaviour for the `calendar.events` sensitive scope; if Google blocks it,
  fall back to ICS read-only (`GOOGLE_CALENDAR_ICS_URL`) — the cache and briefing code
  are identical, only `sync()` changes and `calendar_add_event` returns "not available".
- **Google account**: 2-Step Verification on, create an **App Password** for "Mail"
  (**VERIFY** availability; Workspace accounts may have it disabled by admin — this is a
  personal account). Confirm IMAP access is enabled in Gmail settings (**VERIFY** whether
  the toggle still exists in 2026).
- Telegram: as 6D. No payments anywhere.

---

## 6A. Applications

### Purpose
Track every long-horizon application (Wharton first; scholarships, competitions,
programmes later): what it requires, what is missing, when it is due, and what in the
profile is weak relative to it — with deadlines that come only from the user or the
documents they paste, never from the model's memory.

### Friction
- Create by conversation: "add Wharton — Huntsman Program, deadline 1 January" →
  `add_application`.
- Paste the requirements page text into the dock: "here are the Wharton requirements:
  …" → `import_requirements` extracts a checklist (one LLM call) and writes it; shows
  changes with Undo-all.
- Progress by conversation: "finished the first Wharton essay draft" →
  `update_requirement(status=in_progress|ready)`; or one tap on the requirement row
  (**advance** `missing → in_progress → ready → submitted`, wrapping like syllabus).
- One tap **Make it a task** on a requirement creates a dated todo; the briefing then
  carries it.
- "Where am I weak for Wharton?" → `application_gaps` (cached review).

### Tables (migration `m019_applications.py`)

`applications`

| Column | Type | Notes |
|---|---|---|
| `id` PK; `name` VARCHAR NOT NULL; `institution` VARCHAR; `programme` VARCHAR | | |
| `kind` VARCHAR NOT NULL DEFAULT 'university' | | `university|scholarship|competition|programme|job|other` |
| `deadline` DATE; `deadline_source` VARCHAR | | `user|document|unknown`; `deadline` null when unknown |
| `decision_date` DATE | | |
| `status` VARCHAR NOT NULL DEFAULT 'researching' | | `researching → preparing → submitted → interview → accepted|rejected|withdrawn` |
| `priority` INTEGER DEFAULT 2 | | 1 high … 3 low |
| `portal_url` VARCHAR; `notes` TEXT | | |
| `requirements_updated_at`, `gaps_reviewed_at` TIMESTAMP | | cache control for the gap review |
| `created_at`, `updated_at` | | |

`application_requirements`

| Column | Type | Notes |
|---|---|---|
| `id` PK; `application_id` INTEGER NOT NULL | | |
| `title` VARCHAR NOT NULL; `kind` VARCHAR NOT NULL DEFAULT 'other' | | `essay|test|transcript|recommendation|form|fee|interview|portfolio|other` |
| `status` VARCHAR NOT NULL DEFAULT 'missing' | | `missing → in_progress → ready → submitted`; `not_required` |
| `due_date` DATE; `due_source` VARCHAR | | as above |
| `details` TEXT | | word limits, prompts, who recommends |
| `todo_id` INTEGER; `note_id` INTEGER | | links |
| `sort_order` INTEGER DEFAULT 0 | | |
| `created_at`, `updated_at` | | |

`application_gaps`

| Column | Type | Notes |
|---|---|---|
| `id` PK; `application_id` INTEGER NOT NULL | | |
| `gap` VARCHAR NOT NULL; `severity` VARCHAR NOT NULL | | `high|medium|low` |
| `evidence` TEXT; `suggested_action` TEXT | | |
| `status` VARCHAR NOT NULL DEFAULT 'open' | | `open|addressed|accepted_risk` |
| `review_batch` VARCHAR | | id of the review that produced it (old open gaps from a previous review are closed as `superseded` when a new review runs — add that status) |
| `created_at`, `updated_at` | | |

Indexes on `application_requirements(application_id)`, `application_gaps(application_id,
status)`. RLS on the three. `DELETABLE` gains `applications` (name),
`application_requirements` (title). Retrieval: requirement `details` and gap text indexed
as `source_type="application"`.

### Primitives — `backend/applications.py`

```python
find_application(db, fragment) -> (app|None, candidates)
create(db, name, *, institution=None, programme=None, kind="university", deadline=None, deadline_source=None, priority=2, portal_url=None)
import_requirements(db, app, text) -> dict
#  one LLM call, purpose="application_parse", response_format=json_object, max_tokens=1200, text truncated to 12,000 chars
#  system prompt (stable): extract the checklist; schema
#    {"requirements": [{"title", "kind", "details", "due_date": "YYYY-MM-DD or null", "due_text": "the exact phrase the date came from or null"}],
#     "deadline": "YYYY-MM-DD or null", "deadline_text": "...", "decision_date": ...}
#  RULE in the prompt: dates may only be copied from the text; if the text gives none, null. Never infer a date from knowledge of the institution.
#  code: a requirement with due_date but empty due_text → due_date dropped (guards against invented dates). Upsert by lower(title) (update details/kind/due); set requirements_updated_at; app.deadline set only when app.deadline is null or deadline_source != "user" (user-stated dates win). Everything record_*; returns {created, updated, deadline_set, changes}
advance(db, req) ; set_status(db, req, status) ; make_todo(db, req) -> todo   # text f"{app.name}: {req.title}", due_date = req.due_date or app.deadline - 7d, category "applications"
review_gaps(db, app, *, refresh=False) -> dict
#  cached: if gaps_reviewed_at and gaps_reviewed_at >= requirements_updated_at and not refresh → return stored open gaps
#  context assembled deterministically: requirements (title/status/details), founder_profile.md, syllabus progress per subject (syllabus.grouped progress pct), the 3 most recent notes_search results for app.name, memories recalled for app.name
#  one LLM call, purpose="application_gaps", max_tokens=700, schema {"gaps": [{"gap", "severity", "evidence", "suggested_action"}]} (≤ 8)
#  previous open gaps → status "superseded"; new rows record_create; gaps_reviewed_at = now
timeline(db, app, today) -> dict   # {days_to_deadline, requirements: {missing, in_progress, ready, submitted}, next_due: {...}, at_risk: bool}
#  at_risk = any missing/in_progress requirement whose due (or the deadline) is within 14 days
```

### Endpoints

| Method & path | Body | Returns |
|---|---|---|
| `GET /applications` | `status?` | `[application + timeline + gaps_open]` sorted by deadline |
| `GET /applications/{id}` | | application + `requirements[]` + `gaps[]` (open) + `timeline` |
| `POST /applications` | `{name, institution?, programme?, kind?, deadline?, priority?, portal_url?}` | 201 + change (`deadline_source="user"`) |
| `PATCH /applications/{id}` | fields incl. `status`, `deadline` (sets source `user`) | + change |
| `POST /applications/{id}/requirements/import` | `{text}` | import result |
| `POST /applications/{id}/requirements` | `{title, kind?, due_date?, details?}` | 201 + change |
| `PATCH /requirements/{id}` | `{title?, kind?, status?, due_date?, details?}` | + change |
| `POST /requirements/{id}/advance` | | one tap |
| `POST /requirements/{id}/todo` | | `{todo, change}` (idempotent while the todo is open) |
| `GET /applications/{id}/gaps?refresh=0|1` | | `review_gaps` |
| `PATCH /gaps/{id}` | `{status}` | + change |

### Orchestrator tools

| Tool | Args | Behaviour |
|---|---|---|
| `add_application` | `name` (req), `institution?`, `programme?`, `kind?`, `deadline?` (date text), `priority?`, `portal_url?` | create; fuzzy dedupe on name; logged |
| `import_requirements` | `application` (req), `text` (req) | `import_requirements`; summary "7 requirements added, deadline 1 Jan from the text" |
| `update_requirement` | `application` (req), `requirement` (req, fragment), `status?`, `due_date?`, `details?`, `make_todo?: bool` | fuzzy pick inside the application; ambiguous → candidates |
| `application_status` | `application?` | read-only: timeline, missing items, open gaps (cached), next due |
| `application_gaps` | `application` (req), `refresh?: bool` | `review_gaps` — the model relays gaps as the system's view and says when the review was last run |

Prompt section "APPLICATIONS": deadlines come only from Bandile or pasted documents —
if asked "when is the Wharton deadline?" and `deadline_source` is `unknown`, say the
system does not have it and ask; never state a date from general knowledge; `import_requirements`
only when he pastes or dictates requirement text.

### Briefing integration
`briefing._application_items(db, today)`:
- Deadline within 60 days → category `application`, target_type `application`, score
  **max(10, 82 - days)** (so 7 days out ≈ 75, 30 days ≈ 52), due_label `"{days} days"`,
  reason `f"{missing} requirement(s) still missing"` or `"All requirements ready — submit"`,
  action `"Work the next missing item"` / `"Submit"`.
- Requirement `missing`/`in_progress` with `due_date` within 14 days → category
  `application`, target_type `application_requirement`, score **60 + (14 - days)**, action
  `"Make it a task"` (completable only via its linked todo).
- Open `high` gaps when the deadline is within 90 days → score **24**, action
  `"Address or accept the risk"`. Max 2.
- `counts.applications_at_risk`; included in `attention_count`.
- `application_watch` agent (daily): computes `days_to_deadline` per application and emits
  `application.deadline_near` at the marks in `deadline_days_json` (dedupe by
  `application_id:days` in the payload; 6D turns it into a notification); runs
  `review_gaps(refresh=True)` for applications whose requirements changed since the last
  review (≤ 2 LLM calls per run).
- Alert rule: `{"id": "application_at_risk", "severity": "critical", "check":
  "application_at_risk", "params": {"days": 14}, "message": "An application deadline is
  inside 14 days with requirements still missing."}`; evaluator `_check_application_at_risk`.

### Acceptance tests — `tests/test_phase6a_applications.py`
1. `import_requirements` with a patched LLM returning seven requirements, one with a
   `due_date` but no `due_text` → six get dates, that one has `due_date=None`; deadline
   from the text sets `deadline_source="document"`; a user-set deadline is not overwritten
   by a later import; re-import upserts by title (no duplicates); batch undo removes all.
2. `add_application("Wharton", deadline="1 January")` resolves to next 1 Jan from today,
   `deadline_source="user"`; `application_status` with no deadline reports `unknown`
   (assert the tool result has `deadline: None, deadline_source: "unknown"`).
3. `advance` cycles statuses; `make_todo` creates one todo with the computed due date and
   is idempotent while open; completing the todo via `complete_task` does **not** change
   the requirement (explicit decision: statuses are set by the user), but the briefing item
   for that requirement disappears when `status in (ready, submitted)`.
4. `review_gaps` caches: second call without changes → no LLM call; after a requirement
   update → one call; previous gaps `superseded`.
5. Briefing: deadline in 7 days with 2 missing → application item ≈ 75 and counted in
   `attention_count`; requirement due in 3 days → 71; `application_at_risk` fires.
6. `application_watch` emits `application.deadline_near` once per mark per application.
7. Startup test green with m019.

### Dependencies
Notes/retrieval/syllabus (exist). 6B for `application_watch`, 6D for the countdown
notifications (optional).

### Estimated running cost
Import ≈ 4k in / 900 out ≈ **$0.003** per paste; gap review ≈ 6k in / 600 out ≈ **$0.003**.
A handful per month ≈ $0.02.

### External accounts, approvals, payments
None. (Scraping institution pages is deliberately not done; the user pastes text.)

---

## Phase 6 totals

| Module | New tables | New tools | LLM $/month | External |
|---|---|---|---|---|
| 6B | 4 (+1 column) | 3 | 0 (agents spend Phase 5 budgets) | GitHub secrets, Render env |
| 6D | 2 | 2 | ≈ 0 | Telegram bot (free) |
| 6C | 3 (+1 column on outreach_messages) | 3 | 0 | Google Cloud OAuth client; Gmail app password |
| 6A | 3 | 5 | ≈ 0.02 | none |
| **Total** | **12 tables, m019–m022** | **13 (registry → 57)** | **≈ $0.02 + Phase 5's ≈ $0.65** | |

Env vars added: `JOBS_TOKEN`, `FOUNDER_OS_TZ`, `JOBS_TICK_BUDGET`, `JOBS_IN_PROCESS`,
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_USERNAME`, `TELEGRAM_WEBHOOK_SECRET`,
`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_CALENDAR_ID`, `FOUNDER_OS_API_URL`,
`FOUNDER_OS_FRONTEND_URL`, `EMAIL_ADDRESS`, `EMAIL_APP_PASSWORD`, `EMAIL_IMAP_HOST`,
`EMAIL_SMTP_HOST`, `EMAIL_SMTP_PORT`, `EMAIL_FROM_NAME`.
Auth exemptions added: `/jobs/tick`, `/integrations/google/callback`,
`/integrations/telegram/webhook` — each with its own secret check.
Files added: `backend/agents/*`, `backend/notify.py`, `backend/telegram_inbound.py`,
`backend/integrations.py`, `backend/integrations_google.py`,
`backend/integrations_email.py`, `backend/integrations_telegram.py`,
`backend/applications.py`, `.github/workflows/jobs-tick.yml`, four migrations, four test
modules; `orchestrator.run_turn` extracted.

### Everything marked VERIFY, in one list
1. GitHub Actions: 5-minute minimum, delays, 60-day auto-disable on inactive public repos.
2. Render free tier: request timeout (tick budget 80 s assumes ≥ 100 s), spin-down.
3. Telegram Bot API: `sendMessage` limits (4096), `setWebhook` `secret_token` and the
   `X-Telegram-Bot-Api-Secret-Token` header, webhook response-time expectations.
4. Google OAuth: an unverified "In production" app with the `calendar.events` sensitive
   scope can be authorised by its owner through the warning screen; refresh tokens do not
   expire as they do in Testing.
5. Google Calendar API v3 event field names and all-day representation.
6. Gmail App Passwords still available with 2-Step Verification; IMAP enabled; `X-GM-THRID`
   extension.
7. YouTube Data API `forHandle`, quota units (5D).
8. LinkedIn / X analytics exports exist for the account and their column names (5D).
