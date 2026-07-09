# Founder OS

A personal operating system for disciplined execution, KPI tracking, and systems thinking. This is the author's daily system for academics, health, trading, and business execution — not a multi-tenant SaaS product.

What it does

- Academic tracking — syllabus progress by subject, topic, and subtopic, weighted against actual exam dates
- Health — daily and weekly logs, lift progression
- Trading — backtest logging with a hard-coded 50-trade / 90%-adherence gate before live trading unlocks
- Non-negotiables — daily habit tracking against personal standards
- AI layer — an orchestrator that reasons from personal doctrine, standing rules, and reading plans; can log brain dumps, surface off-track alerts, and answer questions grounded in the author's own rules

What it isn't

Not a SaaS product. Not built for other users. No auth layer, no multi-tenancy — it's scoped to one person's data because that's the only person it was built for.

Tech Stack

- Frontend: HTML / CSS / Vanilla JavaScript (single file, no build step)
- Backend: Optional FastAPI service providing /chat, /parse, voice, and research endpoints
- Persistence: localStorage (frontend) and optional SQLite/Postgres backend for AI features
- AI: OpenAI APIs for orchestrator, STT/TTS, and parsing

Running the project

Option A — Open locally

Just open frontend/index.html in a browser.

Option B — With backend

Ensure your backend is running at:
http://127.0.0.1:8000/chat
Then open the frontend normally.

Data persistence (important)

All execution data is stored in browser localStorage by default. When the backend is used, the AI features and persistence are enabled.

Status

v0.1 — Stable

Planned upgrades include manual export/import of data, backend sync, KPI trend history, and assistant-driven insights.
