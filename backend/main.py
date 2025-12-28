from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session
import sqlite3
from datetime import date, timedelta
import models
import schemas
from database import engine, get_db
from database import SessionLocal
from typing import List, Optional, Dict 
from models import AIMemory
from memory_service import get_recent_memories
from fastapi import Query
from apscheduler.schedulers.background import BackgroundScheduler
from openai_client import get_chat_response
from openai_client import get_chat_response_with_memory


models.Base.metadata.create_all(bind=engine)

app = FastAPI()

# CORS for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# SQLite chat memory
conn = sqlite3.connect("chat_memory.db", check_same_thread=False)
c = conn.cursor()
c.execute("""
CREATE TABLE IF NOT EXISTS chats (
    session_id TEXT,
    role TEXT,
    content TEXT
)
""")
conn.commit()


class ChatRequest(BaseModel):
    message: str
    session_id: str
    context: Optional[ExecutionContext] = None


class ExecutionContext(BaseModel):
    weekly_targets: Optional[List[Dict]] = None
    daily_logs: Optional[List[Dict]] = None
    kpis: Optional[Dict] = None


@app.post("/chat")
def chat_endpoint(request: ChatRequest):
    session = request.session_id
    # Load previous messages from DB
    c.execute("SELECT role, content FROM chats WHERE session_id=?", (session,))
    messages = [{"role": r, "content": m} for r, m in c.fetchall()]

    # Append new user message
    messages.append({"role": "user", "content": request.message})
    c.execute("INSERT INTO chats (session_id, role, content) VALUES (?, ?, ?)",
              (session, "user", request.message))
    conn.commit()

    # Get bot response
    bot_reply = get_chat_response_with_memory(
    messages,
    db=SessionLocal(),
    context_type="chat"
)


    # Save bot reply to DB
    c.execute("INSERT INTO chats (session_id, role, content) VALUES (?, ?, ?)",
              (session, "assistant", bot_reply))
    conn.commit()

    return {"reply": bot_reply}


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


@app.post("/logs")
def create_log(log: schemas.DailyLogCreate, db: Session = Depends(get_db)):
    # Ensure target exists
    target = db.query(models.WeeklyTarget).filter(
        models.WeeklyTarget.id == log.weekly_target_id
    ).first()
    if not target:
        raise HTTPException(status_code=404, detail="Target not found")

    # Just create the log; do NOT update current_value here
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
    # Get target
    target = db.query(models.WeeklyTarget).filter(
        models.WeeklyTarget.id == data.weekly_target_id
    ).first()
    if not target:
        raise HTTPException(status_code=404, detail="Weekly target not found")

    # Get daily log
    daily_log = db.query(models.DailyLog).filter(
        models.DailyLog.id == data.daily_log_id
    ).first()
    if not daily_log:
        raise HTTPException(status_code=404, detail="Daily log not found")

    # Get or create snapshot FIRST
    snapshot = db.query(models.WeeklyTargetSnapshot).filter(
        models.WeeklyTargetSnapshot.weekly_target_id == target.id,
        models.WeeklyTargetSnapshot.week_start == target.week_start,
        models.WeeklyTargetSnapshot.week_end == target.week_end
    ).first()

    if snapshot and snapshot.frozen:
        raise HTTPException(
            status_code=400,
            detail="Cannot apply impact; week is frozen."
        )

    # Calculate impact
    impact_score = data.contribution_level * target.weight

    # Create impact record
    impact = models.LogImpact(
        daily_log_id=data.daily_log_id,
        weekly_target_id=target.id,
        contribution_level=data.contribution_level,
        impact_score=impact_score
    )
    db.add(impact)

    # Update target
    target.current_value += impact_score

    # Calculate progress
    progress_percent = min(
        int((target.current_value / target.target_value) * 100), 
        100
    )

    # Create or update snapshot
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



@app.get("/targets")
def get_targets(db: Session = Depends(get_db)):
    return db.query(models.WeeklyTarget).all()

def create_weekly_snapshot(db: Session, target_id: int):
    target = db.query(models.WeeklyTarget).filter(models.WeeklyTarget.id == target_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Target not found")

    snapshot = models.WeeklyTargetSnapshot(
        weekly_target_id=target.id,
        week_start=target.week_start,
        week_end=target.week_end,
        target_value=target.target_value,
        current_value=target.current_value,
        progress_percent=min(int((target.current_value / target.target_value) * 100), 100),
        status=target.status
    )

    db.add(snapshot)
    db.commit()
    db.refresh(snapshot)
    return snapshot

@app.post("/targets/{target_id}/snapshot")
def snapshot_target(target_id: int, db: Session = Depends(get_db)):
    snapshot = create_weekly_snapshot(db, target_id)
    return {"snapshot_id": snapshot.id, "status": snapshot.status}

def freeze_ended_weeks():
    db: Session = SessionLocal()
    try:
        snapshots = db.query(models.WeeklyTargetSnapshot).filter(
            models.WeeklyTargetSnapshot.week_end < date.today(),
            models.WeeklyTargetSnapshot.frozen == False
        ).all()

        for snapshot in snapshots:
            snapshot.frozen = True
            # Optional: lock current_value to prevent changes
            db.add(snapshot)

        db.commit()
        print(f"[Scheduler] Frozen {len(snapshots)} snapshots for ended weeks.")
    finally:
        db.close()

# Initialize scheduler
if __name__ == "__main__":
    scheduler = BackgroundScheduler()
    scheduler.add_job(freeze_ended_weeks, "interval" , days =1)
    scheduler.start()

@app.post("/freeze-week/{snapshot_id}")
def freeze_week(snapshot_id: int, db: Session = Depends(get_db)):
    snapshot = db.query(models.WeeklyTargetSnapshot).filter(
        models.WeeklyTargetSnapshot.id == snapshot_id
    ).first()

    if not snapshot:
        raise HTTPException(status_code=404, detail="Snapshot not found")

    if snapshot.frozen:
        return {"message": "Snapshot already frozen", "snapshot_id": snapshot.id}

    snapshot.frozen = True
    db.commit()
    db.refresh(snapshot)

    return {
        "snapshot_id": snapshot.id,
        "status": "frozen",
        "week_start": snapshot.week_start,
        "week_end": snapshot.week_end
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
    """
    Executive weekly review.
    Read-only. Snapshot-based. Immutable truth.
    """

    snapshots = db.query(models.WeeklyTargetSnapshot).filter(
        models.WeeklyTargetSnapshot.week_start <= week,
        models.WeeklyTargetSnapshot.week_end >= week
    ).all()

    if not snapshots:
        raise HTTPException(status_code=404, detail="No snapshots found for this week")

    planned_total = 0
    actual_total = 0
    missed_weight = 0
    overperformance = 0
    completed = 0

    target_breakdown = []

    for s in snapshots:
        planned_total += s.target_value
        actual_total += s.current_value

        delta = s.current_value - s.target_value

        if delta < 0:
            missed_weight += abs(delta)
        else:
            overperformance += delta

        if s.status == "completed":
            completed += 1

        target_breakdown.append({
            "weekly_target_id": s.weekly_target_id,
            "planned": s.target_value,
            "actual": s.current_value,
            "progress_percent": s.progress_percent,
            "status": s.status
        })

    completion_rate = int((completed / len(snapshots)) * 100)

    return {
        "week": str(week),
        "summary": {
            "planned_total": planned_total,
            "actual_total": actual_total,
            "completion_rate_percent": completion_rate,
            "missed_weight": missed_weight,
            "overperformance": overperformance
        },
        "targets": target_breakdown,
        "verdict": (
            "Strong execution week"
            if completion_rate >= 80 else
            "Inconsistent execution"
            if completion_rate >= 50 else
            "Execution failure"
        )
    }

@app.get("/weekly-score")
def weekly_score(week: date, db: Session = Depends(get_db)):
    snapshots = db.query(models.WeeklyTargetSnapshot).filter(
        models.WeeklyTargetSnapshot.week_start <= week,
        models.WeeklyTargetSnapshot.week_end >= week
    ).all()

    if not snapshots:
        raise HTTPException(status_code=404, detail="No snapshots found for this week")

    total_targets = len(snapshots)
    completed_targets = sum(1 for s in snapshots if s.status == "completed")

    planned_total = sum(s.target_value for s in snapshots)
    actual_total = sum(s.current_value for s in snapshots)

    missed_weight = sum(
        max(0, s.target_value - s.current_value)
        for s in snapshots
    )

    overperformance = sum(
        max(0, s.current_value - s.target_value)
        for s in snapshots
    )

    # --- Score Components ---
    completion_score = (completed_targets / total_targets) * 40

    delivery_score = min((actual_total / planned_total), 1) * 30

    missed_penalty = min((missed_weight / planned_total), 1) * 20

    overperformance_bonus = min((overperformance / planned_total), 1) * 10

    raw_score = (
        completion_score
        + delivery_score
        + overperformance_bonus
        - missed_penalty
    )

    weekly_score = max(0, min(int(raw_score), 100))

    return {
        "week": str(week),
        "weekly_score": weekly_score,
        "components": {
            "completion_score": int(completion_score),
            "delivery_score": int(delivery_score),
            "missed_penalty": int(missed_penalty),
            "overperformance_bonus": int(overperformance_bonus)
        },
        "verdict": (
            "Elite execution"
            if weekly_score >= 85 else
            "Strong but inconsistent"
            if weekly_score >= 65 else
            "Mediocre execution"
            if weekly_score >= 45 else
            "Execution failure"
        )
    }

@app.get("/weekly-analytics")
def weekly_analytics(week: date, db: Session = Depends(get_db)):
    """
    Core execution intelligence.
    This endpoint explains WHAT happened and WHY.
    """

    snapshots = db.query(models.WeeklyTargetSnapshot).filter(
        models.WeeklyTargetSnapshot.week_start <= week,
        models.WeeklyTargetSnapshot.week_end >= week
    ).all()

    if not snapshots:
        raise HTTPException(status_code=404, detail="No data for this week")

    # --- Aggregates ---
    planned_total = sum(s.target_value for s in snapshots)
    actual_total = sum(s.current_value for s in snapshots)

    efficiency = (
        round(actual_total / planned_total, 2)
        if planned_total > 0 else 0
    )

    # --- Target contribution ranking ---
    target_analysis = []
    total_contribution = sum(s.current_value for s in snapshots)

    for s in snapshots:
        contribution_share = (
            round((s.current_value / total_contribution) * 100, 1)
            if total_contribution > 0 else 0
        )

        target_analysis.append({
            "weekly_target_id": s.weekly_target_id,
            "planned": s.target_value,
            "actual": s.current_value,
            "progress_percent": s.progress_percent,
            "contribution_percent": contribution_share,
            "status": s.status
        })

    target_analysis.sort(
        key=lambda x: x["contribution_percent"],
        reverse=True
    )

    # --- Focus score (Pareto check) ---
    top_2_contribution = sum(
        t["contribution_percent"] for t in target_analysis[:2]
    )

    focus_score = min(int(top_2_contribution), 100)

    # --- Verdict logic ---
    if efficiency >= 1 and focus_score >= 60:
        verdict = "Elite, focused execution"
    elif efficiency >= 0.8:
        verdict = "Strong but unfocused execution"
    elif efficiency >= 0.5:
        verdict = "Busy but ineffective"
    else:
        verdict = "Execution breakdown"

    return {
        "week": str(week),
        "overview": {
            "planned_total": planned_total,
            "actual_total": actual_total,
            "efficiency_ratio": efficiency,
            "focus_score": focus_score
        },
        "target_rankings": target_analysis,
        "verdict": verdict
    }

@app.get("/weekly-ai-review")
def weekly_ai_review(
    week: date,
    db: Session = Depends(get_db),
    temp_instruction: str | None = Query(None, description="Optional temporary instruction for AI")
):
    """
    AI executive interpretation layer.
    Converts analytics into judgement and direction.
    """

    analytics = weekly_analytics(week, db)

    prompt = f"""
You are an elite execution analyst advising a high-performance founder.

Here is the weekly execution data:
{analytics}

Respond in the following exact structure:

SUMMARY:
<2–3 sentences>

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

Rules:
- Do not use markdown
- Do not add extra sections
- Be precise, unsentimental, and strategic
"""

    # Add temp_instruction at the start of the messages if provided
    messages = []
    if temp_instruction:
        messages.append({"role": "system", "content": temp_instruction})
    messages.append({"role": "user", "content": prompt})

    ai_response = get_chat_response_with_memory(
        messages,
        db=db,
        context_type="weekly_ai_review"
    )

    return {
        "week": str(week),
        "analytics_snapshot": analytics["overview"],
        "ai_review": ai_response
    }


@app.get("/memory/recent")
def read_recent_memory(
    limit: int = Query(5, le=20),
    context_type: str | None = None
):
    db = SessionLocal()
    memories = get_recent_memories(db, limit, context_type)

    return [
        {
            "id": m.id,
            "context_type": m.context_type,
            "response": m.response,
            "created_at": m.created_at
        }
        for m in memories
    ]

