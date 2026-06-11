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
    weekly_target_id: Optional[int] = None
    contribution_level: int = 0


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
    current_value: Optional[float] = None
    target_value: Optional[float] = None
    unit: Optional[str] = None
    display_value: Optional[str] = None
    is_complete: bool = False
    priority: int = 3
    is_active: bool = True
    sort_order: int = 0


class AnnualTargetUpdate(BaseModel):
    current_value: Optional[float] = None
    target_value: Optional[float] = None
    unit: Optional[str] = None
    display_value: Optional[str] = None
    is_complete: Optional[bool] = None
    priority: Optional[int] = None
    is_active: Optional[bool] = None
    sort_order: Optional[int] = None


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


class TodoCreate(BaseModel):
    text: str
    priority: int = 5
    category: str = "personal"
    due: Optional[str] = None
    source: str = "manual"
    roadmapId: Optional[str] = None


class ParseRequest(BaseModel):
    text: str
    session_id: Optional[str] = None


class UnifiedInputRequest(BaseModel):
    text: str
    session_id: str
    context: Optional[Dict[str, Any]] = None


class ChatRequest(BaseModel):
    message: str
    session_id: str
    context: Optional[Dict[str, Any]] = None


class ProactiveBriefRequest(BaseModel):
    context: Dict[str, Any]


class BibleEntryCreate(BaseModel):
    ref: str
    date: date


class BookCreate(BaseModel):
    title: str
    author: str = ""
    status: str = "queue"
    page: int = 0
    total_pages: int = 0
    position: int = 0
    is_currently_reading: bool = False


class BookUpdate(BaseModel):
    status: Optional[str] = None
    page: Optional[int] = None
    total_pages: Optional[int] = None
    position: Optional[int] = None
    is_currently_reading: Optional[bool] = None


# ── WAR ROOM ──────────────────────────────────────────────────

class DocumentCreate(BaseModel):
    title: str
    content: str
    source_type: str = "paste"


class DocumentUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None


class NonNegotiableCreate(BaseModel):
    key: str
    label: str
    sort_order: int = 0


class NonNegotiablePatch(BaseModel):
    label: Optional[str] = None
    is_active: Optional[bool] = None
    sort_order: Optional[int] = None


class ReadingPlanCreate(BaseModel):
    name: str


class ReadingPlanEntryCreate(BaseModel):
    ref: str
    day_number: int


class ReadingPlanEntryPatch(BaseModel):
    done: Optional[bool] = None
    pushed: Optional[int] = None


class SocialScoreUpdate(BaseModel):
    value: int


class ClientCreate(BaseModel):
    name: str
    company: str = ""
    status: str = "prospect"
    value: float = 0
    service: str = ""
    notes: str = ""


class ClientUpdate(BaseModel):
    status: Optional[str] = None
    value: Optional[float] = None
    notes: Optional[str] = None
    service: Optional[str] = None


class RevenueCreate(BaseModel):
    amount: float
    source: str = ""
    client_id: Optional[int] = None
    date: date
    notes: str = ""


# ── ACADEMIC ROADMAP ─────────────────────────────────────────

class SubjectCreate(BaseModel):
    name: str
    code: str
    exam_date: Optional[date] = None
    sort_order: int = 0


class SubjectUpdate(BaseModel):
    name: Optional[str] = None
    exam_date: Optional[date] = None
    sort_order: Optional[int] = None


class TopicCreate(BaseModel):
    name: str
    syllabus_weight: int = 5
    sort_order: int = 0


class TopicUpdate(BaseModel):
    name: Optional[str] = None
    syllabus_weight: Optional[int] = None
    sort_order: Optional[int] = None


class SubtopicCreate(BaseModel):
    name: str
    mastery_level: int = 0
    notes: Optional[str] = None
    sort_order: int = 0


class SubtopicUpdate(BaseModel):
    name: Optional[str] = None
    mastery_level: Optional[int] = None
    last_reviewed_date: Optional[date] = None
    notes: Optional[str] = None
    sort_order: Optional[int] = None


class SubjectProgressOut(BaseModel):
    subject_id: int
    subject_name: str
    code: str
    mastery_pct: Optional[float]    # None when zero subtopics
    weighted_pct: Optional[float]   # None when zero subtopics
    subtopic_count: int


class WeakestSubtopicOut(BaseModel):
    subtopic_id: int
    name: str
    mastery_level: int
    topic_name: str
    topic_weight: int
    subject_name: str
    subject_code: str


# ── HEALTH MODULE ─────────────────────────────────────────────

class DailyHealthPatch(BaseModel):
    sleep_hours: Optional[float] = None
    mobility_done: Optional[bool] = None
    session_done: Optional[bool] = None
    notes: Optional[str] = None


class WeeklyHealthPatch(BaseModel):
    bodyweight_kg: Optional[float] = None
    protein_target_hit: Optional[bool] = None
    any_lift_progressed: Optional[bool] = None
    energy_level: Optional[int] = None


class LiftCreate(BaseModel):
    name: str
    sort_order: int = 0


class LiftPatch(BaseModel):
    name: Optional[str] = None
    sort_order: Optional[int] = None
    is_active: Optional[bool] = None


class LiftLogCreate(BaseModel):
    lift_name: str
    date: date
    weight_kg: float
    reps: int
    notes: Optional[str] = None


class LiftLogPatch(BaseModel):
    weight_kg: Optional[float] = None
    reps: Optional[int] = None
    notes: Optional[str] = None


# ── TRADING MODULE ─────────────────────────────────────────────

class PropFirmAccountCreate(BaseModel):
    name: str
    firm: str
    account_size_usd: float
    challenge_type: str
    starting_balance: float
    current_balance: float
    profit_target_pct: float = 8.0
    max_drawdown_pct: float = 6.0
    daily_drawdown_pct: Optional[float] = None
    start_date: date
    status: str = "active"
    notes: Optional[str] = None


class PropFirmAccountPatch(BaseModel):
    name: Optional[str] = None
    firm: Optional[str] = None
    account_size_usd: Optional[float] = None
    challenge_type: Optional[str] = None
    current_balance: Optional[float] = None
    profit_target_pct: Optional[float] = None
    max_drawdown_pct: Optional[float] = None
    daily_drawdown_pct: Optional[float] = None
    status: Optional[str] = None
    notes: Optional[str] = None


class BacktestTradeCreate(BaseModel):
    date: date
    time_of_day: Optional[str] = None
    pair: str
    direction: str
    entry_reason: Optional[str] = None
    r_multiple: float
    rule_adherence: bool
    outcome: str
    notes: Optional[str] = None


class BacktestTradePatch(BaseModel):
    date: Optional[date] = None
    time_of_day: Optional[str] = None
    pair: Optional[str] = None
    direction: Optional[str] = None
    entry_reason: Optional[str] = None
    r_multiple: Optional[float] = None
    rule_adherence: Optional[bool] = None
    outcome: Optional[str] = None
    notes: Optional[str] = None


class LiveTradeCreate(BaseModel):
    date: date
    time_of_day: Optional[str] = None
    pair: str
    direction: str
    entry_reason: Optional[str] = None
    r_multiple: float
    rule_adherence: bool
    outcome: str
    notes: Optional[str] = None
    account_id: Optional[int] = None
    risk_pct: Optional[float] = None
    net_pl_usd: Optional[float] = None
    rule_broken: bool = False
    rule_broken_description: Optional[str] = None


class LiveTradePatch(BaseModel):
    date: Optional[date] = None
    time_of_day: Optional[str] = None
    pair: Optional[str] = None
    direction: Optional[str] = None
    entry_reason: Optional[str] = None
    r_multiple: Optional[float] = None
    rule_adherence: Optional[bool] = None
    outcome: Optional[str] = None
    notes: Optional[str] = None
    account_id: Optional[int] = None
    risk_pct: Optional[float] = None
    net_pl_usd: Optional[float] = None
    rule_broken: Optional[bool] = None
    rule_broken_description: Optional[str] = None
