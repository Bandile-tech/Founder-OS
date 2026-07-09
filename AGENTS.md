# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Running the project

**Frontend only** (no backend required):
```
open frontend/index.html in a browser
```

**With backend** (required for AI chat, persistence, KPIs):
```
cd backend
uvicorn main:app --reload
```
Then visit `http://127.0.0.1:8000` — the backend serves the frontend at `/` and the API at its endpoints.

**Environment**: Create `backend/.env` with:
```
OPENAI_API_KEY=your_key_here
```

**Install dependencies**:
```
cd backend
pip install -r requirements.txt
```

## Architecture

### Frontend
A single self-contained file: `frontend/index.html` with all CSS and JavaScript inline. No build step, no framework. Design uses the "Luna palette" — `#011C40` background, IBM Plex Mono + Syne fonts. The frontend calls the backend API at `http://127.0.0.1:8000`.

### Backend
FastAPI app in `backend/main.py`. Modules:

| File | Role |
|------|------|
| `main.py` | All API endpoints + startup seeding logic |
| `models.py` | SQLAlchemy ORM models |
| `schemas.py` | Pydantic request/response models |
| `database.py` | SQLite engine (`founder_os.db`) + session factory |
| `openai_client.py` | GPT-4o-mini integration + system prompt |
| `memory_service.py` | Fetches recent AI memory from DB |
| `ai_formatter.py` | Strips markdown from AI output |

### Database
SQLite at `backend/founder_os.db`. Tables are auto-created on startup via `models.Base.metadata.create_all()`. On first run, startup handlers seed default habits, annual targets, roadmap tasks, a 20-entry bible plan, and books.

### KPI state
KPIs (sprint times, academic syllabus %) are kept in `_kpi_state` (in-memory dict in `main.py`) loaded at startup from `KPISnapshot` table. Defaults are defined in `KPI_DEFAULTS`. Sprint KPIs use `lower_is_better: True` — this flag affects progress calculation throughout.

### AI / Brain dump
`POST /parse` accepts free-text, sends to GPT-4o-mini, and atomically applies extracted updates to KPIs, todos, habits, roadmap tasks, and annual targets in one transaction. The parse schema is defined inline in `openai_client.get_parse_response()`.

`POST /chat` stores full session history in `chat_messages` table and injects recent `AIMemory` entries as a memory block into the system prompt.

`GET /radar` computes 6 domain scores (core, physical, intellect, business, skills, social) from live DB data and passes them to GPT for interpretation.

### Week freezing
`WeeklyTargetSnapshot.frozen = True` is set on startup for any snapshots whose `week_end` has passed. Frozen weeks reject new impact logs with HTTP 400. This runs automatically on startup via `run_freeze_on_startup`.

## Critical components — treat with care

- **KPI aggregation logic**: `_kpi_state`, `KPI_DEFAULTS`, `_at_progress_pct()` in `main.py`, and `kpi_pct()` in `openai_client.py` — these feed the radar and weekly reviews.
- **Weekly target structure**: `WeeklyTarget` + `WeeklyTargetSnapshot` + `LogImpact` — the snapshot/freeze lifecycle is non-trivial.
- **Execution log schema**: `DailyLog` → `LogImpact` → `WeeklyTargetSnapshot` update chain in `POST /log-impact`.

## Key domain concepts

- **Annual targets**: Year-level goals with `lower_is_better` flag (used for sprint PBs). Progress is compared against year-elapsed % to produce `ahead / on_track / behind / critical` status.
- **Roadmaps**: `sprint` and `academic` types, each with phases (`sprint-p1`, `acad-maths`, etc.) and task IDs like `sprint-acc`, `m-calc`.
- **Habit keys**: `scripture_prayer`, `ironing`, `python_session`, `sprint_training`, `academics` — these exact strings are referenced by the parse engine.
- **KPI keys**: `sprint_100m`, `sprint_200m`, `sprint_400m`, `maths_syllabus`, `further_maths`, `business`, `economics` — referenced by name throughout AI prompts.
