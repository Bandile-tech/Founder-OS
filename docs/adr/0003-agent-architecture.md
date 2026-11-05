# ADR-0003 — Agent architecture

Date: 2026-11-05 · Status: accepted · Applies to: Phase 6B and every agent in Phases 5–6

## Context
The market-intelligence agent (`backend/market_intel/`) is a synchronous, multi-role
pipeline invoked as an orchestrator tool. Phases 5–6 add a dozen things that should run
without being asked: housekeeping (recurring payments, overdue statuses), qualification,
portfolio review, syncs (brand, calendar, email), couriers (briefing, alerts, reminders)
and scheduled research. They must be visible (what ran, what it changed, what it cost),
bounded (no agent may delete, send, or exceed a budget) and cheap to add. The product
rules apply to agents as to the assistant: act then show with undo; ask only before
deleting; never maintain anything by hand.

## Options considered
1. **A framework** (LangChain/LangGraph/CrewAI-style multi-agent orchestration) —
   dependencies, a second mental model next to the existing tool loop, hard to test
   offline, cost opaque. Rejected.
2. **The orchestrator LLM as the scheduler** ("every morning, decide what to do") —
   non-deterministic, costs tokens for routine work, cannot be audited. Rejected.
3. **A job queue and worker** (RQ/Celery/Arq) — needs Redis or a worker process; not free
   on Render (ADR-0001). Rejected.
4. **A declarative registry of plain Python modules, run sequentially by one runner on
   each tick** — each agent is a function with a spec (schedule, event triggers,
   permissions, caps); the runner provides the session, the change-log batch, cost
   attribution and the audit. Chosen.

## Decision
- `backend/agents/` holds `AgentSpec` entries: `key`, schedule (`interval_minutes` or
  `at_time` + weekdays), `events` it reacts to, `Permissions` (allowed read tools,
  writable tables, may notify, may emit which events, max LLM calls, max seconds) and a
  `run(ctx)` function returning a one-line summary and a result dict.
- **One runner, sequential, inside a tick budget.** Each run opens its own session, a
  change-log batch with source `agent:<key>`, and a `llm` run context so every model call
  is attributed to the run. Runs are recorded in `agent_runs` (status, summary, error,
  cost, changes, batch id) and surfaced on the Agents page with Undo per run.
- **Permissions are enforced by the change log, not by trust.** After a run the runner
  audits every change in the batch; a write to a table outside `tables_write` undoes the
  whole batch and marks the run `violation`. Delete tools and `confirm_action` are never
  allow-listed; outbound human-facing sends can only be *proposed* (pending actions).
- **Agents never ask.** Uncertainty becomes a suggestion; anything irreversible becomes a
  pending action; everything else is acted on and logged.
- **Events are rows** (`agent_events`), emitted by modules in their own transactions and
  consumed on the next tick; no in-process pub/sub, no callbacks across threads.
- Deterministic agents (housekeeping, review, sync) have `max_llm_calls = 0`; LLM use is
  the exception and is capped per run.
- The market-intelligence agent is registered as the first entry (`market_intel`),
  running at most one due standing objective per tick; its tool-call path stays for
  explicit "research X" commands.

## Consequences
- Adding an agent = one module with a spec and a `run`; no infrastructure.
- Everything an agent does is visible in three existing places: changes with Undo,
  suggestions, briefing items — plus the run log.
- Sequential runs and a tick budget mean a slow agent delays others to the next tick; the
  specs size agent work accordingly (batches of ≤ 20 leads, one objective per tick).
- Cost attribution during a concurrent assistant turn can be mis-assigned (process-wide
  run context); accepted as a cost-only inaccuracy.
- Permission audit by table name is coarse (an agent allowed to write `todos` may write
  any todo); acceptable for a single-user system where every change is undoable.

## Verify before relying on it
Nothing external. Internal check before build: `change_log.undo_batch` on a batch that
spans several commits (agents commit mid-run) restores every row — covered by acceptance
test 2 of Phase 6B.
