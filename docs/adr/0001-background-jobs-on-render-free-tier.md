# ADR-0001 — Background jobs on Render's free tier

Date: 2026-11-05 · Status: accepted · Applies to: Phase 6B, 6D, every scheduled agent

## Context
The backend is a single FastAPI web service on Render's free tier. It spins down after
about 15 minutes without requests and takes 30–60 s to wake (`docs/sessions/fix_session_5.md`).
The existing `APScheduler` job (vault sync every 6 h, `main.start_vault_scheduler`)
therefore runs only while someone is using the app. Phases 5–6 need work to happen while
the app is closed: a 06:00 briefing, alert checks, calendar and email sync, recurring
payment generation, scheduled research. Nothing may cost money; the stack is deliberately
free (Render and Supabase; Render serves the app at `/app/` and the API beside it, so there
is no separate frontend host).

## Options considered
1. **Render Cron Job / background worker** — correct tool, but not on the free tier
   (paid instance types). Rejected on cost.
2. **Keep the process awake with a pinger** (UptimeRobot etc.) so APScheduler keeps
   running — works until Render's free-tier hours or policy change; relies on a third
   party for liveness and still has no record of what ran. Rejected as fragile.
3. **Supabase `pg_cron` + `pg_net`** calling the HTTP endpoint from inside Postgres —
   plausible and free, but extensions and HTTP-from-database add operational surface the
   user does not otherwise touch. Kept as fallback.
4. **GitHub Actions scheduled workflow** calling an authenticated `POST /jobs/tick` —
   free for a public repository (this one is), already where the code lives, the call
   itself wakes Render, and every run is visible in the Actions tab. Chosen.
5. **A second free host** (e.g. a serverless function) as the clock — another account and
   deploy pipeline for a one-line curl. Rejected.

## Decision
- One authenticated endpoint, `POST /jobs/tick` (header `X-Jobs-Token`), runs every due
  agent sequentially inside a time budget and records a `job_ticks` row. It is the only
  scheduler of record.
- `.github/workflows/jobs-tick.yml` calls it every 15 minutes (`*/15 * * * *`) with retries
  for cold starts, using repository secrets `FOUNDER_OS_URL` and `JOBS_TOKEN`.
- The in-process `APScheduler` also calls `tick("in_process")` every 15 minutes while the
  process is awake, so work is prompt when the user is active; `min_gap_minutes` on each
  agent prevents double runs.
- Agents are written for a **15-minute latency floor** and to be idempotent, because a
  tick may be late, doubled, or missed.
- The Agents page shows the last tick's age; a tick older than an hour is surfaced as a
  warning so a silently disabled workflow is noticed.

## Consequences
- No new paid service; no new account (GitHub already hosts the repo).
- Everything scheduled is an agent (ADR-0003); nothing else may schedule itself.
- Schedules are approximate (≥ 15 min). Anything needing seconds (voice, chat) stays
  request-driven.
- If GitHub's schedule is disabled for inactivity, a commit re-enables it; the fallback
  (option 3, `pg_cron` → `pg_net` → the same endpoint) requires no code change.

## Amendment — 2026-11-11: the external schedule ships switched off

Building Phase 6B showed a cost the decision above missed. A request every 15 minutes
never lets a free Render instance go to sleep, so the service would run around the clock
and use up the month's free instance hours (about 750) before the month ends, taking the
app offline. Free hosting matters more than overnight runs, so:

- `.github/workflows/jobs-tick.yml` ships with `workflow_dispatch` only; the `schedule`
  block is present but commented out. It can be started by hand from the Actions tab.
- The in-process tick (`JOBS_IN_PROCESS`, default on) stays: while the app is open, every
  due agent runs within about 15 minutes. `POST /jobs/tick` is unchanged and is still the
  scheduler of record whenever something calls it.
- Consequence: while the app is closed and Render is asleep, nothing runs. An `at_time`
  agent that was due overnight runs on the first tick after the app is next opened (it is
  due until it has run that day); interval agents run once on waking. The 06:00 briefing
  is therefore delivered when the app is first opened that day, not at 06:00, until the
  schedule is switched on.
- To switch it on (a paid instance, or a host with no hour limit): uncomment the two
  `schedule` lines in the workflow, set the repository secrets `FOUNDER_OS_URL` and
  `JOBS_TOKEN`, set `JOBS_TOKEN` on Render to the same value, and push. No code changes.
- A cheaper middle path, not built: a schedule of a few fixed times a day (for example
  `0 4 * * *` for 06:00 Lusaka) wakes the instance briefly without keeping it awake.

## Verify before relying on it
- GitHub Actions: minimum schedule interval (5 min), typical delay under load, and the
  rule that scheduled workflows on public repos are disabled after 60 days without
  activity (and how re-enabling works).
- Render free tier: HTTP request timeout (the tick budget of 80 s assumes ≥ 100 s) and the
  current spin-down behaviour.
- Supabase: `pg_cron` and `pg_net` availability on the free plan, if the fallback is ever
  needed.
