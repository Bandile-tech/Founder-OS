from sqlalchemy import Column, Integer, String, Date, Float, Text, DateTime, ForeignKey,Boolean
from datetime import datetime
from database import Base

class WeeklyTarget(Base):
    __tablename__ = "weekly_targets"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    description = Column(Text)

    week_start = Column(Date)
    week_end = Column(Date)

    target_value = Column(Float, nullable=False)
    current_value = Column(Float, default=0)

    weight = Column(Integer, nullable=False)  # 1–5
    
    @property
    def status(self):
        return "completed" if self.current_value >= self.target_value else "active"

    created_at = Column(DateTime, default=datetime.utcnow)

class DailyLog(Base):
    __tablename__ = "daily_logs"

    id = Column(Integer, primary_key=True, index=True)
    date = Column(Date)
    entry = Column(String)
    
    weekly_target_id = Column(Integer, ForeignKey("weekly_targets.id"))
    impact_score = Column(Integer, default=0)
    
class LogImpact(Base):
    __tablename__ = "log_impacts"

    id = Column(Integer, primary_key=True, index=True)

    daily_log_id = Column(Integer, ForeignKey("daily_logs.id"))
    weekly_target_id = Column(Integer, ForeignKey("weekly_targets.id"))

    contribution_level = Column(Integer, nullable=False)  # 1–3
    impact_score = Column(Integer, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)

class WeeklyTargetSnapshot(Base):
    __tablename__ = "weekly_target_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    weekly_target_id = Column(Integer, ForeignKey("weekly_targets.id"))
    
    week_start = Column(Date)
    week_end = Column(Date)

    target_value = Column(Float)
    current_value = Column(Float)
    progress_percent = Column(Integer)
    status = Column(String)
    frozen = Column(Boolean, default=False)  # new column
    created_at = Column(DateTime, default=datetime.utcnow)
