from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func
import sqlite3
from datetime import date, timedelta, datetime
from typing import List, Optional, Dict, Any
import json
import os
import database
import models
import schemas
from database import engine, get_db, SessionLocal
from models import AIMemory, Habit, AnnualTarget, KPISnapshot, RoadmapTask
from memory_service import get_recent_memories
from openai_client import (
    get_chat_response_with_memory,
    get_parse_response,
    get_proactive_brief,
    get_radar_scores,
)

# ── CREATE TABLES ────────────────────────────────────────────
models.Base.metadata.create_all(bind=engine)

# ── APP ──────────────────────────────────────────────────────
app = FastAPI(title="Founder OS API", version="2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve frontend from ../frontend if it exists
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(FRONTEND_DIR):
    app.mount("/app", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")

# ── CHAT MEMORY (SQLite, session-based) ──────────────────────
conn = sqlite3.connect("chat_memory.db", check_same_thread=False)
c = conn.cursor()
c.execute("""
CREATE TABLE IF NOT EXISTS chats (
    session_id TEXT,
    role TEXT,
    content TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
)
""")
conn.commit()


# ── KPI DEFAULTS ─────────────────────────────────────────────
# In-memory KPI store (persisted via KPISnapshot on update)
KPI_DEFAULTS = {
    "sprint_100m":    {"label": "100m PB",          "value": 11.2,  "unit": "s",  "target": 10.8, "lower_is_better": True},
    "sprint_200m":    {"label": "200m PB",          "value": 23.1,  "unit": "s",  "target": 22.5, "lower_is_better": True},
    "sprint_400m":    {"label": "400m PB",          "value": 55.99, "unit": "s",  "target": 53.0, "lower_is_better": True},
    "maths_syllabus": {"label": "Maths 9709",       "value": 62,    "unit": "%",  "target": 100,  "lower_is_better": False},
    "further_maths":  {"label": "Further Maths 9231","value": 48,   "unit": "%",  "target": 100,  "lower_is_better": False},
    "business":       {"label": "Business 9609",    "value": 71,    "unit": "%",  "target": 100,  "lower_is_better": False},
    "economics":      {"label": "Economics 9708",   "value": 55,    "unit": "%",  "target": 100,  "lower_is_better": False},
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
@app.on_event("startup")
async def startup():
    db = SessionLocal()
    try:
        load_kpi_state(db)
        _seed_habits_if_empty(db)
        _seed_annual_targets_if_empty(db)
        _seed_roadmap_tasks_if_empty(db)
    finally:
        db.close()


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

def _seed_annual_targets_if_empty(db: Session):
    if db.query(AnnualTarget).count() == 0:
        defaults = [
            AnnualTarget(name="Public brand followers", current_value=0,  target_value=1000, unit="followers", category="business"),
            AnnualTarget(name="AI services revenue",    current_value=0,  target_value=2000, unit="USD",       category="business"),
            AnnualTarget(name="400m personal best",     current_value=55.99, target_value=48.0, unit="s",     category="athletics", lower_is_better=True),
            AnnualTarget(name="CS50P completion",       current_value=15, target_value=100,  unit="%",        category="business"),
        ]
        for d in defaults:
            db.add(d)
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
    return {"ok": True}


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
    target_date = for_date or date.today()
    # Ensure today's habits exist
    existing = {h.key: h for h in db.query(Habit).filter(Habit.date == target_date).all()}
    if not existing:
        _seed_habits_if_empty(db)
        existing = {h.key: h for h in db.query(Habit).filter(Habit.date == target_date).all()}
    return [
        {"id": h.id, "key": h.key, "label": h.label, "done": h.done, "date": str(h.date)}
        for h in existing.values()
    ]


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
@app.get("/annual-targets")
def get_annual_targets(year: int = 2026, db: Session = Depends(get_db)):
    targets = db.query(AnnualTarget).filter(AnnualTarget.year == year).all()
    year_pct = _year_pct()
    result = []
    for t in targets:
        pct = _at_progress_pct(t)
        expected = year_pct * 100
        gap = pct - expected
        if gap > 10:   status = "ahead"
        elif gap > -10: status = "on_track"
        elif gap > -25: status = "behind"
        else:           status = "critical"
        result.append({
            "id": t.id,
            "name": t.name,
            "current_value": t.current_value,
            "target_value": t.target_value,
            "unit": t.unit,
            "category": t.category,
            "lower_is_better": t.lower_is_better,
            "year": t.year,
            "progress_pct": round(pct),
            "expected_pct": round(expected),
            "status": status,
        })
    return result


@app.post("/annual-targets")
def create_annual_target(payload: schemas.AnnualTargetCreate, db: Session = Depends(get_db)):
    t = AnnualTarget(**payload.dict())
    db.add(t)
    db.commit()
    db.refresh(t)
    return {"id": t.id, "name": t.name}


@app.patch("/annual-targets/{target_id}")
def update_annual_target(target_id: int, payload: schemas.AnnualTargetUpdate, db: Session = Depends(get_db)):
    t = db.query(AnnualTarget).filter(AnnualTarget.id == target_id).first()
    if not t:
        raise HTTPException(404, "Annual target not found")
    t.current_value = payload.current_value
    t.updated_at = datetime.utcnow()
    db.commit()
    return {"id": t.id, "current_value": t.current_value}


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
        source=todo.get("source", "manual"),
        roadmap_id=todo.get("roadmapId")
    )
    db.add(new_todo)
    db.commit()
    db.refresh(new_todo)
    return new_todo

@app.post("/todos/{todo_id}/toggle")
def toggle_todo(todo_id: int, db: Session = Depends(get_db)):
    todo = db.query(models.Todo).filter(models.Todo.id == todo_id).first()
    if not todo:
        raise HTTPException(404, "Todo not found")
    todo.done = not todo.done
    db.commit()
    return {"id": todo.id, "done": todo.done}

# ── RADAR ────────────────────────────────────────────────────
@app.get("/radar")
def get_radar(db: Session = Depends(get_db)):
    """Compute radar domain scores from live system data."""
    habits_today = {h.key: h.done for h in db.query(Habit).filter(Habit.date == date.today()).all()}
    annual = [
        {"name": t.name, "current": t.current_value, "target": t.target_value,
         "category": t.category, "lower_is_better": t.lower_is_better}
        for t in db.query(AnnualTarget).filter(AnnualTarget.year == date.today().year).all()
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

    context = {
        "kpis": _kpi_state,
        "habits": habits_today,
        "annual_targets": annual,
        "bible_streak": streak,
        "roadmap_pct": {"sprint": sprint_pct, "academic": acad_pct},
        "social_score": 50,  # manual until social tracking added
    }

    scores = get_radar_scores(context)
    composite = round(sum(scores.values()) / len(scores))
    return {"scores": scores, "composite": composite, "computed_at": str(datetime.utcnow())}


# ── PARSE (Brain Dump) ───────────────────────────────────────
@app.post("/parse")
def parse_brain_dump(payload: schemas.ParseRequest, db: Session = Depends(get_db)):
    global _kpi_state

    roadmap_ids = [t.task_id for t in db.query(RoadmapTask).all()]
    context = {"roadmap_ids": roadmap_ids, "today": str(date.today())}

    parsed = get_parse_response(payload.text, context)

    # Apply KPI updates
    kpi_updates_applied = []
    if parsed.get("kpi_updates"):
        for u in parsed["kpi_updates"]:
            if u["key"] in _kpi_state:
                _kpi_state[u["key"]]["value"] = u["value"]
                db.add(KPISnapshot(key=u["key"], value=u["value"], date=date.today()))
                kpi_updates_applied.append(u["key"])
        if kpi_updates_applied:
            db.commit()

    # Apply roadmap completions
    if parsed.get("roadmap_complete"):
        for task_id in parsed["roadmap_complete"]:
            task = db.query(RoadmapTask).filter(RoadmapTask.task_id == task_id).first()
            if task:
                task.done = True
        db.commit()

    # Apply habit updates
    if parsed.get("habits_done"):
        for key in parsed["habits_done"]:
            habit = db.query(Habit).filter(Habit.key == key, Habit.date == date.today()).first()
            if habit:
                habit.done = True
        db.commit()

    # Apply annual target updates
    if parsed.get("annual_updates"):
        for u in parsed["annual_updates"]:
            frag = u.get("name_fragment", "").lower()
            target = db.query(AnnualTarget).filter(
                func.lower(AnnualTarget.name).contains(frag)
            ).first()
            if target:
                target.current_value = u["current"]
                target.updated_at = datetime.utcnow()
        db.commit()

    if parsed.get("todos_add"):
        for t in parsed["todos_add"]:
            new_todo = models.Todo(
                text=t["text"],
                priority=t.get("priority", 5),
                category=t.get("category", "personal"),
                due=t.get("due", str(date.today())),
                source="ai",
                roadmap_id=t.get("roadmapId")
            )
            db.add(new_todo)
        db.commit()
        
    return {
        "summary": parsed.get("summary") or "Brain dump processed.",
        "advisory": parsed.get("advisory") or "Character Before Status. Maintain discipline.",
        "kpi_updates_applied": kpi_updates_applied,
        "status": "ok"
    }


# ── PROACTIVE BRIEF ──────────────────────────────────────────
@app.post("/proactive-brief")
def proactive_brief(payload: schemas.ProactiveBriefRequest):
    brief = get_proactive_brief(payload.context)
    return {"brief": brief}


# ── CHAT ─────────────────────────────────────────────────────
@app.post("/chat")
def chat_endpoint(request: schemas.ChatRequest, db: Session = Depends(get_db)):
    session = request.session_id

    c.execute("SELECT role, content FROM chats WHERE session_id=? ORDER BY rowid", (session,))
    messages = [{"role": r, "content": m} for r, m in c.fetchall()]
    messages.append({"role": "user", "content": request.message})

    c.execute("INSERT INTO chats (session_id, role, content) VALUES (?, ?, ?)",
              (session, "user", request.message))
    conn.commit()

    # Inject live context if provided
    context_note = ""
    if request.context:
        context_note = f"\n\n[Live system context]: {json.dumps(request.context)}"
        messages[-1]["content"] += context_note

    bot_reply = get_chat_response_with_memory(messages, db=db, context_type="chat")

    c.execute("INSERT INTO chats (session_id, role, content) VALUES (?, ?, ?)",
              (session, "assistant", bot_reply))
    conn.commit()

    return {"reply": bot_reply}


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

    ai_response = get_chat_response_with_memory(messages, db=db, context_type="weekly_ai_review")
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
    if t.lower_is_better:
        if t.current_value <= 0:
            return 0
        gap = t.current_value * 0.15
        d = t.current_value - t.target_value
        return max(0, min(100, ((gap - d) / gap) * 100))
    return min(100, (t.current_value / max(t.target_value, 1)) * 100)


# 1. Get the path to your frontend folder
# This logic looks one folder up from 'backend' to find 'frontend'
current_dir = os.path.dirname(os.path.realpath(__file__))
frontend_path = os.path.join(current_dir, "..", "frontend")

# 2. Serve your index.html at the main address
app.mount("/", StaticFiles(directory=frontend_path, html=True), name="frontend")