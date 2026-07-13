# Founder OS

A personal operating system for disciplined execution, KPI tracking, and systems thinking. This is the author's daily system for academics, health, trading, and business execution — not a multi-tenant SaaS product.

## Status

**Feature-complete, deliberately paused.** All seven core build phases are shipped, plus the Phase 6 orchestrator and the Session C Obsidian cold-archive integration. 268 passing tests, zero known regressions.

Active development is paused while priority shifts to client acquisition (Aether AI outreach) and A-Level exam preparation. This is a closed chapter, not an unfinished one — it reopens on a stated trigger (e.g. Aether AI lands its first paid client, or a bug here directly blocks something else), not by default.

## What it does

- **Academic tracking** — syllabus progress by subject, topic, and subtopic, weighted against actual exam dates
- **Health** — daily and weekly logs, lift progression
- **Trading** — backtest logging with a hard-coded 50-trade / 90%-adherence gate before live trading unlocks
- **Non-negotiables** — daily habit tracking against personal standards
- **AI orchestrator** — a single `/orchestrator` endpoint (GPT-5.4, via `ORCHESTRATOR_MODEL` env var — no Anthropic API budget) exposing eight tools: `route_brain_dump`, `get_dashboard_state`, `detect_off_track`, `synthesize_weekly_review`, `surface_weakest_subtopic`, `query_war_room`, `add_todos`, `query_cold_archive`. `add_todos` and `query_cold_archive` only fire on explicit instruction — the orchestrator surfaces findings, it doesn't write to pipeline tables on its own.
- **Cold archive** — Obsidian vault integrated as searchable long-term memory (Session C)

## What it isn't

Not a SaaS product. Not built for other users. No auth layer, no multi-tenancy — it's scoped to one person's data because that's the only person it was built for.

## Tech stack

- **Frontend:** HTML / CSS / vanilla JavaScript (single file, no build step) — Netlify
- **Backend:** FastAPI — Render
- **Persistence:** Supabase Postgres (RLS enforced across all tables), with localStorage as a frontend fallback
- **AI:** OpenAI APIs (GPT-4o-mini for parsing, GPT-5.4 for orchestration), STT/TTS for voice

## Running the project

**Option A — Frontend only:** open `frontend/index.html` in a browser. Execution data persists to localStorage.

**Option B — With backend:** run the FastAPI backend, then point the frontend at it (default `http://127.0.0.1:8000/chat`). Enables the AI orchestrator, voice, and Postgres-backed persistence.

## Docs

- `docs/specs/` — architecture and feature specs (database, orchestrator, RLS security audit, market intelligence agent, Obsidian integration)
- `docs/sessions/` — chronological build and fix logs from individual development sessions

## Repo

github.com/Bandile-tech/Founder-OS
