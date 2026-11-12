# Founder OS

A personal operating system for one person: academics, health, business and long-term goals, run by conversation. Information gets in by talking or a single tap, never through forms. It is the author's daily system, not a multi-tenant product.

## Status

Phases 1 to 6 are core built (see `ROADMAP.md` for what each phase holds and what remains). The backend suite has 721 tests and runs with no API key and no network. The external accounts for Phase 6 (a Telegram bot, a Google Cloud project, a Gmail App Password) still have to be set up before those features do anything; the steps are in `docs/phases/PROGRESS.md`.

## What it does

- **Conversation first** — one assistant with tools for everything below, by text or live voice. It acts, shows what changed, and every change can be undone. Deletes and anything that leaves the system wait for a yes.
- **Command Centre** — a ranked daily brief, progress, today's calendar events and alerts.
- **Academics** — a four-state syllabus tracker per subject and paper, exam dates, weakest topics.
- **Health, timetable, annual targets, non-negotiables** — daily and weekly logs and habits.
- **Business** — research into opportunities, leads and outreach, clients, deals and payments, a project portfolio, and personal-brand analytics from the platforms' own numbers.
- **Applications** — deadlines, requirements and a gap review for things like university applications.
- **Agents** — small jobs that run on a schedule (housekeeping, follow-ups, research, syncs, the couriers), each with a permission list, a cost cap and an activity log.
- **Integrations and notifications** — Google Calendar, email (replies from known people; approved outreach is sent), and a Telegram bot that sends the morning briefing, alerts and reminders and takes replies.
- **Long-term memory** — notes, an Obsidian vault and remembered facts, searchable by meaning.

## What it isn't

Not a SaaS product and not built for other users: one password, one person's data.

## Tech stack

- **Frontend:** HTML / CSS / vanilla JavaScript (single file, no build step), served by the backend at `/app/`
- **Backend:** FastAPI — Render (one host: the same service serves the app and the API)
- **Persistence:** Supabase Postgres (RLS enforced across all tables), with localStorage as a frontend fallback
- **AI:** OpenAI APIs through one gateway (`backend/llm.py`): `ASSISTANT_MODEL` for the assistant, parser and summaries, an embedding model for retrieval, a realtime model for live voice

## Running the project

Run the backend and open the app it serves:

```
cd backend
pip install -r requirements.txt      # Python 3.12
uvicorn main:app --reload
```

Then visit `http://127.0.0.1:8000/app/` (`/` redirects there). The page calls the API on its own
origin. In production the same thing runs on Render: the app is at `https://<render-service>/app/`
and installs to a phone's home screen as "Founder OS" (web app manifest, standalone display).
Deployment steps: `docs/DEPLOY.md`.

## Docs

- `ROADMAP.md` — the phases, what is built and what remains
- `CLAUDE.md` — how the system is put together, environment variables, how to run the tests
- `docs/REBUILD_HANDOFF.md` — every endpoint and its shape
- `docs/phases/` — the Phase 5 and 6 specifications, the build order, and `PROGRESS.md` (what was built, every decision, the setup steps for external accounts)
- `docs/adr/` — architecture decisions; `docs/CONVENTIONS.md` — the patterns new code follows
- `docs/DEPLOY.md` — deployment and rollback
- `docs/specs/`, `docs/sessions/` — earlier specs and build logs

## Repo

github.com/Bandile-tech/Founder-OS
