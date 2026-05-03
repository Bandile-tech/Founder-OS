from pydantic import BaseModel
from datetime import date
from typing import Optional, List, Dict, Any


# ── EXISTING ────────────────────────────────────────────────

class WeeklyTargetCreate(BaseModel):
    title: str
    description: Optional[str] = None
    week_start: date
    week_end: date
    target_value: float
    weight: int  # 1–5


class DailyLogCreate(BaseModel):
    date: date
    entry: str
    weekly_target_id: int
    contribution_level: int


class LogImpactCreate(BaseModel):
    daily_log_id: int
    weekly_target_id: int
    contribution_level: int  # 1–3


# ── NEW ─────────────────────────────────────────────────────

class HabitToggle(BaseModel):
    key: str
    label: str
    done: bool
    date: date


class HabitBulkUpsert(BaseModel):
    habits: List[HabitToggle]


class AnnualTargetCreate(BaseModel):
    name: str
    current_value: float = 0
    target_value: float
    unit: str = ""
    category: str = "personal"
    lower_is_better: bool = False
    year: int = 2026


class AnnualTargetUpdate(BaseModel):
    current_value: float


class KPIUpdate(BaseModel):
    key: str
    value: float


class KPIBulkUpdate(BaseModel):
    updates: List[KPIUpdate]


class RoadmapTaskUpdate(BaseModel):
    task_id: str
    roadmap: str        # "sprint" | "academic"
    phase_id: str
    done: bool


class RoadmapBulkUpdate(BaseModel):
    tasks: List[RoadmapTaskUpdate]


class ParseRequest(BaseModel):
    text: str
    session_id: Optional[str] = None


class ChatRequest(BaseModel):
    message: str
    session_id: str
    context: Optional[Dict[str, Any]] = None


class ProactiveBriefRequest(BaseModel):
    context: Dict[str, Any]
