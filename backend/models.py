from sqlalchemy import Column, Integer, String, Date, Float, Text, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import relationship
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
    weekly_target_id = Column(Integer, ForeignKey("weekly_targets.id"), nullable=True)
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
    frozen = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class AIMemory(Base):
    __tablename__ = "ai_memory"

    id = Column(Integer, primary_key=True, index=True)
    user = Column(String, default="Bandile")
    project = Column(String, default="founder_os")
    context_type = Column(String)
    context_data = Column(Text)
    response = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)


# ── NEW MODELS ──────────────────────────────────────────────

class Habit(Base):
    """Tracks daily habit completion. One row per habit per day."""
    __tablename__ = "habits"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String, nullable=False)        # e.g. "scripture_prayer"
    label = Column(String, nullable=False)
    done = Column(Boolean, default=False)
    date = Column(Date, nullable=False)         # which day this record is for
    created_at = Column(DateTime, default=datetime.utcnow)


class AnnualTarget(Base):
    """Year-level goals. Numeric targets have target_value; descriptive ones use display_value."""
    __tablename__ = "annual_targets"

    id            = Column(Integer, primary_key=True, index=True)
    name          = Column(String, nullable=False)
    current_value = Column(Float, nullable=True)
    target_value  = Column(Float, nullable=True)
    unit          = Column(String, nullable=True)
    display_value = Column(String, nullable=True)   # for non-numeric / descriptive targets
    is_complete   = Column(Boolean, default=False)  # only used when target_value is null
    priority      = Column(Integer, default=3)      # 1=high, 5=low
    is_active     = Column(Boolean, default=True)
    sort_order    = Column(Integer, default=0)
    created_at    = Column(DateTime, default=datetime.utcnow)
    updated_at    = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class KPISnapshot(Base):
    """Point-in-time KPI values for trending."""
    __tablename__ = "kpi_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String, nullable=False)        # e.g. "sprint_400m"
    value = Column(Float, nullable=False)
    date = Column(Date, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class RoadmapTask(Base):
    """Persists roadmap task completion state."""
    __tablename__ = "roadmap_tasks"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(String, nullable=False, unique=True)  # e.g. "sprint-acc"
    roadmap = Column(String, nullable=False)               # "sprint" | "academic"
    phase_id = Column(String)
    done = Column(Boolean, default=False)
    pushed_count = Column(Integer, default=0)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class Todo(Base):
    __tablename__ = "todos"
    id = Column(Integer, primary_key=True, index=True)
    text = Column(String)
    priority = Column(Integer, default=5)
    done = Column(Boolean, default=False)
    category = Column(String)
    due = Column(String)
    source = Column(String)
    roadmap_id = Column(String, nullable=True)


class ChatMessage(Base):
    """Replaces chat_memory.db — stores per-session chat history in founder_os.db."""
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String, nullable=False, index=True)
    role = Column(String, nullable=False)       # "user" | "assistant"
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class BibleEntry(Base):
    """Daily Bible reading plan entries."""
    __tablename__ = "bible_entries"

    id = Column(Integer, primary_key=True, index=True)
    ref = Column(String, nullable=False)
    date = Column(Date, nullable=False)
    done = Column(Boolean, default=False)
    pushed = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class Book(Base):
    """Reading queue — from queue through done."""
    __tablename__ = "books"

    id                  = Column(Integer, primary_key=True, index=True)
    title               = Column(String, nullable=False)
    author              = Column(String, default="")
    status              = Column(String, default="queue")    # reading|queue|done
    page                = Column(Integer, default=0)
    total_pages         = Column(Integer, default=0)
    position            = Column(Integer, default=0)         # display order in queue
    is_currently_reading= Column(Boolean, default=False)
    created_at          = Column(DateTime, default=datetime.utcnow)


class SocialScore(Base):
    """Daily social/network score (0–100), feeds radar."""
    __tablename__ = "social_scores"

    id = Column(Integer, primary_key=True, index=True)
    value = Column(Integer, nullable=False)
    date = Column(Date, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class Client(Base):
    """Aether business pipeline — prospect → active → closed."""
    __tablename__ = "clients"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    company = Column(String, default="")
    status = Column(String, default="prospect")  # prospect|active|closed|lost
    value = Column(Float, default=0)
    service = Column(String, default="")
    notes = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Revenue(Base):
    """Individual revenue transactions."""
    __tablename__ = "revenue"

    id = Column(Integer, primary_key=True, index=True)
    amount = Column(Float, nullable=False)
    source = Column(String, default="")
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=True)
    date = Column(Date, nullable=False)
    notes = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)


# ── ACADEMIC ROADMAP ────────────────────────────────────────

class Subject(Base):
    """One row per A-Level subject (e.g. Maths 9709)."""
    __tablename__ = "subjects"

    id         = Column(Integer, primary_key=True, index=True)
    name       = Column(String, nullable=False)        # "Maths 9709"
    code       = Column(String, nullable=False, unique=True)  # "9709"
    exam_date  = Column(Date, nullable=True)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

    topics = relationship(
        "Topic", back_populates="subject",
        cascade="all, delete-orphan",
        order_by="Topic.sort_order",
    )


class Topic(Base):
    """A chapter / paper within a subject (e.g. Pure Mathematics 1)."""
    __tablename__ = "topics"

    id              = Column(Integer, primary_key=True, index=True)
    subject_id      = Column(Integer, ForeignKey("subjects.id", ondelete="CASCADE"), nullable=False)
    name            = Column(String, nullable=False)
    sort_order      = Column(Integer, default=0)
    syllabus_weight = Column(Integer, default=5)  # 1-10, used for weighted_pct
    created_at      = Column(DateTime, default=datetime.utcnow)

    subject   = relationship("Subject", back_populates="topics")
    subtopics = relationship(
        "Subtopic", back_populates="topic",
        cascade="all, delete-orphan",
        order_by="Subtopic.sort_order",
    )


class Subtopic(Base):
    """A specific revision item within a topic."""
    __tablename__ = "subtopics"

    id                 = Column(Integer, primary_key=True, index=True)
    topic_id           = Column(Integer, ForeignKey("topics.id", ondelete="CASCADE"), nullable=False)
    name               = Column(String, nullable=False)
    mastery_level      = Column(Integer, default=0)   # 0-100
    last_reviewed_date = Column(Date, nullable=True)
    notes              = Column(Text, nullable=True)
    sort_order         = Column(Integer, default=0)
    created_at         = Column(DateTime, default=datetime.utcnow)

    topic = relationship("Topic", back_populates="subtopics")


# ── HEALTH MODULE ────────────────────────────────────────────

class DailyHealth(Base):
    """One row per calendar day. Captures sleep and mobility; lifts are in LiftLog."""
    __tablename__ = "daily_health"

    id            = Column(Integer, primary_key=True, index=True)
    date          = Column(Date, nullable=False, unique=True)
    sleep_hours   = Column(Float, nullable=True)
    mobility_done = Column(Boolean, default=False)
    session_done  = Column(Boolean, default=False)
    notes         = Column(Text, nullable=True)
    created_at    = Column(DateTime, default=datetime.utcnow)
    updated_at    = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Lift(Base):
    """User-editable list of tracked compound lifts."""
    __tablename__ = "lifts"

    id         = Column(Integer, primary_key=True, index=True)
    name       = Column(String, nullable=False, unique=True)
    sort_order = Column(Integer, default=0)
    is_active  = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    logs = relationship("LiftLog", back_populates="lift", cascade="all, delete-orphan")


class LiftLog(Base):
    """One row per logged set. Multiple rows per day allowed."""
    __tablename__ = "lift_logs"

    id         = Column(Integer, primary_key=True, index=True)
    lift_id    = Column(Integer, ForeignKey("lifts.id"), nullable=False)
    lift_name  = Column(String, nullable=False)          # denormalised for fast queries
    date       = Column(Date, nullable=False)
    weight_kg  = Column(Float, nullable=False)
    reps       = Column(Integer, nullable=False)
    notes      = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    lift = relationship("Lift", back_populates="logs")


class WeeklyHealth(Base):
    """One row per ISO week (keyed by Monday). Weekly check-in data."""
    __tablename__ = "weekly_health"

    id                   = Column(Integer, primary_key=True, index=True)
    week_start_date      = Column(Date, nullable=False, unique=True)
    bodyweight_kg        = Column(Float, nullable=True)
    protein_target_hit   = Column(Boolean, nullable=True)
    any_lift_progressed  = Column(Boolean, nullable=True)
    energy_level         = Column(Integer, nullable=True)   # 1–5
    created_at           = Column(DateTime, default=datetime.utcnow)
    updated_at           = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ── TRADING MODULE ───────────────────────────────────────────

class PropFirmAccount(Base):
    """A prop firm challenge or funded account."""
    __tablename__ = "prop_firm_accounts"

    id                  = Column(Integer, primary_key=True, index=True)
    name                = Column(String, nullable=False)
    firm                = Column(String, nullable=False)
    account_size_usd    = Column(Float, nullable=False)
    challenge_type      = Column(String, nullable=False)
    starting_balance    = Column(Float, nullable=False)
    current_balance     = Column(Float, nullable=False)
    peak_balance        = Column(Float, nullable=False)
    profit_target_pct   = Column(Float, default=8.0)
    max_drawdown_pct    = Column(Float, default=6.0)
    daily_drawdown_pct  = Column(Float, nullable=True)
    start_date          = Column(Date, nullable=False)
    status              = Column(String, default="active")  # active|passed|blown|withdrawn
    notes               = Column(Text, nullable=True)
    created_at          = Column(DateTime, default=datetime.utcnow)
    updated_at          = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    live_trades = relationship("LiveTrade", back_populates="account")


class BacktestTrade(Base):
    """One backtest trade logged from TradingView replay."""
    __tablename__ = "backtest_trades"

    id             = Column(Integer, primary_key=True, index=True)
    date           = Column(Date, nullable=False)
    time_of_day    = Column(String, nullable=True)
    pair           = Column(String, nullable=False)
    direction      = Column(String, nullable=False)     # long|short
    entry_reason   = Column(String, nullable=True)
    r_multiple     = Column(Float, nullable=False)
    rule_adherence = Column(Boolean, nullable=False)
    outcome        = Column(String, nullable=False)     # win|loss|breakeven
    notes          = Column(Text, nullable=True)
    created_at     = Column(DateTime, default=datetime.utcnow)


class LiveTrade(Base):
    """A live or sim trade executed on a prop account."""
    __tablename__ = "live_trades"

    id                       = Column(Integer, primary_key=True, index=True)
    date                     = Column(Date, nullable=False)
    time_of_day              = Column(String, nullable=True)
    pair                     = Column(String, nullable=False)
    direction                = Column(String, nullable=False)
    entry_reason             = Column(String, nullable=True)
    r_multiple               = Column(Float, nullable=False)
    rule_adherence           = Column(Boolean, nullable=False)
    outcome                  = Column(String, nullable=False)
    notes                    = Column(Text, nullable=True)
    account_id               = Column(Integer, ForeignKey("prop_firm_accounts.id"), nullable=True)
    risk_pct                 = Column(Float, nullable=True)
    net_pl_usd               = Column(Float, nullable=True)
    rule_broken              = Column(Boolean, default=False)
    rule_broken_description  = Column(String, nullable=True)
    created_at               = Column(DateTime, default=datetime.utcnow)

    account = relationship("PropFirmAccount", back_populates="live_trades")


# ── WAR ROOM MODULE ──────────────────────────────────────────

class Document(Base):
    """Doctrine documents — pasted or uploaded context for the AI."""
    __tablename__ = "documents"

    id          = Column(Integer, primary_key=True, index=True)
    title       = Column(String, nullable=False)
    content     = Column(Text, nullable=False)
    source_type = Column(String, default="paste")   # paste|upload
    created_at  = Column(DateTime, default=datetime.utcnow)
    updated_at  = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    chunks = relationship("DocumentChunk", back_populates="document",
                          cascade="all, delete-orphan", order_by="DocumentChunk.chunk_index")


class DocumentChunk(Base):
    """Fixed-size chunks derived from a Document, used for /context/search."""
    __tablename__ = "document_chunks"

    id          = Column(Integer, primary_key=True, index=True)
    document_id = Column(Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    content     = Column(Text, nullable=False)
    chunk_index = Column(Integer, nullable=False)
    created_at  = Column(DateTime, default=datetime.utcnow)

    document = relationship("Document", back_populates="chunks")


class NonNegotiable(Base):
    """User-editable list of daily non-negotiables (replaces hardcoded HABIT_DEFAULTS)."""
    __tablename__ = "non_negotiables"

    id         = Column(Integer, primary_key=True, index=True)
    key        = Column(String, nullable=False, unique=True)
    label      = Column(String, nullable=False)
    is_active  = Column(Boolean, default=True)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class ReadingPlan(Base):
    """A named reading plan (e.g. 'Bible 2026')."""
    __tablename__ = "reading_plans"

    id         = Column(Integer, primary_key=True, index=True)
    name       = Column(String, nullable=False)
    is_active  = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    entries = relationship("ReadingPlanEntry", back_populates="plan",
                           cascade="all, delete-orphan", order_by="ReadingPlanEntry.day_number")


class ReadingPlanEntry(Base):
    """One entry in a ReadingPlan (replaces BibleEntry for the plan page)."""
    __tablename__ = "reading_plan_entries"

    id         = Column(Integer, primary_key=True, index=True)
    plan_id    = Column(Integer, ForeignKey("reading_plans.id", ondelete="CASCADE"), nullable=False)
    ref        = Column(String, nullable=False)
    day_number = Column(Integer, nullable=False)
    done       = Column(Boolean, default=False)
    pushed     = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

    plan = relationship("ReadingPlan", back_populates="entries")