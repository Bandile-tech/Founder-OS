from pydantic import BaseModel
from datetime import date
from typing import Optional

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
