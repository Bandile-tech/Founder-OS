from fastapi import FastAPI, Depends, HTTPException, Query, UploadFile, File, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, Response, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import date, timedelta, datetime
from typing import List, Optional, Dict, Any
import json
import os
import database
import models
import schemas
from database import engine, get_db, SessionLocal
from models import (
    AIMemory, Habit, AnnualTarget, KPISnapshot, RoadmapTask,
    BibleEntry, Book, SocialScore, Client, Revenue,
    Subject, Topic, Subtopic,
    DailyHealth, WeeklyHealth, Lift, LiftLog,
    PropFirmAccount, BacktestTrade, LiveTrade,
    Document, DocumentChunk, NonNegotiable, ReadingPlan, ReadingPlanEntry,
    DailyBibleLog,
)
from memory_service import get_recent_memories
from openai_client import (
    get_chat_response_with_memory,
    get_parse_response,
    get_proactive_brief,
    get_radar_scores,
    transcribe_audio,
    synthesize_speech,
    ParseFailed,
)
import llm

# ── CREATE TABLES ────────────────────────────────────────────
models.Base.metadata.create_all(bind=engine)

# ── APP ──────────────────────────────────────────────────────
app = FastAPI(title="Founder OS API", version="2.0")

import auth as _auth
# CORS is restricted to the app's own origin(s): FOUNDER_OS_ORIGINS (comma-separated),
# defaulting to the local dev origins. Same-origin requests (the backend serving the
# frontend) need no CORS at all.
app.add_middleware(
    CORSMiddleware,
    allow_origins=_auth.allowed_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Login: every route except /health, /ping, /auth/* and the static frontend needs a
# session token once FOUNDER_OS_PASSWORD is set. Added after CORS so preflights pass.
app.add_middleware(_auth.AuthMiddleware)
app.include_router(_auth.router)

# ── Routers (rebuild modules) ────────────────────────────────
import usage as _usage
import change_log as _change_log
import actions as _actions
import notes as _notes
import syllabus as _syllabus
import briefing as _briefing
import voice_realtime as _voice_realtime
app.include_router(_usage.router)
app.include_router(_change_log.router)
app.include_router(_actions.router)
app.include_router(_notes.router)
app.include_router(_syllabus.router)
app.include_router(_briefing.router)
app.include_router(_voice_realtime.router)
import conversation as _conversation
app.include_router(_conversation.router)

# Serve frontend from ../frontend if it exists
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(FRONTEND_DIR):
    app.mount("/app", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")

# ── CONSTANTS ────────────────────────────────────────────────
TEN_K_START_DATE = date(2026, 6, 1)   # floor for both AI revenue and trading P/L aggregation

# ── KPI DEFAULTS ─────────────────────────────────────────────
# In-memory KPI store (persisted via KPISnapshot on update)
KPI_DEFAULTS = {
    "maths_syllabus": {"label": "Maths 9709",        "value": 62, "unit": "%", "target": 100, "lower_is_better": False},
    "further_maths":  {"label": "Further Maths 9231", "value": 48, "unit": "%", "target": 100, "lower_is_better": False},
    "business":       {"label": "Business 9609",      "value": 71, "unit": "%", "target": 100, "lower_is_better": False},
    "economics":      {"label": "Economics 9708",     "value": 55, "unit": "%", "target": 100, "lower_is_better": False},
}

# Runtime KPI state (loaded from latest snapshots on startup)
_kpi_state: Dict[str, dict] = {}


def load_kpi_state(db: Session):
    global _kpi_state
    _kpi_state = {k: dict(v) for k, v in KPI_DEFAULTS.items()}
    # Override with latest snapshots
    for key in _kpi_state:
        snap = db.query(KPISnapshot).filter(
            KPISnapshot.key == key
        ).order_by(KPISnapshot.date.desc()).first()
        if snap:
            _kpi_state[key]["value"] = snap.value


# ── STARTUP ──────────────────────────────────────────────────
BOOK_DEFAULTS_DATA = [
    ("The Almanack of Naval Ravikant", "Naval Ravikant", "reading", 67, 240),
    ("Zero to One", "Peter Thiel", "queue", 0, 195),
    ("The Hard Thing About Hard Things", "Ben Horowitz", "queue", 0, 304),
    ("Atomic Habits", "James Clear", "done", 320, 320),
]

def _seed_books_if_empty(db: Session):
    if db.query(Book).count() == 0:
        for title, author, status, page, total in BOOK_DEFAULTS_DATA:
            db.add(Book(title=title, author=author, status=status, page=page, total_pages=total))
        db.commit()


@app.on_event("startup")
async def startup():
    from migrations.m002_academic_roadmap import seed_subjects
    from migrations.m003_lift_log import run as run_m003
    from migrations.m004_warroom import run as run_m004
    if not _auth.auth_required():
        print("[startup] WARNING: FOUNDER_OS_PASSWORD is not set — the API is open. Set it on Render.")
    db = SessionLocal()
    try:
        load_kpi_state(db)
        _seed_habits_if_empty(db)
        _seed_roadmap_tasks_if_empty(db)
        _seed_books_if_empty(db)
        seed_subjects(db)   # m002
        run_m003(db)        # m003 — must run after m002
        run_m004(db)        # m004 — war room schema migration
        from migrations.m005_reading_plan_schema import run as run_m005
        run_m005(db)        # m005 — reading plan columns + book position backfill
        from migrations.m006_todo_completed_at import run as run_m006
        run_m006(db)        # m006 — todos.completed_at column
        from migrations.m007_cold_archive import run as run_m007
        run_m007(db)        # m007 — cold_archive_chunks table
        from migrations.m009_market_intel import run as run_m009
        run_m009(db)        # m009 — market intelligence agent tables
        from migrations.m010_assistant_core import run as run_m010
        run_m010(db)        # m010 — chat thread/tool columns, summaries, llm_usage
        from migrations.m011_change_log import run as run_m011
        run_m011(db)        # m011 — change_log, pending_actions, suggestions, notes
        from migrations.m012_notes_currency_retrieval import run as run_m012
        run_m012(db)        # m012 — revenue.currency, todos.due_date, embeddings, exam_dates
        from migrations.m013_syllabus_briefing import run as run_m013
        run_m013(db)        # m013 — syllabus_points, briefing_summaries
        _validate_schema(engine)   # raises if any ORM column is absent from the live DB
        _seed_non_negotiables_if_empty(db)
        _verify_seed_integrity(db)
    finally:
        db.close()


def _validate_schema(db_engine) -> None:
    """
    Confirm every ORM-declared column exists in the live database.

    Runs after all migrations complete.  If any column is missing it raises
    RuntimeError immediately — this is preferable to a cryptic AttributeError
    or silent wrong query result later.

    Uses information_schema on Postgres and PRAGMA table_info on SQLite.
    Tables with zero live columns are skipped (they may not have been created
    yet, which create_all() handles elsewhere).
    """
    from sqlalchemy import text as _text
    is_postgres = db_engine.dialect.name == "postgresql"
    missing: dict[str, list[str]] = {}

    with db_engine.connect() as conn:
        for table in models.Base.metadata.sorted_tables:
            tbl = table.name
            if is_postgres:
                rows = conn.execute(_text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = :t"
                ), {"t": tbl}).fetchall()
            else:
                rows = conn.execute(_text(f"PRAGMA table_info({tbl})")).fetchall()

            if not rows:
                # Table not yet created — create_all() will handle it
                continue

            db_cols = {r[0] for r in rows} if is_postgres else {r[1] for r in rows}
            orm_cols = {col.name for col in table.columns}
            drift = orm_cols - db_cols
            if drift:
                missing[tbl] = sorted(drift)

    if missing:
        lines = "\n".join(f"  {tbl}: {cols}" for tbl, cols in missing.items())
        raise RuntimeError(
            f"Schema drift detected — run the relevant migration before "
            f"starting the server:\n{lines}"
        )

    print("[startup] Schema OK: all ORM columns present in live database.")


def _verify_seed_integrity(db: Session) -> None:
    """Log loud warnings if expected seed rows are missing after startup."""
    subject_count = db.query(Subject).count()
    lift_count = db.query(Lift).filter(Lift.is_active == True).count()

    if subject_count < 4:
        print(
            f"[STARTUP WARNING] Expected 4 seeded subjects, found {subject_count}. "
            "GET /subjects will return incomplete data. Re-check m002 seed logic."
        )
    else:
        print(f"[startup] Subjects OK: {subject_count} subjects present.")

    if lift_count < 5:
        print(
            f"[STARTUP WARNING] Expected 5 default lifts, found {lift_count}. "
            "GET /health/lifts will return incomplete data. Re-check m003 seed logic."
        )
    else:
        print(f"[startup] Lifts OK: {lift_count} active lifts present.")


# ── SCHEDULER — freeze ended week snapshots ──────────────────
# Runs on startup check and can be called manually
def freeze_ended_weeks_job():
    db = SessionLocal()
    try:
        snapshots = db.query(models.WeeklyTargetSnapshot).filter(
            models.WeeklyTargetSnapshot.week_end < date.today(),
            models.WeeklyTargetSnapshot.frozen == False
        ).all()
        for snapshot in snapshots:
            snapshot.frozen = True
        db.commit()
        print(f"[Startup freeze] Frozen {len(snapshots)} ended-week snapshots.")
    finally:
        db.close()


@app.on_event("startup")
async def run_freeze_on_startup():
    freeze_ended_weeks_job()


# ── VAULT SYNC — startup (non-blocking) + APScheduler ────────

def _vault_sync_job():
    from vault_sync import sync_vault as _sync, redact_credentials
    db = SessionLocal()
    try:
        result = _sync(db)
        print(f"[vault_scheduler] sync complete: {redact_credentials(result)}")
    except Exception as exc:
        print(f"[vault_scheduler] sync error: {redact_credentials(exc)}")
    finally:
        db.close()


@app.on_event("startup")
async def start_vault_scheduler():
    import threading
    # Non-blocking startup sync
    t = threading.Thread(target=_vault_sync_job, daemon=True, name="vault-startup-sync")
    t.start()

    # Scheduled recurring sync via APScheduler
    from apscheduler.schedulers.background import BackgroundScheduler
    from vault_sync import VAULT_SYNC_INTERVAL_HOURS
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        _vault_sync_job,
        "interval",
        hours=VAULT_SYNC_INTERVAL_HOURS,
        id="vault_sync",
        replace_existing=True,
    )
    scheduler.start()
    print(f"[vault_sync] Scheduler started — every {VAULT_SYNC_INTERVAL_HOURS}h.")


# ── SEED HELPERS ─────────────────────────────────────────────
HABIT_DEFAULTS = [
    ("scripture_prayer", "Scripture & Prayer (pre-5:20am)"),
    ("ironing",          "Clothes ironed night before"),
    ("python_session",   "Python / Aether (20:30–21:30)"),
    ("sprint_training",  "Sprint training"),
    ("academics",        "Academic study block"),
]

def _seed_habits_if_empty(db: Session):
    today = date.today()
    existing = db.query(Habit).filter(Habit.date == today).count()
    if existing == 0:
        for key, label in HABIT_DEFAULTS:
            db.add(Habit(key=key, label=label, done=False, date=today))
        db.commit()


def _seed_non_negotiables_if_empty(db: Session):
    """Seed NonNegotiable rows from HABIT_DEFAULTS if the table is empty."""
    from models import NonNegotiable
    if db.query(NonNegotiable).count() == 0:
        for i, (key, label) in enumerate(HABIT_DEFAULTS):
            db.add(NonNegotiable(key=key, label=label, is_active=True, sort_order=i))
        db.commit()


ROADMAP_DEFAULTS = [
    # Sprint
    ("sprint-base",   "sprint", "sprint-p1", True),
    ("sprint-form",   "sprint", "sprint-p1", True),
    ("sprint-acc",    "sprint", "sprint-p2", False),
    ("sprint-max",    "sprint", "sprint-p2", False),
    ("sprint-plyo",   "sprint", "sprint-p2", True),
    ("sprint-200se",  "sprint", "sprint-p3", False),
    ("sprint-fly30",  "sprint", "sprint-p3", False),
    ("sprint-400sub48","sprint","sprint-p3", False),
    ("sprint-taper",  "sprint", "sprint-p3", False),
    # Academic
    ("m-functions",   "academic", "acad-maths", True),
    ("m-coords",      "academic", "acad-maths", True),
    ("m-trigonometry","academic", "acad-maths", True),
    ("m-calc",        "academic", "acad-maths", False),
    ("m-stats",       "academic", "acad-maths", False),
    ("fm-polys",      "academic", "acad-fm",    True),
    ("fm-matrices",   "academic", "acad-fm",    False),
    ("fm-vectors",    "academic", "acad-fm",    False),
    ("fm-crv",        "academic", "acad-fm",    False),
    ("biz-people",    "academic", "acad-biz",   False),
    ("biz-ops",       "academic", "acad-biz",   False),
    ("biz-finance",   "academic", "acad-biz",   False),
    ("econ-micro",    "academic", "acad-econ",  False),
    ("econ-macro",    "academic", "acad-econ",  False),
    ("econ-market",   "academic", "acad-econ",  False),
]

def _seed_roadmap_tasks_if_empty(db: Session):
    if db.query(RoadmapTask).count() == 0:
        for task_id, roadmap, phase_id, done in ROADMAP_DEFAULTS:
            db.add(RoadmapTask(task_id=task_id, roadmap=roadmap, phase_id=phase_id, done=done))
        db.commit()


# ════════════════════════════════════════════════════════════
# ENDPOINTS
# ════════════════════════════════════════════════════════════

# ── HEALTH ───────────────────────────────────────────────────
#@app.get("/")
#def root():
#    return {"status": "Founder OS API v2 running", "date": str(date.today())}

@app.get("/health")
def health():
    return {"ok": True, "ai_configured": llm.is_configured(), "assistant_model": llm.assistant_model()}


@app.get("/ping")
def ping():
    return {"status": "ok", "ts": datetime.utcnow().isoformat()}


# ── KPIs ─────────────────────────────────────────────────────
@app.get("/kpis")
def get_kpis():
    return _kpi_state


@app.post("/kpis")
def update_kpis(payload: schemas.KPIBulkUpdate, db: Session = Depends(get_db)):
    global _kpi_state
    updated = []
    for u in payload.updates:
        if u.key in _kpi_state:
            _kpi_state[u.key]["value"] = u.value
            db.add(KPISnapshot(key=u.key, value=u.value, date=date.today()))
            updated.append(u.key)
    db.commit()
    return {"updated": updated, "kpis": _kpi_state}


@app.get("/kpis/history/{key}")
def kpi_history(key: str, limit: int = 30, db: Session = Depends(get_db)):
    snaps = db.query(KPISnapshot).filter(
        KPISnapshot.key == key
    ).order_by(KPISnapshot.date.desc()).limit(limit).all()
    return [{"date": str(s.date), "value": s.value} for s in reversed(snaps)]


# ── HABITS ───────────────────────────────────────────────────
@app.get("/habits")
def get_habits(for_date: Optional[date] = None, db: Session = Depends(get_db)):
    from models import NonNegotiable
    target_date = for_date or date.today()
    active_nns = db.query(NonNegotiable).filter(NonNegotiable.is_active == True).order_by(NonNegotiable.sort_order, NonNegotiable.id).all()
    if not active_nns:
        # Fallback: seed non-negotiables then retry
        _seed_non_negotiables_if_empty(db)
        active_nns = db.query(NonNegotiable).filter(NonNegotiable.is_active == True).order_by(NonNegotiable.sort_order, NonNegotiable.id).all()
    existing = {h.key: h for h in db.query(Habit).filter(Habit.date == target_date).all()}
    result = []
    for nn in active_nns:
        h = existing.get(nn.key)
        if h:
            result.append({"id": h.id, "key": h.key, "label": h.label, "done": h.done, "date": str(h.date)})
        else:
            result.append({"id": None, "key": nn.key, "label": nn.label, "done": False, "date": str(target_date)})
    return result


@app.post("/habits/toggle")
def toggle_habit(payload: schemas.HabitToggle, db: Session = Depends(get_db)):
    habit = db.query(Habit).filter(
        Habit.key == payload.key,
        Habit.date == payload.date
    ).first()
    if habit:
        habit.done = payload.done
    else:
        habit = Habit(key=payload.key, label=payload.label, done=payload.done, date=payload.date)
        db.add(habit)
    db.commit()
    return {"key": payload.key, "done": payload.done, "date": str(payload.date)}


@app.get("/habits/streak/{key}")
def habit_streak(key: str, db: Session = Depends(get_db)):
    """Return consecutive days done for a habit key."""
    records = db.query(Habit).filter(
        Habit.key == key,
        Habit.done == True
    ).order_by(Habit.date.desc()).all()
    streak = 0
    check = date.today()
    for r in records:
        if r.date == check:
            streak += 1
            check = check - timedelta(days=1)
        else:
            break
    return {"key": key, "streak": streak}


# ── ANNUAL TARGETS ───────────────────────────────────────────

def _serialize_annual_target(t: AnnualTarget, year_pct: float) -> dict:
    is_numeric = t.target_value is not None
    if is_numeric:
        pct = _at_progress_pct(t)
        expected = year_pct * 100
        gap = pct - expected
        if gap > 10:    status = "ahead"
        elif gap > -10: status = "on_track"
        elif gap > -25: status = "behind"
        else:           status = "critical"
    else:
        pct = 100 if t.is_complete else 0
        expected = year_pct * 100
        status = "done" if t.is_complete else "pending"
    return {
        "id":            t.id,
        "name":          t.name,
        "current_value": t.current_value,
        "target_value":  t.target_value,
        "unit":          t.unit,
        "display_value": t.display_value,
        "is_complete":   t.is_complete,
        "is_numeric":    is_numeric,
        "priority":      t.priority,
        "is_active":     t.is_active,
        "sort_order":    t.sort_order,
        "progress_pct":  round(pct),
        "expected_pct":  round(expected),
        "status":        status,
    }


@app.get("/annual-targets")
def get_annual_targets(db: Session = Depends(get_db)):
    targets = (db.query(AnnualTarget)
               .filter(AnnualTarget.is_active == True)
               .order_by(AnnualTarget.sort_order, AnnualTarget.id)
               .all())
    year_pct = _year_pct()
    return [_serialize_annual_target(t, year_pct) for t in targets]


@app.get("/annual-targets/all")
def get_all_annual_targets(db: Session = Depends(get_db)):
    """Returns all targets including inactive ones."""
    targets = db.query(AnnualTarget).order_by(AnnualTarget.sort_order, AnnualTarget.id).all()
    year_pct = _year_pct()
    return [_serialize_annual_target(t, year_pct) for t in targets]


@app.post("/annual-targets")
def create_annual_target(payload: schemas.AnnualTargetCreate, db: Session = Depends(get_db)):
    t = AnnualTarget(**payload.model_dump())
    db.add(t)
    db.commit()
    db.refresh(t)
    return _serialize_annual_target(t, _year_pct())


@app.patch("/annual-targets/{target_id}")
def update_annual_target(target_id: int, payload: schemas.AnnualTargetUpdate, db: Session = Depends(get_db)):
    t = db.query(AnnualTarget).filter(AnnualTarget.id == target_id).first()
    if not t:
        raise HTTPException(404, "Annual target not found")
    for field, val in payload.model_dump(exclude_none=True).items():
        setattr(t, field, val)
    t.updated_at = datetime.utcnow()
    db.commit()
    return _serialize_annual_target(t, _year_pct())


@app.delete("/annual-targets/{target_id}")
def delete_annual_target(target_id: int, db: Session = Depends(get_db)):
    t = db.query(AnnualTarget).filter(AnnualTarget.id == target_id).first()
    if not t:
        raise HTTPException(404, "Annual target not found")
    db.delete(t)
    db.commit()
    return {"deleted": target_id}


# ── ROADMAP TASKS ────────────────────────────────────────────
@app.get("/roadmap/{roadmap_type}")
def get_roadmap(roadmap_type: str, db: Session = Depends(get_db)):
    tasks = db.query(RoadmapTask).filter(RoadmapTask.roadmap == roadmap_type).all()
    return [{"task_id": t.task_id, "phase_id": t.phase_id, "done": t.done, "pushed_count": t.pushed_count} for t in tasks]


@app.post("/roadmap/update")
def update_roadmap_tasks(payload: schemas.RoadmapBulkUpdate, db: Session = Depends(get_db)):
    updated = []
    for u in payload.tasks:
        task = db.query(RoadmapTask).filter(RoadmapTask.task_id == u.task_id).first()
        if task:
            task.done = u.done
            task.updated_at = datetime.utcnow()
        else:
            task = RoadmapTask(task_id=u.task_id, roadmap=u.roadmap, phase_id=u.phase_id, done=u.done)
            db.add(task)
        updated.append(u.task_id)
    db.commit()
    return {"updated": updated}

# ── TODOS ────────────────────────────────────────────────────
@app.get("/todos")
def get_todos(db: Session = Depends(get_db)):
    return db.query(models.Todo).all()

@app.post("/todos")
def create_todo(todo: dict, db: Session = Depends(get_db)):
    new_todo = models.Todo(
        text=todo.get("text"),
        priority=todo.get("priority", 5),
        done=False,
        category=todo.get("category", "personal"),
        due=todo.get("due", str(date.today())),
        due_date=_due_date_from(todo.get("due", str(date.today()))),
        source=todo.get("source", "manual"),
        roadmap_id=todo.get("roadmapId")
    )
    db.add(new_todo)
    db.commit()
    db.refresh(new_todo)
    return new_todo

@app.post("/todos/{todo_id}/complete")
def complete_todo(todo_id: int, db: Session = Depends(get_db)):
    """One-tap completion from the briefing. Recorded in the change log so it can be undone."""
    import change_log
    todo = db.query(models.Todo).filter(models.Todo.id == todo_id).first()
    if not todo:
        raise HTTPException(404, "Todo not found")
    with change_log.batch_scope(db, "ui"):
        if todo.done:
            change = None
        else:
            before = change_log.snapshot(todo)
            todo.done = True
            todo.completed_at = date.today()
            change = change_log.record_update(db, todo, before, f"Completed task: {todo.text[:80]}")
        db.commit()
    return {"id": todo.id, "done": todo.done, "completed_at": str(todo.completed_at) if todo.completed_at else None,
            "change": change}


@app.post("/todos/{todo_id}/toggle")
def toggle_todo(todo_id: int, db: Session = Depends(get_db)):
    todo = db.query(models.Todo).filter(models.Todo.id == todo_id).first()
    if not todo:
        raise HTTPException(404, "Todo not found")
    todo.done = not todo.done
    todo.completed_at = date.today() if todo.done else None
    db.commit()
    return {"id": todo.id, "done": todo.done, "completed_at": str(todo.completed_at) if todo.completed_at else None}

# ── RADAR ────────────────────────────────────────────────────
import time as _time

# Date-keyed 60s cache. SQLite + sync SQLAlchemy means async/gather would block
# the event loop, so we cache instead. 60s staleness is acceptable for radar.
_radar_cache: dict = {"key": None, "ts": 0.0, "payload": None}
_RADAR_TTL = 60.0


@app.get("/radar")
def get_radar(db: Session = Depends(get_db)):
    """Compute radar domain scores from live system data."""
    today_key = date.today()
    now = _time.monotonic()
    if (
        _radar_cache["key"] == today_key
        and _radar_cache["payload"] is not None
        and now - _radar_cache["ts"] < _RADAR_TTL
    ):
        return _radar_cache["payload"]

    habits_today = {h.key: h.done for h in db.query(Habit).filter(Habit.date == date.today()).all()}
    annual = [
        {"name": t.name, "current": t.current_value, "target": t.target_value}
        for t in db.query(AnnualTarget).filter(
            AnnualTarget.is_active == True,
            AnnualTarget.target_value != None,
        ).all()
    ]

    # Bible streak from habits
    bible_records = db.query(Habit).filter(
        Habit.key == "scripture_prayer", Habit.done == True
    ).order_by(Habit.date.desc()).all()
    streak = 0
    check = date.today()
    for r in bible_records:
        if r.date == check:
            streak += 1
            check -= timedelta(days=1)
        else:
            break

    # Roadmap completion pct
    sprint_tasks = db.query(RoadmapTask).filter(RoadmapTask.roadmap == "sprint").all()
    acad_tasks = db.query(RoadmapTask).filter(RoadmapTask.roadmap == "academic").all()
    sprint_pct = round(sum(1 for t in sprint_tasks if t.done) / max(len(sprint_tasks), 1) * 100)
    acad_pct = round(sum(1 for t in acad_tasks if t.done) / max(len(acad_tasks), 1) * 100)

    social_snap = db.query(SocialScore).order_by(SocialScore.date.desc()).first()
    social_score = social_snap.value if social_snap else 50

    clients_all = db.query(Client).all()
    revenue_total = db.query(func.sum(Revenue.amount)).scalar() or 0

    # Academic mastery from Subject/Topic/Subtopic (single source of truth)
    academic_progress = _compute_subject_progress(db)
    acad_mastery = {row["code"]: row["weighted_pct"] for row in academic_progress}

    # Health axis data — last 7 days of DailyHealth rows
    health_context = _compute_health_radar(db)

    context = {
        "kpis": _kpi_state,
        "habits": habits_today,
        "annual_targets": annual,
        "bible_streak": streak,
        "roadmap_pct": {"sprint": sprint_pct, "academic": acad_pct},
        "social_score": social_score,
        "clients": [{"name": c.name, "status": c.status, "value": c.value} for c in clients_all],
        "revenue_total": revenue_total,
        "acad_mastery": acad_mastery,
        "health": health_context,
    }

    scores = get_radar_scores(context)
    composite = round(sum(scores.values()) / len(scores))
    payload = {"scores": scores, "composite": composite, "computed_at": str(datetime.utcnow())}
    _radar_cache["key"] = today_key
    _radar_cache["ts"] = now
    _radar_cache["payload"] = payload
    return payload


# ── ACADEMIC ROADMAP ─────────────────────────────────────────

def _compute_subject_progress(db: Session) -> list[dict]:
    """
    Aggregate mastery per subject.  Called by both /subjects/progress
    and /radar so the intellect score and the dashboard KPIs share
    exactly one computation path.

    Returns a list of dicts:
      {subject_id, subject_name, code, mastery_pct, weighted_pct,
       subtopic_count}

    mastery_pct  = simple average mastery across all subtopics
    weighted_pct = Σ(topic.weight * avg_mastery_in_topic) / Σ(topic.weight)
                   Topics with zero subtopics are excluded from weighting.
    Both are None when the subject has zero subtopics across all topics.
    """
    subjects = db.query(Subject).order_by(Subject.sort_order).all()
    results = []
    for subj in subjects:
        all_mastery: list[int] = []
        weighted_sum = 0.0
        weight_total = 0
        for topic in subj.topics:
            if not topic.subtopics:
                continue
            topic_avg = sum(s.mastery_level for s in topic.subtopics) / len(topic.subtopics)
            all_mastery.extend(s.mastery_level for s in topic.subtopics)
            weighted_sum += topic.syllabus_weight * topic_avg
            weight_total += topic.syllabus_weight

        subtopic_count = len(all_mastery)
        mastery_pct  = round(sum(all_mastery) / subtopic_count, 1) if subtopic_count else None
        weighted_pct = round(weighted_sum / weight_total, 1)       if weight_total  else None

        results.append({
            "subject_id":     subj.id,
            "subject_name":   subj.name,
            "code":           subj.code,
            "exam_date":      str(subj.exam_date) if subj.exam_date else None,
            "mastery_pct":    mastery_pct,
            "weighted_pct":   weighted_pct,
            "subtopic_count": subtopic_count,
        })
    return results


def _serialize_subject(s: Subject) -> dict:
    return {
        "id":         s.id,
        "name":       s.name,
        "code":       s.code,
        "exam_date":  str(s.exam_date) if s.exam_date else None,
        "sort_order": s.sort_order,
        "topics": [_serialize_topic(t) for t in s.topics],
    }


def _serialize_topic(t: Topic) -> dict:
    return {
        "id":               t.id,
        "subject_id":       t.subject_id,
        "name":             t.name,
        "syllabus_weight":  t.syllabus_weight,
        "sort_order":       t.sort_order,
        "subtopics": [_serialize_subtopic(st) for st in t.subtopics],
    }


def _serialize_subtopic(st: Subtopic) -> dict:
    return {
        "id":                 st.id,
        "topic_id":           st.topic_id,
        "name":               st.name,
        "mastery_level":      st.mastery_level,
        "last_reviewed_date": str(st.last_reviewed_date) if st.last_reviewed_date else None,
        "notes":              st.notes,
        "sort_order":         st.sort_order,
    }


# Subject CRUD
@app.get("/subjects")
def list_subjects(db: Session = Depends(get_db)):
    subjects = db.query(Subject).order_by(Subject.sort_order).all()
    return [_serialize_subject(s) for s in subjects]


@app.post("/subjects", status_code=201)
def create_subject(payload: schemas.SubjectCreate, db: Session = Depends(get_db)):
    existing = db.query(Subject).filter(Subject.code == payload.code).first()
    if existing:
        raise HTTPException(400, f"Subject with code '{payload.code}' already exists")
    subj = Subject(**payload.model_dump())
    db.add(subj)
    db.commit()
    db.refresh(subj)
    return _serialize_subject(subj)


@app.patch("/subjects/{subject_id}")
def update_subject(subject_id: int, payload: schemas.SubjectUpdate, db: Session = Depends(get_db)):
    subj = db.query(Subject).filter(Subject.id == subject_id).first()
    if not subj:
        raise HTTPException(404, "Subject not found")
    for field, val in payload.model_dump(exclude_none=True).items():
        setattr(subj, field, val)
    db.commit()
    db.refresh(subj)
    return _serialize_subject(subj)


@app.delete("/subjects/{subject_id}", status_code=204)
def delete_subject(subject_id: int, db: Session = Depends(get_db)):
    subj = db.query(Subject).filter(Subject.id == subject_id).first()
    if not subj:
        raise HTTPException(404, "Subject not found")
    db.delete(subj)
    db.commit()


# Topic CRUD
@app.post("/subjects/{subject_id}/topics", status_code=201)
def create_topic(subject_id: int, payload: schemas.TopicCreate, db: Session = Depends(get_db)):
    subj = db.query(Subject).filter(Subject.id == subject_id).first()
    if not subj:
        raise HTTPException(404, "Subject not found")
    topic = Topic(subject_id=subject_id, **payload.model_dump())
    db.add(topic)
    db.commit()
    db.refresh(topic)
    return _serialize_topic(topic)


@app.patch("/topics/{topic_id}")
def update_topic(topic_id: int, payload: schemas.TopicUpdate, db: Session = Depends(get_db)):
    topic = db.query(Topic).filter(Topic.id == topic_id).first()
    if not topic:
        raise HTTPException(404, "Topic not found")
    for field, val in payload.model_dump(exclude_none=True).items():
        setattr(topic, field, val)
    db.commit()
    db.refresh(topic)
    return _serialize_topic(topic)


@app.delete("/topics/{topic_id}", status_code=204)
def delete_topic(topic_id: int, db: Session = Depends(get_db)):
    topic = db.query(Topic).filter(Topic.id == topic_id).first()
    if not topic:
        raise HTTPException(404, "Topic not found")
    db.delete(topic)
    db.commit()


# Subtopic CRUD
@app.post("/topics/{topic_id}/subtopics", status_code=201)
def create_subtopic(topic_id: int, payload: schemas.SubtopicCreate, db: Session = Depends(get_db)):
    topic = db.query(Topic).filter(Topic.id == topic_id).first()
    if not topic:
        raise HTTPException(404, "Topic not found")
    st = Subtopic(topic_id=topic_id, **payload.model_dump())
    db.add(st)
    db.commit()
    db.refresh(st)
    return _serialize_subtopic(st)


@app.patch("/subtopics/{subtopic_id}")
def update_subtopic(subtopic_id: int, payload: schemas.SubtopicUpdate, db: Session = Depends(get_db)):
    st = db.query(Subtopic).filter(Subtopic.id == subtopic_id).first()
    if not st:
        raise HTTPException(404, "Subtopic not found")
    updates = payload.model_dump(exclude_none=True)
    # Auto-set last_reviewed_date when mastery changes (unless caller provided a date)
    if "mastery_level" in updates and "last_reviewed_date" not in updates:
        updates["last_reviewed_date"] = date.today()
    for field, val in updates.items():
        setattr(st, field, val)
    db.commit()
    db.refresh(st)
    return _serialize_subtopic(st)


@app.delete("/subtopics/{subtopic_id}", status_code=204)
def delete_subtopic(subtopic_id: int, db: Session = Depends(get_db)):
    st = db.query(Subtopic).filter(Subtopic.id == subtopic_id).first()
    if not st:
        raise HTTPException(404, "Subtopic not found")
    db.delete(st)
    db.commit()


# Progress + Weakest
@app.get("/subjects/progress")
def subjects_progress(db: Session = Depends(get_db)):
    return _compute_subject_progress(db)


@app.get("/subjects/weakest")
def weakest_subtopics(limit: int = Query(5, ge=1, le=50), db: Session = Depends(get_db)):
    """
    Returns the N subtopics with the lowest mastery_level, ordered by
    parent topic's syllabus_weight DESC then mastery_level ASC.
    Topics with higher weight surface weak subtopics first.
    """
    rows = (
        db.query(Subtopic, Topic, Subject)
        .join(Topic, Subtopic.topic_id == Topic.id)
        .join(Subject, Topic.subject_id == Subject.id)
        .order_by(Topic.syllabus_weight.desc(), Subtopic.mastery_level.asc())
        .limit(limit)
        .all()
    )
    return [
        {
            "subtopic_id":   st.id,
            "name":          st.name,
            "mastery_level": st.mastery_level,
            "topic_name":    t.name,
            "topic_weight":  t.syllabus_weight,
            "subject_name":  s.name,
            "subject_code":  s.code,
        }
        for st, t, s in rows
    ]


# Defined AFTER the static /subjects/progress and /subjects/weakest routes so
# the path param does not shadow them.
@app.get("/subjects/{subject_id}")
def get_subject(subject_id: int, db: Session = Depends(get_db)):
    subj = db.query(Subject).filter(Subject.id == subject_id).first()
    if not subj:
        raise HTTPException(404, "Subject not found")
    return _serialize_subject(subj)


# ── HEALTH MODULE ────────────────────────────────────────────

# Mon=0, Tue=1, Wed=2, Fri=4, Sat=5
TRAINING_WEEKDAYS = {0, 1, 2, 4, 5}


def _get_or_create_daily_health(db: Session, for_date: date) -> DailyHealth:
    row = db.query(DailyHealth).filter(DailyHealth.date == for_date).first()
    if not row:
        row = DailyHealth(date=for_date)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def _get_or_create_weekly_health(db: Session, week_start: date) -> WeeklyHealth:
    row = db.query(WeeklyHealth).filter(WeeklyHealth.week_start_date == week_start).first()
    if not row:
        row = WeeklyHealth(week_start_date=week_start)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def _current_week_start() -> date:
    today = date.today()
    return today - timedelta(days=today.weekday())  # last Monday


def _serialize_daily_health(r: DailyHealth) -> dict:
    return {
        "id": r.id, "date": str(r.date),
        "sleep_hours": r.sleep_hours, "mobility_done": r.mobility_done,
        "session_done": r.session_done, "notes": r.notes,
    }


def _serialize_lift(r: Lift) -> dict:
    return {"id": r.id, "name": r.name, "sort_order": r.sort_order, "is_active": r.is_active}


def _serialize_lift_log(r: LiftLog) -> dict:
    return {
        "id": r.id, "lift_id": r.lift_id, "lift_name": r.lift_name,
        "date": str(r.date), "weight_kg": r.weight_kg, "reps": r.reps,
        "notes": r.notes,
    }


def _get_or_create_lift(db: Session, name: str) -> Lift:
    """Case-insensitive lookup; creates new active Lift if not found."""
    lift = db.query(Lift).filter(Lift.name.ilike(name)).first()
    if not lift:
        lift = Lift(name=name, sort_order=0, is_active=True)
        db.add(lift)
        db.commit()
        db.refresh(lift)
    return lift


def _serialize_weekly_health(r: WeeklyHealth) -> dict:
    return {
        "id": r.id, "week_start_date": str(r.week_start_date),
        "bodyweight_kg": r.bodyweight_kg,
        "protein_target_hit": r.protein_target_hit,
        "any_lift_progressed": r.any_lift_progressed,
        "energy_level": r.energy_level,
    }


def _compute_health_radar(db: Session) -> dict:
    """
    Compute the Health radar axis from the last 7 DailyHealth rows.
    Returns None for each component when fewer than 3 rows exist.
    """
    cutoff = date.today() - timedelta(days=6)
    rows = db.query(DailyHealth).filter(DailyHealth.date >= cutoff).all()
    if len(rows) < 3:
        return {"insufficient_data": True, "score": None}

    mobility_pct = sum(1 for r in rows if r.mobility_done) / 7 * 100

    training_days_in_window = sum(
        1 for i in range(7)
        if ((date.today() - timedelta(days=i)).weekday() in TRAINING_WEEKDAYS)
    )
    sessions_done = sum(1 for r in rows if r.session_done)
    session_pct = (sessions_done / max(training_days_in_window, 1)) * 100

    sleep_vals = [r.sleep_hours for r in rows if r.sleep_hours is not None]
    if sleep_vals:
        avg_sleep = sum(sleep_vals) / len(sleep_vals)
        sleep_score = min(100, (avg_sleep / 8) * 100)
    else:
        sleep_score = 0

    score = round(0.4 * mobility_pct + 0.4 * session_pct + 0.2 * sleep_score)
    return {
        "insufficient_data": False,
        "score": max(0, min(100, score)),
        "mobility_pct": round(mobility_pct),
        "session_pct": round(session_pct),
        "sleep_score": round(sleep_score),
        "rows_in_window": len(rows),
    }


@app.get("/health/daily/today")
def get_daily_health_today(db: Session = Depends(get_db)):
    return _serialize_daily_health(_get_or_create_daily_health(db, date.today()))


@app.patch("/health/daily/{log_date}")
def patch_daily_health(log_date: date, payload: schemas.DailyHealthPatch, db: Session = Depends(get_db)):
    row = _get_or_create_daily_health(db, log_date)
    for field, val in payload.model_dump(exclude_none=True).items():
        setattr(row, field, val)
    db.commit()
    db.refresh(row)
    return _serialize_daily_health(row)


@app.get("/health/daily/recent")
def get_daily_health_recent(days: int = Query(14, ge=1, le=90), db: Session = Depends(get_db)):
    cutoff = date.today() - timedelta(days=days - 1)
    rows = db.query(DailyHealth).filter(DailyHealth.date >= cutoff).order_by(DailyHealth.date.desc()).all()
    return [_serialize_daily_health(r) for r in rows]


@app.get("/health/weekly/current")
def get_weekly_health_current(db: Session = Depends(get_db)):
    return _serialize_weekly_health(_get_or_create_weekly_health(db, _current_week_start()))


@app.patch("/health/weekly/{week_start}")
def patch_weekly_health(week_start: date, payload: schemas.WeeklyHealthPatch, db: Session = Depends(get_db)):
    row = _get_or_create_weekly_health(db, week_start)
    for field, val in payload.model_dump(exclude_none=True).items():
        setattr(row, field, val)
    db.commit()
    db.refresh(row)
    return _serialize_weekly_health(row)


@app.get("/health/weekly/history")
def get_weekly_health_history(weeks: int = Query(8, ge=1, le=52), db: Session = Depends(get_db)):
    cutoff = _current_week_start() - timedelta(weeks=weeks - 1)
    rows = db.query(WeeklyHealth).filter(
        WeeklyHealth.week_start_date >= cutoff
    ).order_by(WeeklyHealth.week_start_date.desc()).all()
    return [_serialize_weekly_health(r) for r in rows]


@app.get("/health/lifts/progression")
def get_lift_progression(db: Session = Depends(get_db)):
    active_lifts = db.query(Lift).filter(Lift.is_active == True).order_by(Lift.sort_order, Lift.name).all()
    results = []
    for lift in active_lifts:
        logs = db.query(LiftLog).filter(LiftLog.lift_id == lift.id).all()
        best: LiftLog | None = None
        for lg in logs:
            if best is None or (lg.weight_kg, lg.reps) > (best.weight_kg, best.reps):
                best = lg
        results.append({
            "lift_name":      lift.name,
            "best_weight":    best.weight_kg if best else None,
            "best_reps":      best.reps if best else None,
            "best_date":      str(best.date) if best else None,
            "sessions_logged": len(logs),
        })
    return results


# ── Lift CRUD ─────────────────────────────────────────────────

@app.get("/health/lifts")
def list_lifts(db: Session = Depends(get_db)):
    lifts = db.query(Lift).filter(Lift.is_active == True).order_by(Lift.sort_order, Lift.name).all()
    return [_serialize_lift(l) for l in lifts]


@app.post("/health/lifts", status_code=201)
def create_lift(payload: schemas.LiftCreate, db: Session = Depends(get_db)):
    existing = db.query(Lift).filter(Lift.name.ilike(payload.name)).first()
    if existing:
        raise HTTPException(400, f"Lift '{payload.name}' already exists")
    lift = Lift(name=payload.name, sort_order=payload.sort_order)
    db.add(lift)
    db.commit()
    db.refresh(lift)
    return _serialize_lift(lift)


@app.patch("/health/lifts/{lift_id}")
def update_lift(lift_id: int, payload: schemas.LiftPatch, db: Session = Depends(get_db)):
    lift = db.query(Lift).filter(Lift.id == lift_id).first()
    if not lift:
        raise HTTPException(404, "Lift not found")
    for field, val in payload.model_dump(exclude_none=True).items():
        setattr(lift, field, val)
    db.commit()
    db.refresh(lift)
    return _serialize_lift(lift)


@app.delete("/health/lifts/{lift_id}", status_code=200)
def deactivate_lift(lift_id: int, db: Session = Depends(get_db)):
    """Soft-delete: sets is_active=False to preserve historical LiftLog data."""
    lift = db.query(Lift).filter(Lift.id == lift_id).first()
    if not lift:
        raise HTTPException(404, "Lift not found")
    lift.is_active = False
    db.commit()
    return {"id": lift_id, "is_active": False}


# ── LiftLog CRUD ──────────────────────────────────────────────

@app.post("/health/lift-logs", status_code=201)
def create_lift_log(payload: schemas.LiftLogCreate, db: Session = Depends(get_db)):
    lift = _get_or_create_lift(db, payload.lift_name)
    log = LiftLog(
        lift_id=lift.id, lift_name=lift.name,
        date=payload.date, weight_kg=payload.weight_kg,
        reps=payload.reps, notes=payload.notes,
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return _serialize_lift_log(log)


@app.get("/health/lift-logs/today")
def get_lift_logs_today(db: Session = Depends(get_db)):
    logs = db.query(LiftLog).filter(LiftLog.date == date.today()).order_by(LiftLog.id).all()
    return [_serialize_lift_log(l) for l in logs]


@app.get("/health/lift-logs/recent")
def get_lift_logs_recent(days: int = Query(14, ge=1, le=90), db: Session = Depends(get_db)):
    cutoff = date.today() - timedelta(days=days - 1)
    logs = db.query(LiftLog).filter(LiftLog.date >= cutoff).order_by(LiftLog.date.desc(), LiftLog.id).all()
    return [_serialize_lift_log(l) for l in logs]


@app.patch("/health/lift-logs/{log_id}")
def update_lift_log(log_id: int, payload: schemas.LiftLogPatch, db: Session = Depends(get_db)):
    log = db.query(LiftLog).filter(LiftLog.id == log_id).first()
    if not log:
        raise HTTPException(404, "Lift log entry not found")
    for field, val in payload.model_dump(exclude_none=True).items():
        setattr(log, field, val)
    db.commit()
    db.refresh(log)
    return _serialize_lift_log(log)


@app.delete("/health/lift-logs/{log_id}", status_code=200)
def delete_lift_log(log_id: int, db: Session = Depends(get_db)):
    log = db.query(LiftLog).filter(LiftLog.id == log_id).first()
    if not log:
        raise HTTPException(404, "Lift log entry not found")
    db.delete(log)
    db.commit()
    return {"deleted": log_id}


# ── SHARED PARSE-APPLICATION HELPER ─────────────────────────
# ── CURRENCY ─────────────────────────────────────────────────
DEFAULT_ZMW_PER_USD = 27.0


def _zmw_per_usd() -> float:
    """Kwacha per dollar for the $10K scoreboard. Set ZMW_PER_USD on Render."""
    try:
        v = float(os.getenv("ZMW_PER_USD", DEFAULT_ZMW_PER_USD))
        return v if v > 0 else DEFAULT_ZMW_PER_USD
    except ValueError:
        return DEFAULT_ZMW_PER_USD


def _to_usd(amount: float, currency: str | None) -> float:
    cur = (currency or "USD").upper()
    if cur in ("ZMW", "K", "KWACHA"):
        return float(amount) / _zmw_per_usd()
    return float(amount)


def _revenue_usd_total(db: Session, since: date | None = None) -> float:
    q = db.query(Revenue)
    if since is not None:
        q = q.filter(Revenue.date >= since)
    return round(sum(_to_usd(r.amount, r.currency) for r in q.all()), 2)


def _normalise_currency(value) -> str:
    v = (str(value or "USD")).strip().upper()
    if v in ("K", "ZMW", "KWACHA", "ZK"):
        return "ZMW"
    if v in ("$", "USD", "DOLLAR", "DOLLARS", "US$"):
        return "USD"
    return v[:3] if v else "USD"


# ── DUE DATES ────────────────────────────────────────────────
def _due_date_from(value) -> date | None:
    from migrations.m012_notes_currency_retrieval import parse_due
    return parse_due(value)


# ── TODO FUZZY MATCHING ──────────────────────────────────────
_COMPLETE_STRIP = {
    "finished", "finish", "finishing", "completed", "complete", "done", "did", "do",
    "the", "my", "a", "an", "that", "this", "with", "off", "up", "task", "todo", "today",
}


def _todo_words(text: str) -> set[str]:
    import re as _re
    return {w for w in _re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 1 and w not in _COMPLETE_STRIP}


def match_open_todos(db: Session, fragment: str, min_score: float = 0.5) -> list[tuple[float, "models.Todo"]]:
    """Score open todos against a free-text fragment. Highest first."""
    import difflib
    frag = " ".join(sorted(_todo_words(fragment)))
    fw = _todo_words(fragment)
    if not fw:
        return []
    scored = []
    for t in db.query(models.Todo).filter(models.Todo.done == False).all():
        tw = _todo_words(t.text)
        if not tw:
            continue
        overlap = len(fw & tw) / min(len(fw), len(tw))
        ratio = difflib.SequenceMatcher(None, frag, " ".join(sorted(tw))).ratio()
        tl, fl = (t.text or "").lower(), (fragment or "").lower().strip()
        contain = 1.0 if (fl and (fl in tl or tl in fl)) else 0.0
        score = max(overlap * 0.9, ratio, contain)
        if score >= min_score:
            scored.append((round(score, 3), t))
    scored.sort(key=lambda x: (x[0], -(x[1].id)), reverse=True)
    return scored


def resolve_todo_completion(db: Session, fragment: str):
    """(todo, candidates). ``todo`` is set when exactly one open task clearly matches;
    otherwise ``candidates`` lists the plausible ones (empty when nothing matches)."""
    scored = match_open_todos(db, fragment)
    if not scored:
        return None, []
    top, second = scored[0][0], (scored[1][0] if len(scored) > 1 else 0.0)
    if top >= 0.75 and (top - second) >= 0.2:
        return scored[0][1], []
    return None, scored[:3]


def _intent_confidence_min() -> float:
    try:
        return float(os.getenv("INTENT_CONFIDENCE_MIN", "0.7"))
    except ValueError:
        return 0.7


def apply_parse_updates(db: Session, parsed: dict, today: date) -> dict:
    """
    Apply every update extracted by the AI parser to the database.

    Called by /parse, /input, the orchestrator's route_brain_dump tool and
    suggestion acceptance, so there is exactly one copy of this logic.

    Atomic: everything is applied in one transaction and committed once at the end;
    any failure rolls back every write. Every write is recorded in the change log
    (one batch per call unless an assistant batch is already open). The in-memory KPI
    state is only updated after the commit succeeds so a rollback can never desync it.

    Returns a structured summary of what was applied, plus ``changes`` and ``batch_id``.
    """
    import change_log

    with change_log.batch_scope(db, "parser"):
        try:
            result = _apply_parse_updates_inner(db, parsed, today)
            db.commit()
        except Exception:
            db.rollback()
            raise
        for key, value in result.pop("_kpi_pending", {}).items():
            _kpi_state[key]["value"] = value
        result["changes"] = change_log.current_batch_changes(db)
        result["batch_id"] = change_log.current_batch_id(db)
    return result


def _apply_parse_updates_inner(db: Session, parsed: dict, today: date) -> dict:
    import change_log
    import actions
    import notes as notes_mod

    result: dict = {
        "habits_updated":          [],   # list of {key, label, strategy}
        "kpi_updates_applied":     [],   # list of key strings
        "todos_added":             [],   # list of text strings
        "todos_completed":         [],   # list of {id, text}
        "ideas_inboxed":           [],   # list of idea texts appended to the Inbox note
        "discussion_skipped":      [],   # list of texts classified as discussion (nothing written)
        "suggestions":             [],   # low-confidence items awaiting one-tap acceptance
        "roadmap_completed":       [],   # list of task_id strings
        "annual_updates_applied":  [],   # list of name_fragment strings
        "reading_updates_applied":    [],   # list of book titles marked currently-reading
        "reading_match_failures":     [],   # list of fragments that didn't match any book
        "bible_log_entries_created":  0,    # count of DailyBibleLog rows created
        "revenue_logged":             [],   # list of {amount, source}
        "log_entry_created":       False,
        "_kpi_pending":            {},   # key -> value, applied to _kpi_state after commit
    }
    source_text = parsed.get("summary") or parsed.get("log_entry")

    # ── KPI updates ──────────────────────────────────────────
    for u in parsed.get("kpi_updates") or []:
        if u.get("key") in _kpi_state and u.get("value") is not None:
            snap = KPISnapshot(key=u["key"], value=u["value"], date=today)
            db.add(snap)
            change_log.record_create(db, snap, f"KPI {u['key']} → {u['value']}")
            result["_kpi_pending"][u["key"]] = u["value"]
            result["kpi_updates_applied"].append(u["key"])

    # ── Roadmap completions ───────────────────────────────────
    for task_id in parsed.get("roadmap_complete") or []:
        task = db.query(RoadmapTask).filter(RoadmapTask.task_id == task_id).first()
        if task and not task.done:
            before = change_log.snapshot(task)
            task.done = True
            change_log.record_update(db, task, before, f"Roadmap task {task_id} completed")
            result["roadmap_completed"].append(task_id)
        elif task:
            result["roadmap_completed"].append(task_id)

    # ── Non-negotiables — matched against the ACTIVE non_negotiables (exact key →
    #    label substring → shared word). The day's tick row is created when missing, so
    #    logging works on any day, not only on the day the server started.
    if parsed.get("habits_done"):
        import assistant_state as _state
        for ai_key in parsed["habits_done"]:
            hit = _state.mark_non_negotiable_done(db, str(ai_key), today)
            if hit:
                result["habits_updated"].append(hit)
                print(f"[parse] non-negotiable '{ai_key}' matched '{hit['key']}' via {hit['strategy']}")
            else:
                print(f"[parse] non-negotiable '{ai_key}' — no active match")

    # ── Annual target updates ─────────────────────────────────
    for u in parsed.get("annual_updates") or []:
        frag = (u.get("name_fragment") or "").lower()
        if not frag or u.get("current") is None:
            continue
        target = db.query(AnnualTarget).filter(
            func.lower(AnnualTarget.name).contains(frag)
        ).first()
        if target:
            before = change_log.snapshot(target)
            target.current_value = u["current"]
            target.updated_at = datetime.utcnow()
            change_log.record_update(db, target, before, f"Annual target '{target.name}' → {u['current']}")
            result["annual_updates_applied"].append(frag)

    # ── Reading updates ───────────────────────────────────────
    for u in parsed.get("reading_updates") or []:
        frag = (u.get("title_fragment") or "").lower()
        if not frag:
            continue
        book = db.query(models.Book).filter(
            func.lower(models.Book.title).contains(frag)
        ).first()
        if book:
            # Clear current flag on all books, set only the matched one
            for other in db.query(models.Book).filter(models.Book.is_currently_reading == True).all():
                if other.id != book.id:
                    b4 = change_log.snapshot(other)
                    other.is_currently_reading = False
                    change_log.record_update(db, other, b4, f"'{other.title}' no longer current")
            before = change_log.snapshot(book)
            book.is_currently_reading = True
            change_log.record_update(db, book, before, f"Now reading '{book.title}'")
            result["reading_updates_applied"].append(book.title)
        else:
            result["reading_match_failures"].append(frag)
        progress = (u.get("progress") or "").strip()
        if progress:
            note = notes_mod.book_entry(db, book.title if book else u.get("title_fragment", "").strip(), progress, on=today)
            result.setdefault("reading_notes_appended", []).append(note.title)

    # ── Bible log entries ─────────────────────────────────────
    for bl in parsed.get("bible_log") or []:
        book_name = (bl.get("book") or "").strip()
        chapter   = bl.get("chapter")
        if not book_name or not chapter:
            continue
        entry = DailyBibleLog(date=today, book=book_name, chapter=int(chapter), notes=bl.get("notes"))
        db.add(entry)
        change_log.record_create(db, entry, f"Bible: {book_name} {chapter}")
        result["bible_log_entries_created"] += 1
        notes_mod.bible_entry(db, book_name, int(chapter), bl.get("notes"), on=today)
        # Advance matching active ReadingPlan if current_book matches
        plan = db.query(ReadingPlan).filter(
            ReadingPlan.is_active == True,
            func.lower(ReadingPlan.current_book) == book_name.lower(),
        ).first()
        if plan and plan.current_chapter != int(chapter):
            before = change_log.snapshot(plan)
            plan.current_chapter = int(chapter)
            change_log.record_update(db, plan, before, f"Reading plan '{plan.name}' at {book_name} {chapter}")

    # ── Todo additions — intent-classified ────────────────────
    #   commitment → task (if confident)   idea → Inbox note   discussion → nothing
    #   below INTENT_CONFIDENCE_MIN → suggestion, written only when accepted
    threshold = _intent_confidence_min()
    for t in parsed.get("todos_add") or []:
        text = (t.get("text") or "").strip()
        if not text:
            continue
        intent = (t.get("intent") or "commitment").strip().lower()
        if intent not in ("commitment", "idea", "discussion"):
            intent = "commitment"
        conf = t.get("confidence")
        conf = 1.0 if conf is None else max(0.0, min(1.0, float(conf)))
        if intent == "discussion":
            result["discussion_skipped"].append(text)
            continue
        if conf < threshold:
            kind = "todo_add" if intent == "commitment" else "inbox_add"
            payload = {"text": text, "priority": t.get("priority", 5),
                       "category": t.get("category", "personal"), "due": t.get("due"),
                       "roadmap_id": t.get("roadmap_id") or t.get("roadmapId")}
            result["suggestions"].append(actions.add_suggestion(
                db, kind, payload, confidence=conf,
                reason=f"Only {int(conf * 100)}% sure this is a {intent}",
                source_text=source_text,
            ))
            continue
        if intent == "idea":
            notes_mod.inbox_add(db, text, on=today)
            result["ideas_inboxed"].append(text)
            continue
        due_raw = t.get("due") or str(today)
        todo = models.Todo(
            text=text,
            priority=t.get("priority", 5),
            category=t.get("category", "personal"),
            due=str(due_raw),
            due_date=_due_date_from(due_raw),
            source="ai",
            roadmap_id=t.get("roadmap_id") or t.get("roadmapId"),
        )
        db.add(todo)
        change_log.record_create(db, todo, f"Added task: {text[:80]}")
        result["todos_added"].append(text)

    # ── Ideas (explicit list, e.g. from an accepted suggestion) ──
    for idea in parsed.get("ideas") or []:
        text = (idea.get("text") if isinstance(idea, dict) else str(idea)) or ""
        text = text.strip()
        if text:
            notes_mod.inbox_add(db, text, on=today)
            result["ideas_inboxed"].append(text)

    # ── Todo completions ──────────────────────────────────────
    def _complete(todo):
        if todo.done:
            return
        before = change_log.snapshot(todo)
        todo.done = True
        todo.completed_at = today
        change_log.record_update(db, todo, before, f"Completed task: {todo.text[:80]}")
        result["todos_completed"].append({"id": todo.id, "text": todo.text})

    for todo_id in parsed.get("todos_complete_ids") or []:
        todo = db.query(models.Todo).filter(models.Todo.id == int(todo_id)).first()
        if todo:
            _complete(todo)

    # Free-text fragments: one clear match completes; ambiguity becomes suggestions.
    result["todos_complete_unmatched"] = []
    for frag in parsed.get("todos_complete") or []:
        frag = (frag or "").strip()
        if not frag:
            continue
        todo, candidates = resolve_todo_completion(db, frag)
        if todo is not None:
            _complete(todo)
        elif candidates:
            for score, cand in candidates:
                result["suggestions"].append(actions.add_suggestion(
                    db, "todo_complete",
                    {"todo_id": cand.id, "text": cand.text, "fragment": frag, "score": score},
                    confidence=score,
                    reason=f"'{frag}' could mean this task — tap to complete it",
                    source_text=source_text,
                ))
        else:
            result["todos_complete_unmatched"].append(frag)

    # ── Revenue updates ───────────────────────────────────────
    for ru in parsed.get("revenue_updates") or []:
        amount = ru.get("amount")
        if not amount or float(amount) <= 0:
            continue
        source = ru.get("source") or ""
        client_name = ru.get("client") or ""
        client_id = None
        if client_name:
            matched = db.query(Client).filter(
                func.lower(Client.name).contains(client_name.lower())
            ).first()
            if matched:
                client_id = matched.id
        currency = _normalise_currency(ru.get("currency"))
        rev = Revenue(
            amount=float(amount),
            currency=currency,
            source=source,
            client_id=client_id,
            date=today,
            notes="auto-logged via brain dump",
        )
        db.add(rev)
        change_log.record_create(db, rev, f"Revenue {currency} {float(amount):g} from {source or 'unknown'}")
        result["revenue_logged"].append({"amount": float(amount), "currency": currency, "source": source})

    # ── DailyLog entry ────────────────────────────────────────
    if parsed.get("log_entry"):
        log = models.DailyLog(
            date=today,
            entry=parsed["log_entry"],
            weekly_target_id=None,
            impact_score=0,
        )
        db.add(log)
        change_log.record_create(db, log, f"Log: {str(parsed['log_entry'])[:80]}")
        result["log_entry_created"] = True

    # ── Health updates (daily log fields) ────────────────────
    if parsed.get("health_updates"):
        hu = parsed["health_updates"]
        health_row = db.query(DailyHealth).filter(DailyHealth.date == today).first()
        created = False
        if health_row is None:
            health_row = DailyHealth(date=today)
            db.add(health_row)
            db.flush()
            created = True
        before = change_log.snapshot(health_row)
        daily_fields = ["sleep_hours", "mobility_done", "session_done"]
        changed = False
        for field in daily_fields:
            if hu.get(field) is not None:
                setattr(health_row, field, hu[field])
                changed = True
        if created:
            change_log.record_create(db, health_row, f"Health log for {today}")
        elif changed:
            change_log.record_update(db, health_row, before, f"Health log for {today} updated")
        result["health_updated"] = changed

    # ── Lift logs from brain dump ─────────────────────────────
    if parsed.get("lift_logs"):
        logs_created = []
        for entry in parsed["lift_logs"]:
            name = entry.get("lift_name") or ""
            weight = entry.get("weight_kg")
            reps = entry.get("reps")
            if not name or weight is None or reps is None:
                continue
            lift = db.query(Lift).filter(Lift.name.ilike(name)).first()
            if not lift:
                lift = Lift(name=name, sort_order=0, is_active=True)
                db.add(lift)
                change_log.record_create(db, lift, f"New lift '{name}'")
            log = LiftLog(
                lift_id=lift.id, lift_name=lift.name,
                date=today, weight_kg=float(weight), reps=int(reps),
            )
            db.add(log)
            change_log.record_create(db, log, f"{lift.name} {float(weight):g}kg x {int(reps)}")
            logs_created.append({"lift": lift.name, "weight_kg": float(weight), "reps": int(reps)})
        result["lift_logs_created"] = logs_created

    # Trading is no longer tracked by the parser; any trade_logs key is ignored.

    return result


# ── AI FAILURE HANDLING ──────────────────────────────────────
def _ai_http_error(exc: Exception) -> HTTPException:
    """Map an AI-layer failure to a clean HTTP error instead of a 500 traceback."""
    if isinstance(exc, llm.AIUnavailable):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, ParseFailed):
        return HTTPException(status_code=502, detail=f"Parser failed: {exc}. Nothing was written.")
    return HTTPException(status_code=502, detail=f"AI request failed: {type(exc).__name__}: {exc}")


def _run_parse(db: Session, text: str) -> dict:
    """Call the parser with the standard context. Raises AIUnavailable / ParseFailed /
    SDK errors to the caller — callers translate via ``_ai_http_error``."""
    roadmap_ids = [t.task_id for t in db.query(RoadmapTask).all()]
    context = {"roadmap_ids": roadmap_ids, "today": str(date.today())}
    return get_parse_response(text, context)


# ── PARSE (Brain Dump) ───────────────────────────────────────
@app.post("/parse")
def parse_brain_dump(payload: schemas.ParseRequest, db: Session = Depends(get_db)):
    try:
        parsed = _run_parse(db, payload.text)
    except Exception as exc:  # noqa: BLE001
        raise _ai_http_error(exc)
    updates = apply_parse_updates(db, parsed, date.today())

    # Backward-compatible response: keep existing top-level fields, add "updates" object
    response: dict = {
        "summary":             parsed.get("summary") or "Brain dump processed.",
        "advisory":            parsed.get("advisory") or "Character Before Status. Maintain discipline.",
        "kpi_updates_applied": updates["kpi_updates_applied"],
        "revenue_logged":      updates["revenue_logged"],
        "updates":             updates,
        "status":              "ok",
    }
    # Surface gate-locked warning prominently so the frontend can render it
    if updates.get("gate_locked_warning"):
        response["gate_locked_warning"] = updates["gate_locked_warning"]
        response["status"] = "partial"
    return response


# ── UNIFIED INPUT (routes to parse or chat) ──────────────────
_QUERY_STARTERS = ("what","when","why","how","who","where","is","are","can","could","should","will","tell","show","give","explain","summarise","summarize","help","do you","did i","have i","am i")

@app.post("/input")
def unified_input(request: schemas.UnifiedInputRequest, db: Session = Depends(get_db)):
    """
    Single entry point for Dashboard unified command box.
    Routes to /parse (log) or /chat (query) and returns
    {type: "log"|"query", ...fields}.
    """
    global _kpi_state
    text = request.text.strip()
    lo = text.lower()
    is_query = text.endswith("?") or any(lo.startswith(w) for w in _QUERY_STARTERS)

    if is_query:
        # ── CHAT path ──────────────────────────────────────────
        session = request.session_id
        history = db.query(models.ChatMessage).filter(
            models.ChatMessage.session_id == session
        ).order_by(models.ChatMessage.created_at).all()
        messages = [{"role": m.role, "content": m.content} for m in history]
        messages.append({"role": "user", "content": text})
        db.add(models.ChatMessage(session_id=session, role="user", content=text))
        db.commit()

        context_parts: list[str] = []
        today_logs = db.query(models.DailyLog).filter(
            models.DailyLog.date == date.today()
        ).order_by(models.DailyLog.id.asc()).all()
        if today_logs:
            log_lines = "\n".join(f"- {l.entry}" for l in today_logs if l.entry)
            if log_lines:
                context_parts.append(f"[Today's activity log]\n{log_lines}")
        if request.context:
            context_parts.append(f"[Live system context]\n{json.dumps(request.context)}")
        if context_parts:
            messages[-1]["content"] += "\n\n" + "\n\n".join(context_parts)

        try:
            reply = get_chat_response_with_memory(messages, db=db, context_type="chat")
        except Exception as exc:  # noqa: BLE001
            raise _ai_http_error(exc)
        db.add(models.ChatMessage(session_id=session, role="assistant", content=reply))
        db.commit()
        return {"type": "query", "reply": reply}
    else:
        # ── PARSE path ─────────────────────────────────────────
        try:
            parsed = _run_parse(db, text)
        except Exception as exc:  # noqa: BLE001
            raise _ai_http_error(exc)
        updates = apply_parse_updates(db, parsed, date.today())

        resp: dict = {
            "type":     "log",
            "summary":  parsed.get("summary") or "Entry logged.",
            "advisory": parsed.get("advisory") or "Character Before Status.",
            "updates":  updates,
            "status":   "ok",
        }
        if updates.get("gate_locked_warning"):
            resp["gate_locked_warning"] = updates["gate_locked_warning"]
            resp["status"] = "partial"
        return resp


# ── ORCHESTRATOR (Phase 6) ───────────────────────────────────
# Single model-driven entry point. Decides log-vs-query itself, streams reasoning +
# tool calls over SSE, and reuses /parse, /input, /chat logic internally. The legacy
# /input keyword router above is kept alive for backward compatibility.
@app.post("/orchestrator")
def orchestrator_endpoint(request: schemas.UnifiedInputRequest, db: Session = Depends(get_db)):
    import orchestrator
    return StreamingResponse(
        orchestrator.orchestrator_stream(request, db),
        media_type="text/event-stream",
    )


@app.get("/orchestrator/alerts")
def orchestrator_alerts(db: Session = Depends(get_db)):
    """Off-track alerts for dashboard load. Plain JSON array — empty when clear."""
    import orchestrator
    return orchestrator.run_off_track(db)


# ── PROACTIVE BRIEF ──────────────────────────────────────────
@app.post("/proactive-brief")
def proactive_brief(payload: schemas.ProactiveBriefRequest):
    try:
        brief = get_proactive_brief(payload.context)
    except Exception as exc:  # noqa: BLE001
        raise _ai_http_error(exc)
    return {"brief": brief}


# ── CHAT ─────────────────────────────────────────────────────
@app.post("/chat")
def chat_endpoint(request: schemas.ChatRequest, db: Session = Depends(get_db)):
    session = request.session_id

    history = db.query(models.ChatMessage).filter(
        models.ChatMessage.session_id == session
    ).order_by(models.ChatMessage.created_at).all()

    messages = [{"role": m.role, "content": m.content} for m in history]
    messages.append({"role": "user", "content": request.message})

    db.add(models.ChatMessage(session_id=session, role="user", content=request.message))
    db.commit()

    # Build context suffix: today's log entries + optional client-supplied context
    context_parts: list[str] = []

    today_logs = db.query(models.DailyLog).filter(
        models.DailyLog.date == date.today()
    ).order_by(models.DailyLog.id.asc()).all()
    if today_logs:
        log_lines = "\n".join(f"- {l.entry}" for l in today_logs if l.entry)
        if log_lines:
            context_parts.append(f"[Today's activity log]\n{log_lines}")

    if request.context:
        context_parts.append(f"[Live system context]\n{json.dumps(request.context)}")

    if context_parts:
        messages[-1]["content"] += "\n\n" + "\n\n".join(context_parts)

    try:
        bot_reply = get_chat_response_with_memory(messages, db=db, context_type="chat")
    except Exception as exc:  # noqa: BLE001
        # The user turn is already saved; surface a clean error instead of a 500.
        raise _ai_http_error(exc)

    db.add(models.ChatMessage(session_id=session, role="assistant", content=bot_reply))
    db.commit()

    return {"reply": bot_reply}


# ── VOICE ────────────────────────────────────────────────────
@app.post("/voice/transcribe")
def voice_transcribe(audio: UploadFile = File(...)):
    # Plain def (like /voice/speak): FastAPI runs it in the threadpool, so the
    # blocking Whisper SDK call can't stall the event loop.
    data = audio.file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty audio upload")
    try:
        text = transcribe_audio(data, filename=audio.filename or "audio.webm")
    except llm.AIUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Transcription failed: {e}")
    if not text:
        raise HTTPException(status_code=422, detail="No speech detected")
    return {"text": text}


@app.post("/voice/speak")
def voice_speak(request: schemas.SpeakRequest):
    text = request.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Text required")
    try:
        audio_bytes = synthesize_speech(text)
    except llm.AIUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Speech synthesis failed: {e}")
    return Response(content=audio_bytes, media_type="audio/mpeg")


# ── BIBLE ────────────────────────────────────────────────────
@app.get("/bible")
def get_bible(db: Session = Depends(get_db)):
    entries = db.query(BibleEntry).order_by(BibleEntry.date.asc()).all()
    return [{"id": e.id, "ref": e.ref, "date": str(e.date), "done": e.done, "pushed": e.pushed} for e in entries]


@app.post("/bible")
def add_bible_entry(payload: schemas.BibleEntryCreate, db: Session = Depends(get_db)):
    entry = BibleEntry(ref=payload.ref, date=payload.date, done=False, pushed=0)
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return {"id": entry.id, "ref": entry.ref, "date": str(entry.date)}


@app.patch("/bible/{entry_id}/toggle")
def toggle_bible_entry(entry_id: int, db: Session = Depends(get_db)):
    entry = db.query(BibleEntry).filter(BibleEntry.id == entry_id).first()
    if not entry:
        raise HTTPException(404, "Entry not found")
    entry.done = not entry.done
    db.commit()
    return {"id": entry.id, "done": entry.done}


@app.get("/bible/streak")
def bible_streak_endpoint(db: Session = Depends(get_db)):
    records = db.query(BibleEntry).filter(
        BibleEntry.done == True
    ).order_by(BibleEntry.date.desc()).all()
    streak = 0
    check = date.today()
    for r in records:
        if r.date == check:
            streak += 1
            check = check - timedelta(days=1)
        else:
            break
    return {"streak": streak}


# ── BIBLE LOG ────────────────────────────────────────────────

def _serialize_bible_log(e: DailyBibleLog) -> dict:
    return {"id": e.id, "date": str(e.date), "book": e.book, "chapter": e.chapter, "notes": e.notes}


@app.post("/bible-log", status_code=201)
def create_bible_log(payload: schemas.DailyBibleLogCreate, db: Session = Depends(get_db)):
    entry = DailyBibleLog(
        date=payload.entry_date or date.today(),
        book=payload.book.strip(),
        chapter=payload.chapter,
        notes=payload.notes,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return _serialize_bible_log(entry)


@app.get("/bible-log/today")
def get_bible_log_today(db: Session = Depends(get_db)):
    today = date.today()
    entries = db.query(DailyBibleLog).filter(DailyBibleLog.date == today).order_by(DailyBibleLog.created_at.asc()).all()
    return [_serialize_bible_log(e) for e in entries]


@app.get("/bible-log/recent")
def get_bible_log_recent(days: int = 30, db: Session = Depends(get_db)):
    since = date.today() - timedelta(days=days - 1)
    entries = db.query(DailyBibleLog).filter(DailyBibleLog.date >= since).order_by(DailyBibleLog.date.desc(), DailyBibleLog.created_at.asc()).all()
    grouped: dict = {}
    for e in entries:
        key = str(e.date)
        grouped.setdefault(key, []).append(_serialize_bible_log(e))
    return [{"date": k, "entries": v} for k, v in grouped.items()]


@app.delete("/bible-log/{entry_id}", status_code=204)
def delete_bible_log(entry_id: int, db: Session = Depends(get_db)):
    entry = db.query(DailyBibleLog).filter(DailyBibleLog.id == entry_id).first()
    if not entry:
        raise HTTPException(404, "Entry not found")
    db.delete(entry)
    db.commit()


# ── BOOKS ─────────────────────────────────────────────────────

def _serialize_book(b: Book) -> dict:
    return {
        "id": b.id, "title": b.title, "author": b.author, "status": b.status,
        "page": b.page, "total_pages": b.total_pages,
        "position": b.position, "is_currently_reading": b.is_currently_reading,
    }


@app.get("/books")
def get_books(db: Session = Depends(get_db)):
    books = db.query(Book).order_by(Book.position.asc(), Book.created_at.asc()).all()
    if not books:
        _seed_books_if_empty(db)
        books = db.query(Book).order_by(Book.position.asc(), Book.created_at.asc()).all()
    return [_serialize_book(b) for b in books]


@app.post("/books")
def add_book(payload: schemas.BookCreate, db: Session = Depends(get_db)):
    max_pos = db.query(func.max(Book.position)).scalar() or 0
    data = payload.model_dump()
    data["position"] = max_pos + 1
    book = Book(**data)
    db.add(book)
    db.commit()
    db.refresh(book)
    return _serialize_book(book)


@app.patch("/books/{book_id}")
def update_book(book_id: int, payload: schemas.BookUpdate, db: Session = Depends(get_db)):
    book = db.query(Book).filter(Book.id == book_id).first()
    if not book:
        raise HTTPException(404, "Book not found")
    for field, val in payload.model_dump(exclude_none=True).items():
        setattr(book, field, val)
    db.commit()
    return _serialize_book(book)


@app.patch("/books/{book_id}/currently-reading")
def toggle_currently_reading(book_id: int, db: Session = Depends(get_db)):
    book = db.query(Book).filter(Book.id == book_id).first()
    if not book:
        raise HTTPException(404, "Book not found")
    book.is_currently_reading = not book.is_currently_reading
    db.commit()
    return _serialize_book(book)


@app.post("/books/reorder")
def reorder_books(order: List[int], db: Session = Depends(get_db)):
    """Accepts a list of book IDs in the desired display order."""
    for pos, book_id in enumerate(order):
        book = db.query(Book).filter(Book.id == book_id).first()
        if book:
            book.position = pos
    db.commit()
    return {"reordered": len(order)}


@app.delete("/books/{book_id}")
def delete_book(book_id: int, db: Session = Depends(get_db)):
    book = db.query(Book).filter(Book.id == book_id).first()
    if not book:
        raise HTTPException(404, "Book not found")
    db.delete(book)
    db.commit()
    return {"deleted": book_id}


# ── WAR ROOM — DOCUMENTS ─────────────────────────────────────

CHUNK_SIZE = 800  # characters per chunk

def _chunk_text(text: str, size: int = CHUNK_SIZE) -> list[str]:
    """Split text into overlapping chunks for retrieval."""
    words = text.split()
    chunks, buf = [], []
    char_count = 0
    for word in words:
        buf.append(word)
        char_count += len(word) + 1
        if char_count >= size:
            chunks.append(" ".join(buf))
            buf = buf[-(size // 10):]   # 10% overlap
            char_count = sum(len(w) + 1 for w in buf)
    if buf:
        chunks.append(" ".join(buf))
    return chunks or [text]


def _rebuild_chunks(doc: Document, db: Session):
    for old in list(doc.chunks):
        db.delete(old)
    for i, chunk_text in enumerate(_chunk_text(doc.content)):
        db.add(DocumentChunk(document_id=doc.id, content=chunk_text, chunk_index=i))


@app.get("/documents")
def list_documents(db: Session = Depends(get_db)):
    docs = db.query(Document).order_by(Document.created_at.desc()).all()
    return [{"id": d.id, "title": d.title, "source_type": d.source_type,
             "created_at": str(d.created_at), "chunk_count": len(d.chunks)} for d in docs]


@app.post("/documents", status_code=201)
def create_document(payload: schemas.DocumentCreate, db: Session = Depends(get_db)):
    doc = Document(title=payload.title, content=payload.content, source_type=payload.source_type)
    db.add(doc)
    db.flush()
    _rebuild_chunks(doc, db)
    db.commit()
    db.refresh(doc)
    return {"id": doc.id, "title": doc.title, "chunk_count": len(doc.chunks)}


@app.get("/documents/{doc_id}")
def get_document(doc_id: int, db: Session = Depends(get_db)):
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        raise HTTPException(404, "Document not found")
    return {"id": doc.id, "title": doc.title, "content": doc.content,
            "source_type": doc.source_type, "created_at": str(doc.created_at),
            "chunks": [{"index": c.chunk_index, "content": c.content} for c in doc.chunks]}


@app.patch("/documents/{doc_id}")
def update_document(doc_id: int, payload: schemas.DocumentUpdate, db: Session = Depends(get_db)):
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        raise HTTPException(404, "Document not found")
    if payload.title is not None:
        doc.title = payload.title
    if payload.content is not None:
        doc.content = payload.content
        _rebuild_chunks(doc, db)
    doc.updated_at = datetime.utcnow()
    db.commit()
    return {"id": doc.id, "title": doc.title, "chunk_count": len(doc.chunks)}


@app.delete("/documents/{doc_id}")
def delete_document(doc_id: int, db: Session = Depends(get_db)):
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        raise HTTPException(404, "Document not found")
    db.delete(doc)
    db.commit()
    return {"deleted": doc_id}


# ── WAR ROOM — NON-NEGOTIABLES ────────────────────────────────

@app.get("/non-negotiables")
def list_non_negotiables(db: Session = Depends(get_db)):
    rows = db.query(NonNegotiable).order_by(NonNegotiable.sort_order, NonNegotiable.id).all()
    return [{"id": r.id, "key": r.key, "label": r.label,
             "is_active": r.is_active, "sort_order": r.sort_order} for r in rows]


@app.post("/non-negotiables", status_code=201)
def create_non_negotiable(payload: schemas.NonNegotiableCreate, db: Session = Depends(get_db)):
    existing = db.query(NonNegotiable).filter(NonNegotiable.key == payload.key).first()
    if existing:
        raise HTTPException(400, f"Key '{payload.key}' already exists")
    nn = NonNegotiable(**payload.model_dump())
    db.add(nn)
    db.commit()
    db.refresh(nn)
    return {"id": nn.id, "key": nn.key, "label": nn.label}


@app.patch("/non-negotiables/{nn_id}")
def update_non_negotiable(nn_id: int, payload: schemas.NonNegotiablePatch, db: Session = Depends(get_db)):
    nn = db.query(NonNegotiable).filter(NonNegotiable.id == nn_id).first()
    if not nn:
        raise HTTPException(404, "Non-negotiable not found")
    for field, val in payload.model_dump(exclude_none=True).items():
        setattr(nn, field, val)
    db.commit()
    return {"id": nn.id, "key": nn.key, "label": nn.label, "is_active": nn.is_active}


@app.delete("/non-negotiables/{nn_id}")
def delete_non_negotiable(nn_id: int, db: Session = Depends(get_db)):
    nn = db.query(NonNegotiable).filter(NonNegotiable.id == nn_id).first()
    if not nn:
        raise HTTPException(404, "Non-negotiable not found")
    db.delete(nn)
    db.commit()
    return {"deleted": nn_id}


# ── WAR ROOM — READING PLAN ───────────────────────────────────

def _serialize_reading_plan(p: ReadingPlan) -> dict:
    from datetime import date as _date
    today = _date.today()
    days_remaining = None
    if p.target_completion_date:
        days_remaining = (p.target_completion_date - today).days
    return {
        "id": p.id,
        "name": p.name,
        "is_active": p.is_active,
        "status": "active" if p.is_active else "archived",
        "current_book": p.current_book or "",
        "current_chapter": p.current_chapter or 1,
        "daily_target_chapters": p.daily_target_chapters or 1,
        "start_date": str(p.start_date) if p.start_date else None,
        "target_completion_date": str(p.target_completion_date) if p.target_completion_date else None,
        "days_remaining": days_remaining,
        "notes": p.notes or "",
        "entry_count": len(p.entries),
        "done_count": sum(1 for e in p.entries if e.done),
    }


@app.get("/reading-plans")
def list_reading_plans(db: Session = Depends(get_db)):
    plans = db.query(ReadingPlan).order_by(ReadingPlan.id.asc()).all()
    return [_serialize_reading_plan(p) for p in plans]


@app.post("/reading-plans", status_code=201)
def create_reading_plan(payload: schemas.ReadingPlanCreate, db: Session = Depends(get_db)):
    plan = ReadingPlan(**payload.model_dump())
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return _serialize_reading_plan(plan)


@app.patch("/reading-plans/{plan_id}")
def update_reading_plan(plan_id: int, payload: schemas.ReadingPlanUpdate, db: Session = Depends(get_db)):
    plan = db.query(ReadingPlan).filter(ReadingPlan.id == plan_id).first()
    if not plan:
        raise HTTPException(404, "Reading plan not found")
    for field, val in payload.model_dump(exclude_none=True).items():
        setattr(plan, field, val)
    db.commit()
    return _serialize_reading_plan(plan)


@app.delete("/reading-plans/{plan_id}")
def archive_reading_plan(plan_id: int, db: Session = Depends(get_db)):
    plan = db.query(ReadingPlan).filter(ReadingPlan.id == plan_id).first()
    if not plan:
        raise HTTPException(404, "Reading plan not found")
    plan.is_active = False
    db.commit()
    return {"archived": plan_id}


@app.post("/reading-plans/{plan_id}/mark-today")
def mark_today_reading(plan_id: int, db: Session = Depends(get_db)):
    plan = db.query(ReadingPlan).filter(ReadingPlan.id == plan_id).first()
    if not plan:
        raise HTTPException(404, "Reading plan not found")
    plan.current_chapter = (plan.current_chapter or 1) + (plan.daily_target_chapters or 1)
    db.commit()
    return _serialize_reading_plan(plan)


@app.get("/reading-plans/{plan_id}/entries")
def get_reading_plan_entries(plan_id: int, db: Session = Depends(get_db)):
    plan = db.query(ReadingPlan).filter(ReadingPlan.id == plan_id).first()
    if not plan:
        raise HTTPException(404, "Reading plan not found")
    return {"id": plan.id, "name": plan.name, "is_active": plan.is_active,
            "entries": [{"id": e.id, "ref": e.ref, "day_number": e.day_number,
                         "done": e.done, "pushed": e.pushed} for e in plan.entries]}


@app.post("/reading-plans/{plan_id}/entries", status_code=201)
def add_reading_plan_entry(plan_id: int, payload: schemas.ReadingPlanEntryCreate, db: Session = Depends(get_db)):
    plan = db.query(ReadingPlan).filter(ReadingPlan.id == plan_id).first()
    if not plan:
        raise HTTPException(404, "Reading plan not found")
    entry = ReadingPlanEntry(plan_id=plan_id, ref=payload.ref, day_number=payload.day_number)
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return {"id": entry.id, "ref": entry.ref, "day_number": entry.day_number}


@app.patch("/reading-plans/entries/{entry_id}")
def update_reading_plan_entry(entry_id: int, payload: schemas.ReadingPlanEntryPatch, db: Session = Depends(get_db)):
    entry = db.query(ReadingPlanEntry).filter(ReadingPlanEntry.id == entry_id).first()
    if not entry:
        raise HTTPException(404, "Entry not found")
    if payload.done is not None:
        entry.done = payload.done
    if payload.pushed is not None:
        entry.pushed = payload.pushed
    db.commit()
    return {"id": entry.id, "done": entry.done, "pushed": entry.pushed}


@app.patch("/reading-plans/entries/{entry_id}/toggle")
def toggle_reading_plan_entry(entry_id: int, db: Session = Depends(get_db)):
    entry = db.query(ReadingPlanEntry).filter(ReadingPlanEntry.id == entry_id).first()
    if not entry:
        raise HTTPException(404, "Entry not found")
    entry.done = not entry.done
    db.commit()
    return {"id": entry.id, "done": entry.done}


# ── WAR ROOM — CONTEXT ────────────────────────────────────────

@app.get("/context/core")
def get_core_context(db: Session = Depends(get_db)):
    """Returns all active doctrine documents as a single context block."""
    docs = db.query(Document).order_by(Document.created_at.asc()).all()
    nns = db.query(NonNegotiable).filter(NonNegotiable.is_active == True).order_by(NonNegotiable.sort_order).all()
    targets = db.query(AnnualTarget).filter(AnnualTarget.is_active == True).order_by(AnnualTarget.sort_order).all()
    return {
        "documents": [{"id": d.id, "title": d.title, "content": d.content} for d in docs],
        "non_negotiables": [{"key": n.key, "label": n.label} for n in nns],
        "annual_targets": [
            {"name": t.name, "current_value": t.current_value, "target_value": t.target_value,
             "unit": t.unit, "display_value": t.display_value, "is_complete": t.is_complete}
            for t in targets
        ],
    }


@app.get("/context/search")
def search_context(q: str, db: Session = Depends(get_db)):
    """Keyword search across document chunks."""
    if not q or len(q) < 2:
        raise HTTPException(400, "Query too short")
    q_lower = q.lower()
    chunks = db.query(DocumentChunk).all()
    results = []
    for chunk in chunks:
        if q_lower in chunk.content.lower():
            results.append({
                "document_id": chunk.document_id,
                "chunk_index": chunk.chunk_index,
                "content": chunk.content,
            })
    return {"query": q, "results": results[:20]}


# ── VAULT (COLD ARCHIVE) ──────────────────────────────────────

@app.post("/vault/sync")
def vault_sync_endpoint(db: Session = Depends(get_db)):
    """Manually trigger a vault clone/pull and re-index."""
    from vault_sync import sync_vault
    return sync_vault(db)


@app.get("/vault/status")
def vault_status(db: Session = Depends(get_db)):
    """Return sync status: last synced time, chunk count, file count.

    Never include VAULT_REPO_URL: in production it embeds a GitHub token.
    """
    chunk_count = db.query(models.ColdArchiveChunk).count()
    file_count = (
        db.query(models.ColdArchiveChunk.file_path)
        .distinct()
        .count()
    )
    latest = (
        db.query(models.ColdArchiveChunk)
        .order_by(models.ColdArchiveChunk.last_synced.desc())
        .first()
    )
    last_synced = str(latest.last_synced) if latest and latest.last_synced else None
    return {
        "last_synced": last_synced,
        "chunk_count": chunk_count,
        "file_count": file_count,
    }


@app.get("/vault/search")
def vault_search(q: str, db: Session = Depends(get_db)):
    """Keyword search across cold archive chunks. Returns top 8."""
    from vault_sync import search_vault
    if not q or len(q) < 2:
        raise HTTPException(400, "Query too short")
    return {"query": q, "results": search_vault(q, db, limit=8)}


# ── MARKET INTELLIGENCE (RESEARCH) ────────────────────────────

_PIPELINE_STAGES = {"discovered", "researching", "validating", "building", "rejected"}


@app.get("/research/projects")
def research_projects(db: Session = Depends(get_db)):
    """Research projects newest-first, with finding counts."""
    projects = db.query(models.ResearchProject).order_by(
        models.ResearchProject.created_at.desc()
    ).all()
    return [{
        "id": p.id,
        "title": p.title,
        "objective": p.objective,
        "status": p.status,
        "finding_count": len(p.findings),
        "created_at": str(p.created_at) if p.created_at else None,
        "completed_at": str(p.completed_at) if p.completed_at else None,
    } for p in projects]


@app.get("/research/projects/{project_id}")
def research_project_detail(project_id: int, db: Session = Depends(get_db)):
    """One project with its full findings (JSON fields parsed)."""
    from market_intel.agent import _serialize_finding
    project = db.query(models.ResearchProject).filter(
        models.ResearchProject.id == project_id
    ).first()
    if not project:
        raise HTTPException(404, "Research project not found")
    return {
        "id": project.id,
        "title": project.title,
        "objective": project.objective,
        "status": project.status,
        "error": project.error,
        "created_at": str(project.created_at) if project.created_at else None,
        "completed_at": str(project.completed_at) if project.completed_at else None,
        "findings": [_serialize_finding(f) for f in project.findings],
    }


@app.get("/research/pipeline")
def research_pipeline(db: Session = Depends(get_db)):
    """Opportunity pipeline entries joined to their findings."""
    from market_intel.agent import _serialize_finding
    entries = db.query(models.OpportunityPipeline).order_by(
        models.OpportunityPipeline.promoted_at.desc()
    ).all()
    return [{
        "id": e.id,
        "stage": e.stage,
        "notes": e.notes,
        "promoted_at": str(e.promoted_at) if e.promoted_at else None,
        "updated_at": str(e.updated_at) if e.updated_at else None,
        "finding": _serialize_finding(e.finding) if e.finding else None,
    } for e in entries]


@app.post("/research/promote")
def research_promote(payload: schemas.PromoteFindingRequest, db: Session = Depends(get_db)):
    """Explicitly promote a surfaced finding into the pipeline — the 'save this
    opportunity' action. The agent never does this on its own."""
    import orchestrator_tools
    result, _summary = orchestrator_tools.promote_research_opportunity(
        db, finding_id=payload.finding_id, notes=payload.notes
    )
    if result.get("error"):
        raise HTTPException(404, result["error"])
    return result


@app.patch("/research/pipeline/{entry_id}")
def research_pipeline_update(entry_id: int, payload: schemas.PipelineUpdateRequest,
                             db: Session = Depends(get_db)):
    """Move a pipeline entry between stages or update its notes."""
    entry = db.query(models.OpportunityPipeline).filter(
        models.OpportunityPipeline.id == entry_id
    ).first()
    if not entry:
        raise HTTPException(404, "Pipeline entry not found")
    if payload.stage is not None:
        if payload.stage not in _PIPELINE_STAGES:
            raise HTTPException(400, f"Invalid stage. Must be one of: {sorted(_PIPELINE_STAGES)}")
        entry.stage = payload.stage
    if payload.notes is not None:
        entry.notes = payload.notes
    db.commit()
    return {"id": entry.id, "stage": entry.stage, "notes": entry.notes}


@app.get("/research/memory")
def research_memory(db: Session = Depends(get_db)):
    """The agent's lesson notes — one lesson per note."""
    notes = db.query(models.ResearchMemoryNote).order_by(
        models.ResearchMemoryNote.updated_at.desc()
    ).all()
    return [{
        "slug": n.slug,
        "summary": n.summary,
        "content": n.content,
        "times_reinforced": n.times_reinforced,
        "updated_at": str(n.updated_at) if n.updated_at else None,
    } for n in notes]


# ── SOCIAL SCORE ──────────────────────────────────────────────
@app.get("/social-score")
def get_social_score(db: Session = Depends(get_db)):
    latest = db.query(SocialScore).order_by(SocialScore.date.desc()).first()
    return {"value": latest.value if latest else 50, "date": str(latest.date) if latest else None}


@app.post("/social-score")
def update_social_score(payload: schemas.SocialScoreUpdate, db: Session = Depends(get_db)):
    db.add(SocialScore(value=payload.value, date=date.today()))
    db.commit()
    return {"value": payload.value}


# ── CLIENTS ───────────────────────────────────────────────────
@app.get("/clients")
def get_clients(db: Session = Depends(get_db)):
    clients = db.query(Client).order_by(Client.created_at.desc()).all()
    return [{"id": c.id, "name": c.name, "company": c.company, "status": c.status,
             "value": c.value, "service": c.service, "notes": c.notes,
             "created_at": str(c.created_at)} for c in clients]


@app.post("/clients")
def add_client(payload: schemas.ClientCreate, db: Session = Depends(get_db)):
    client = Client(**payload.dict())
    db.add(client)
    db.commit()
    db.refresh(client)
    return {"id": client.id, "name": client.name, "status": client.status}


@app.patch("/clients/{client_id}")
def update_client(client_id: int, payload: schemas.ClientUpdate, db: Session = Depends(get_db)):
    client = db.query(Client).filter(Client.id == client_id).first()
    if not client:
        raise HTTPException(404, "Client not found")
    if payload.status is not None:
        client.status = payload.status
    if payload.value is not None:
        client.value = payload.value
    if payload.notes is not None:
        client.notes = payload.notes
    if payload.service is not None:
        client.service = payload.service
    client.updated_at = datetime.utcnow()
    db.commit()
    return {"id": client.id, "status": client.status}


@app.delete("/clients/{client_id}")
def delete_client(client_id: int, db: Session = Depends(get_db)):
    client = db.query(Client).filter(Client.id == client_id).first()
    if not client:
        raise HTTPException(404, "Client not found")
    db.delete(client)
    db.commit()
    return {"deleted": client_id}


# ── REVENUE ───────────────────────────────────────────────────
@app.get("/revenue")
def get_revenue(db: Session = Depends(get_db)):
    records = db.query(Revenue).order_by(Revenue.date.desc()).all()
    by_currency: dict = {}
    for r in records:
        cur = (r.currency or "USD").upper()
        by_currency[cur] = round(by_currency.get(cur, 0.0) + r.amount, 2)
    return {
        "total": sum(r.amount for r in records),          # legacy: raw sum across currencies
        "total_usd": round(sum(_to_usd(r.amount, r.currency) for r in records), 2),
        "by_currency": by_currency,
        "zmw_per_usd": _zmw_per_usd(),
        "records": [{"id": r.id, "amount": r.amount, "currency": (r.currency or "USD").upper(),
                     "source": r.source, "client_id": r.client_id, "date": str(r.date), "notes": r.notes}
                    for r in records]
    }


@app.post("/revenue")
def add_revenue(payload: schemas.RevenueCreate, db: Session = Depends(get_db)):
    data = payload.dict()
    data["currency"] = _normalise_currency(data.get("currency"))
    record = Revenue(**data)
    db.add(record)
    db.commit()
    db.refresh(record)
    return {"id": record.id, "amount": record.amount, "currency": record.currency}


@app.delete("/revenue/{revenue_id}")
def delete_revenue(revenue_id: int, db: Session = Depends(get_db)):
    record = db.query(Revenue).filter(Revenue.id == revenue_id).first()
    if not record:
        raise HTTPException(404, "Revenue record not found")
    db.delete(record)
    db.commit()
    return {"deleted": revenue_id}


# ── TRADING MODULE ────────────────────────────────────────────

# ── Helpers ───────────────────────────────────────────────────

def _serialize_prop_account(a: PropFirmAccount) -> dict:
    days_active = (date.today() - a.start_date).days if a.start_date else 0
    drawdown_pct = round((a.peak_balance - a.current_balance) / a.peak_balance * 100, 2) if a.peak_balance else 0.0
    profit_target_usd = a.starting_balance * (a.profit_target_pct / 100)
    profit_made = a.current_balance - a.starting_balance
    target_progress_pct = round((profit_made / profit_target_usd) * 100, 1) if profit_target_usd else 0.0
    gain_pct = round((a.current_balance - a.starting_balance) / a.starting_balance * 100, 2) if a.starting_balance else 0.0
    return {
        "id": a.id, "name": a.name, "firm": a.firm,
        "account_size_usd": a.account_size_usd, "challenge_type": a.challenge_type,
        "starting_balance": a.starting_balance, "current_balance": a.current_balance,
        "peak_balance": a.peak_balance,
        "profit_target_pct": a.profit_target_pct, "max_drawdown_pct": a.max_drawdown_pct,
        "daily_drawdown_pct": a.daily_drawdown_pct,
        "start_date": str(a.start_date), "status": a.status, "notes": a.notes,
        "days_active": days_active,
        "drawdown_from_peak_pct": drawdown_pct,
        "gain_pct": gain_pct,
        "target_progress_pct": target_progress_pct,
    }


def _serialize_backtest(t: BacktestTrade) -> dict:
    return {
        "id": t.id, "date": str(t.date), "time_of_day": t.time_of_day,
        "pair": t.pair, "direction": t.direction, "entry_reason": t.entry_reason,
        "r_multiple": t.r_multiple, "rule_adherence": t.rule_adherence,
        "outcome": t.outcome, "notes": t.notes,
        "created_at": str(t.created_at),
    }


def _serialize_live_trade(t: LiveTrade) -> dict:
    return {
        "id": t.id, "date": str(t.date), "time_of_day": t.time_of_day,
        "pair": t.pair, "direction": t.direction, "entry_reason": t.entry_reason,
        "r_multiple": t.r_multiple, "rule_adherence": t.rule_adherence,
        "outcome": t.outcome, "notes": t.notes,
        "account_id": t.account_id, "risk_pct": t.risk_pct,
        "net_pl_usd": t.net_pl_usd, "rule_broken": t.rule_broken,
        "rule_broken_description": t.rule_broken_description,
        "created_at": str(t.created_at),
    }


def _compute_gate(db: Session) -> dict:
    """Compute gate status fresh from DB. No caching."""
    trades_required = 50
    adherence_required = 90.0
    all_trades = db.query(BacktestTrade).all()
    total = len(all_trades)
    adherent = sum(1 for t in all_trades if t.rule_adherence)
    adherence_pct = round((adherent / total) * 100, 1) if total else 0.0
    cleared = total >= trades_required and adherence_pct >= adherence_required
    return {
        "total_backtests": total,
        "adherence_pct": adherence_pct,
        "trades_required": trades_required,
        "adherence_required": adherence_required,
        "status": "CLEARED" if cleared else "LOCKED",
        "missing": {
            "trades": max(0, trades_required - total),
            "adherence_pct_gap": round(max(0.0, adherence_required - adherence_pct), 1),
        },
    }


def _trade_stats(trades) -> dict:
    total = len(trades)
    if not total:
        return {"total": 0, "win_rate": 0.0, "expectancy_r": 0.0, "total_r": 0.0,
                "by_pair": {}, "adherence_pct": 0.0, "rule_breaks": 0}
    wins = sum(1 for t in trades if t.outcome == "win")
    r_vals = [t.r_multiple for t in trades]
    by_pair: dict = {}
    for t in trades:
        by_pair.setdefault(t.pair, 0)
        by_pair[t.pair] += 1
    adherent = sum(1 for t in trades if t.rule_adherence)
    return {
        "total": total,
        "win_rate": round(wins / total * 100, 1),
        "expectancy_r": round(sum(r_vals) / total, 2),
        "total_r": round(sum(r_vals), 2),
        "by_pair": by_pair,
        "adherence_pct": round(adherent / total * 100, 1),
        "rule_breaks": total - adherent,
    }


# ── Prop Firm Account CRUD ────────────────────────────────────

@app.get("/trading/accounts")
def list_accounts(db: Session = Depends(get_db)):
    accounts = db.query(PropFirmAccount).order_by(PropFirmAccount.created_at.asc()).all()
    return [_serialize_prop_account(a) for a in accounts]


@app.post("/trading/accounts", status_code=201)
def create_account(payload: schemas.PropFirmAccountCreate, db: Session = Depends(get_db)):
    data = payload.model_dump()
    # peak_balance starts equal to current_balance at creation
    data["peak_balance"] = data["current_balance"]
    acct = PropFirmAccount(**data)
    db.add(acct)
    db.commit()
    db.refresh(acct)
    return _serialize_prop_account(acct)


@app.patch("/trading/accounts/{account_id}")
def update_account(account_id: int, payload: schemas.PropFirmAccountPatch, db: Session = Depends(get_db)):
    acct = db.query(PropFirmAccount).filter(PropFirmAccount.id == account_id).first()
    if not acct:
        raise HTTPException(404, "Account not found")
    updates = payload.model_dump(exclude_none=True)
    for field, val in updates.items():
        setattr(acct, field, val)
    # Auto-update peak_balance if current_balance grew
    if acct.current_balance > acct.peak_balance:
        acct.peak_balance = acct.current_balance
    acct.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(acct)
    return _serialize_prop_account(acct)


@app.delete("/trading/accounts/{account_id}")
def delete_account(account_id: int, db: Session = Depends(get_db)):
    """Soft-delete: sets status=withdrawn."""
    acct = db.query(PropFirmAccount).filter(PropFirmAccount.id == account_id).first()
    if not acct:
        raise HTTPException(404, "Account not found")
    acct.status = "withdrawn"
    acct.updated_at = datetime.utcnow()
    db.commit()
    return {"id": account_id, "status": "withdrawn"}


# ── Backtest CRUD ─────────────────────────────────────────────

@app.get("/trading/backtest")
def list_backtests(db: Session = Depends(get_db)):
    trades = db.query(BacktestTrade).order_by(BacktestTrade.date.desc(), BacktestTrade.id.desc()).all()
    return [_serialize_backtest(t) for t in trades]


@app.get("/trading/backtest/recent")
def recent_backtests(n: int = Query(20, ge=1, le=200), db: Session = Depends(get_db)):
    trades = db.query(BacktestTrade).order_by(BacktestTrade.date.desc(), BacktestTrade.id.desc()).limit(n).all()
    return [_serialize_backtest(t) for t in trades]


@app.post("/trading/backtest", status_code=201)
def create_backtest(payload: schemas.BacktestTradeCreate, db: Session = Depends(get_db)):
    trade = BacktestTrade(**payload.model_dump())
    db.add(trade)
    db.commit()
    db.refresh(trade)
    return _serialize_backtest(trade)


@app.patch("/trading/backtest/{trade_id}")
def update_backtest(trade_id: int, payload: schemas.BacktestTradePatch, db: Session = Depends(get_db)):
    trade = db.query(BacktestTrade).filter(BacktestTrade.id == trade_id).first()
    if not trade:
        raise HTTPException(404, "Backtest trade not found")
    for field, val in payload.model_dump(exclude_none=True).items():
        setattr(trade, field, val)
    db.commit()
    db.refresh(trade)
    return _serialize_backtest(trade)


@app.delete("/trading/backtest/{trade_id}")
def delete_backtest(trade_id: int, db: Session = Depends(get_db)):
    trade = db.query(BacktestTrade).filter(BacktestTrade.id == trade_id).first()
    if not trade:
        raise HTTPException(404, "Backtest trade not found")
    db.delete(trade)
    db.commit()
    return {"deleted": trade_id}


# ── Gate ──────────────────────────────────────────────────────

@app.get("/trading/gate")
def get_gate(db: Session = Depends(get_db)):
    return _compute_gate(db)


# ── Live Trades CRUD ──────────────────────────────────────────

@app.get("/trading/live")
def list_live_trades(db: Session = Depends(get_db)):
    trades = db.query(LiveTrade).order_by(LiveTrade.date.desc(), LiveTrade.id.desc()).all()
    return [_serialize_live_trade(t) for t in trades]


@app.get("/trading/live/recent")
def recent_live_trades(n: int = Query(20, ge=1, le=200), db: Session = Depends(get_db)):
    trades = db.query(LiveTrade).order_by(LiveTrade.date.desc(), LiveTrade.id.desc()).limit(n).all()
    return [_serialize_live_trade(t) for t in trades]


@app.post("/trading/live", status_code=201)
def create_live_trade(payload: schemas.LiveTradeCreate, db: Session = Depends(get_db)):
    gate = _compute_gate(db)
    if gate["status"] == "LOCKED":
        raise HTTPException(
            status_code=423,
            detail={"message": "Backtest gate not cleared. Live trading is locked.", "gate": gate},
        )
    data = payload.model_dump()
    if data.get("rule_broken") and not data.get("rule_broken_description"):
        raise HTTPException(400, "rule_broken_description is required when rule_broken=True")
    trade = LiveTrade(**data)
    db.add(trade)
    db.commit()
    db.refresh(trade)
    return _serialize_live_trade(trade)


@app.patch("/trading/live/{trade_id}")
def update_live_trade(trade_id: int, payload: schemas.LiveTradePatch, db: Session = Depends(get_db)):
    trade = db.query(LiveTrade).filter(LiveTrade.id == trade_id).first()
    if not trade:
        raise HTTPException(404, "Live trade not found")
    updates = payload.model_dump(exclude_none=True)
    if updates.get("rule_broken") and not (updates.get("rule_broken_description") or trade.rule_broken_description):
        raise HTTPException(400, "rule_broken_description is required when rule_broken=True")
    for field, val in updates.items():
        setattr(trade, field, val)
    db.commit()
    db.refresh(trade)
    return _serialize_live_trade(trade)


@app.delete("/trading/live/{trade_id}")
def delete_live_trade(trade_id: int, db: Session = Depends(get_db)):
    trade = db.query(LiveTrade).filter(LiveTrade.id == trade_id).first()
    if not trade:
        raise HTTPException(404, "Live trade not found")
    db.delete(trade)
    db.commit()
    return {"deleted": trade_id}


# ── Stats ─────────────────────────────────────────────────────

@app.get("/trading/stats/backtest")
def backtest_stats(db: Session = Depends(get_db)):
    trades = db.query(BacktestTrade).all()
    return _trade_stats(trades)


@app.get("/trading/stats/live")
def live_stats(db: Session = Depends(get_db)):
    trades = db.query(LiveTrade).all()
    stats = _trade_stats(trades)
    stats["total_pl_usd"] = round(sum(t.net_pl_usd or 0 for t in trades), 2)
    return stats


# ── Monthly Scoreboard ────────────────────────────────────────

@app.get("/trading/scoreboard/monthly")
def monthly_scoreboard(month: str = Query(..., regex=r"^\d{4}-\d{2}$"), db: Session = Depends(get_db)):
    year, mo = int(month[:4]), int(month[5:])
    from calendar import monthrange
    _, last_day = monthrange(year, mo)
    start = date(year, mo, 1)
    end = date(year, mo, last_day)
    trades = db.query(LiveTrade).filter(LiveTrade.date >= start, LiveTrade.date <= end).all()
    net_pl = round(sum(t.net_pl_usd or 0 for t in trades), 2)
    return {"month": month, "net_pl_usd": net_pl, "trade_count": len(trades)}


# ── Unified Ten-K Scoreboard ──────────────────────────────────

@app.get("/scoreboard/ten-k")
def ten_k_scoreboard(db: Session = Depends(get_db)):
    target_date = date(2026, 9, 30)
    days_remaining = max(0, (target_date - date.today()).days)

    ai_revenue = _revenue_usd_total(db, since=TEN_K_START_DATE)   # ZMW converted via ZMW_PER_USD

    trading_pl = db.query(func.sum(LiveTrade.net_pl_usd)).filter(
        LiveTrade.date >= TEN_K_START_DATE,
        LiveTrade.net_pl_usd.isnot(None),
    ).scalar() or 0.0

    total = round(float(ai_revenue) + float(trading_pl), 2)

    # Monthly breakdown
    from calendar import monthrange
    breakdown = []
    # Walk months from TEN_K_START_DATE to today
    cur = TEN_K_START_DATE.replace(day=1)
    today_month = date.today().replace(day=1)
    while cur <= today_month:
        yr, mo = cur.year, cur.month
        _, last_day = monthrange(yr, mo)
        m_start, m_end = date(yr, mo, 1), date(yr, mo, last_day)
        label = f"{yr}-{mo:02d}"

        ai_mo = float(sum(
            _to_usd(r.amount, r.currency) for r in
            db.query(Revenue).filter(Revenue.date >= m_start, Revenue.date <= m_end).all()
        ))
        tr_mo = float(db.query(func.sum(LiveTrade.net_pl_usd)).filter(
            LiveTrade.date >= m_start, LiveTrade.date <= m_end,
            LiveTrade.net_pl_usd.isnot(None),
        ).scalar() or 0)
        breakdown.append({"month": label, "ai": round(ai_mo, 2), "trading": round(tr_mo, 2),
                          "total": round(ai_mo + tr_mo, 2)})
        # Advance one month
        if mo == 12:
            cur = date(yr + 1, 1, 1)
        else:
            cur = date(yr, mo + 1, 1)

    return {
        "target_usd": 10000,
        "target_date": str(target_date),
        "days_remaining": days_remaining,
        "total_progress_usd": total,
        "ai_revenue_usd": round(float(ai_revenue), 2),
        "trading_pl_usd": round(float(trading_pl), 2),
        "zmw_per_usd": _zmw_per_usd(),
        "monthly_breakdown": breakdown,
    }


# ── EXISTING WEEKLY TARGET ENDPOINTS (unchanged) ─────────────
@app.post("/targets")
def create_target(target: schemas.WeeklyTargetCreate, db: Session = Depends(get_db)):
    new_target = models.WeeklyTarget(
        title=target.title,
        description=target.description,
        week_start=target.week_start,
        week_end=target.week_end,
        target_value=target.target_value,
        weight=target.weight
    )
    db.add(new_target)
    db.commit()
    db.refresh(new_target)
    return new_target


@app.get("/targets")
def get_targets(db: Session = Depends(get_db)):
    return db.query(models.WeeklyTarget).all()


@app.post("/logs")
def create_log(log: schemas.DailyLogCreate, db: Session = Depends(get_db)):
    # weekly_target_id is optional — omit it for standalone/brain-dump entries
    if log.weekly_target_id is not None:
        target = db.query(models.WeeklyTarget).filter(
            models.WeeklyTarget.id == log.weekly_target_id
        ).first()
        if not target:
            raise HTTPException(status_code=404, detail="Target not found")
    new_log = models.DailyLog(
        date=log.date,
        entry=log.entry,
        weekly_target_id=log.weekly_target_id,
        impact_score=0
    )
    db.add(new_log)
    db.commit()
    db.refresh(new_log)
    return {"log_id": new_log.id, "impact_score": new_log.impact_score}


@app.get("/logs/today")
def get_today_logs(db: Session = Depends(get_db)):
    """Return all DailyLog entries for today, newest first."""
    logs = db.query(models.DailyLog).filter(
        models.DailyLog.date == date.today()
    ).order_by(models.DailyLog.id.desc()).all()
    return [{"id": l.id, "entry": l.entry, "weekly_target_id": l.weekly_target_id,
             "impact_score": l.impact_score, "date": str(l.date)} for l in logs]


@app.get("/logs/recent")
def get_recent_logs(days: int = 30, db: Session = Depends(get_db)):
    """Return DailyLog entries from the last N days (default 30), newest first."""
    from datetime import timedelta
    cutoff = date.today() - timedelta(days=days)
    logs = db.query(models.DailyLog).filter(
        models.DailyLog.date >= cutoff
    ).order_by(models.DailyLog.date.desc(), models.DailyLog.id.desc()).all()
    return [{"id": l.id, "entry": l.entry, "weekly_target_id": l.weekly_target_id,
             "impact_score": l.impact_score, "date": str(l.date)} for l in logs]


@app.post("/log-impact")
def log_impact(data: schemas.LogImpactCreate, db: Session = Depends(get_db)):
    target = db.query(models.WeeklyTarget).filter(
        models.WeeklyTarget.id == data.weekly_target_id
    ).first()
    if not target:
        raise HTTPException(status_code=404, detail="Weekly target not found")

    daily_log = db.query(models.DailyLog).filter(
        models.DailyLog.id == data.daily_log_id
    ).first()
    if not daily_log:
        raise HTTPException(status_code=404, detail="Daily log not found")

    snapshot = db.query(models.WeeklyTargetSnapshot).filter(
        models.WeeklyTargetSnapshot.weekly_target_id == target.id,
        models.WeeklyTargetSnapshot.week_start == target.week_start,
        models.WeeklyTargetSnapshot.week_end == target.week_end
    ).first()

    if snapshot and snapshot.frozen:
        raise HTTPException(status_code=400, detail="Cannot apply impact; week is frozen.")

    impact_score = data.contribution_level * target.weight
    impact = models.LogImpact(
        daily_log_id=data.daily_log_id,
        weekly_target_id=target.id,
        contribution_level=data.contribution_level,
        impact_score=impact_score
    )
    db.add(impact)
    target.current_value += impact_score
    progress_percent = min(int((target.current_value / target.target_value) * 100), 100)

    if snapshot:
        snapshot.current_value = target.current_value
        snapshot.progress_percent = progress_percent
        snapshot.status = target.status
    else:
        snapshot = models.WeeklyTargetSnapshot(
            weekly_target_id=target.id,
            week_start=target.week_start,
            week_end=target.week_end,
            target_value=target.target_value,
            current_value=target.current_value,
            progress_percent=progress_percent,
            status=target.status,
            frozen=False
        )
        db.add(snapshot)

    db.commit()
    db.refresh(snapshot)
    return {
        "impact_score": impact_score,
        "updated_target_progress": target.current_value,
        "target_value": target.target_value,
        "status": target.status,
        "progress_percent": progress_percent,
        "snapshot_id": snapshot.id
    }


@app.get("/snapshots")
def get_snapshots(week: date, db: Session = Depends(get_db)):
    snapshots = db.query(models.WeeklyTargetSnapshot).filter(
        models.WeeklyTargetSnapshot.week_start <= week,
        models.WeeklyTargetSnapshot.week_end >= week
    ).all()
    return [
        {
            "snapshot_id": s.id,
            "weekly_target_id": s.weekly_target_id,
            "week_start": s.week_start,
            "week_end": s.week_end,
            "target_value": s.target_value,
            "current_value": s.current_value,
            "progress_percent": s.progress_percent,
            "status": s.status,
            "frozen": s.frozen
        }
        for s in snapshots
    ]


@app.get("/weekly-review")
def weekly_review(week: date, db: Session = Depends(get_db)):
    snapshots = db.query(models.WeeklyTargetSnapshot).filter(
        models.WeeklyTargetSnapshot.week_start <= week,
        models.WeeklyTargetSnapshot.week_end >= week
    ).all()
    if not snapshots:
        raise HTTPException(status_code=404, detail="No snapshots found for this week")

    planned_total = sum(s.target_value for s in snapshots)
    actual_total = sum(s.current_value for s in snapshots)
    completed = sum(1 for s in snapshots if s.status == "completed")
    completion_rate = int((completed / len(snapshots)) * 100)

    return {
        "week": str(week),
        "summary": {
            "planned_total": planned_total,
            "actual_total": actual_total,
            "completion_rate_percent": completion_rate,
            "missed_weight": sum(max(0, s.target_value - s.current_value) for s in snapshots),
            "overperformance": sum(max(0, s.current_value - s.target_value) for s in snapshots),
        },
        "targets": [
            {"weekly_target_id": s.weekly_target_id, "planned": s.target_value,
             "actual": s.current_value, "progress_percent": s.progress_percent, "status": s.status}
            for s in snapshots
        ],
        "verdict": (
            "Strong execution week" if completion_rate >= 80 else
            "Inconsistent execution" if completion_rate >= 50 else
            "Execution failure"
        )
    }


@app.get("/weekly-ai-review")
def weekly_ai_review(
    week: date,
    db: Session = Depends(get_db),
    temp_instruction: str | None = Query(None)
):
    try:
        analytics_data = weekly_analytics_internal(week, db)
    except HTTPException:
        raise

    prompt = f"""You are an elite execution analyst advising a high-performance founder.

Weekly execution data: {json.dumps(analytics_data)}

Respond in this exact structure:

SUMMARY:
<2-3 sentences>

LEVERAGE POINT:
<1 sentence>

UNCOMFORTABLE TRUTH:
<1 sentence>

NEXT ACTIONS:
1. ...
2. ...
3. ...

LEADERSHIP PRINCIPLE:
<1 sentence>

Rules: Plain text only. No markdown. Precise and strategic."""

    messages = []
    if temp_instruction:
        messages.append({"role": "system", "content": temp_instruction})
    messages.append({"role": "user", "content": prompt})

    try:
        ai_response = get_chat_response_with_memory(messages, db=db, context_type="weekly_ai_review")
    except Exception as exc:  # noqa: BLE001
        raise _ai_http_error(exc)
    return {"week": str(week), "ai_review": ai_response}


def weekly_analytics_internal(week: date, db: Session) -> dict:
    snapshots = db.query(models.WeeklyTargetSnapshot).filter(
        models.WeeklyTargetSnapshot.week_start <= week,
        models.WeeklyTargetSnapshot.week_end >= week
    ).all()
    if not snapshots:
        raise HTTPException(status_code=404, detail="No data for this week")

    planned_total = sum(s.target_value for s in snapshots)
    actual_total = sum(s.current_value for s in snapshots)
    efficiency = round(actual_total / planned_total, 2) if planned_total > 0 else 0
    total_contribution = sum(s.current_value for s in snapshots)

    target_analysis = []
    for s in snapshots:
        share = round((s.current_value / total_contribution) * 100, 1) if total_contribution > 0 else 0
        target_analysis.append({
            "weekly_target_id": s.weekly_target_id,
            "planned": s.target_value,
            "actual": s.current_value,
            "progress_percent": s.progress_percent,
            "contribution_percent": share,
            "status": s.status
        })
    target_analysis.sort(key=lambda x: x["contribution_percent"], reverse=True)

    top_2 = sum(t["contribution_percent"] for t in target_analysis[:2])
    focus_score = min(int(top_2), 100)

    if efficiency >= 1 and focus_score >= 60:   verdict = "Elite, focused execution"
    elif efficiency >= 0.8:                       verdict = "Strong but unfocused execution"
    elif efficiency >= 0.5:                       verdict = "Busy but ineffective"
    else:                                         verdict = "Execution breakdown"

    return {
        "week": str(week),
        "overview": {"planned_total": planned_total, "actual_total": actual_total,
                     "efficiency_ratio": efficiency, "focus_score": focus_score},
        "target_rankings": target_analysis,
        "verdict": verdict
    }


@app.get("/weekly-analytics")
def weekly_analytics(week: date, db: Session = Depends(get_db)):
    return weekly_analytics_internal(week, db)


@app.get("/memory/recent")
def read_recent_memory(limit: int = Query(5, le=20), context_type: str | None = None):
    db = SessionLocal()
    memories = get_recent_memories(db, limit, context_type)
    return [
        {"id": m.id, "context_type": m.context_type, "response": m.response, "created_at": m.created_at}
        for m in memories
    ]


# ── UTILS ────────────────────────────────────────────────────
def _year_pct() -> float:
    now = datetime.now()
    start = datetime(now.year, 1, 1)
    end = datetime(now.year + 1, 1, 1)
    return (now - start) / (end - start)


def _at_progress_pct(t: AnnualTarget) -> float:
    """Percentage progress for a numeric annual target. Descriptive targets skip this."""
    if t.target_value is None or t.current_value is None:
        return 0.0
    return min(100.0, (t.current_value / max(t.target_value, 1e-9)) * 100)


# ── ROOT → /app/ ─────────────────────────────────────────────
# The frontend is served only from the /app mount above. Served at /, its relative
# "assets/…" URLs resolved to /assets/…, which the auth middleware blocks (an <img> cannot
# send the bearer token). Redirecting keeps every frontend file under /app/ without
# widening the auth exemption.
@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
@app.api_route("/index.html", methods=["GET", "HEAD"], include_in_schema=False)
def root_redirect(request: Request):
    target = "/app/"
    if request.url.query:
        target += "?" + request.url.query
    return RedirectResponse(target, status_code=307)