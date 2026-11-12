from sqlalchemy import Column, Integer, String, Date, Float, Text, DateTime, ForeignKey, Boolean, Index
from sqlalchemy.orm import relationship
from datetime import datetime
import os
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
    """Long-term memory. Legacy rows hold prompt/response pairs; rows written by the
    assistant's ``remember`` tool use context_type='memory' with the fact in
    ``context_data`` and ``kind`` = fact | decision | preference."""
    __tablename__ = "ai_memory"

    id = Column(Integer, primary_key=True, index=True)
    user = Column(String, default="Bandile")
    project = Column(String, default="founder_os")
    context_type = Column(String)
    context_data = Column(Text)
    response = Column(Text)
    kind = Column(String, nullable=True)
    tags = Column(String, nullable=True)
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
    due = Column(String)                      # legacy free-text due; kept for compatibility
    due_date = Column(Date, nullable=True)    # real date — overdue is computed server-side from this
    source = Column(String)
    roadmap_id = Column(String, nullable=True)
    completed_at = Column(Date, nullable=True)
    lead_id = Column(Integer, nullable=True)     # follow-up for a lead (category 'outreach')
    client_id = Column(Integer, nullable=True)   # task tied to a client


class ChatMessage(Base):
    """Server-side conversation history. ``thread_id`` is the conversation (one default
    thread shared across devices); ``session_id`` is the browser id kept for compatibility.
    Tool-calling turns persist ``tool_calls_json`` on the assistant row and
    ``tool_call_id`` on the following role='tool' rows."""
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String, nullable=False, index=True)
    thread_id = Column(String, nullable=True, index=True)
    role = Column(String, nullable=False)       # "user" | "assistant" | "tool"
    content = Column(Text, nullable=False)
    tool_calls_json = Column(Text, nullable=True)
    tool_call_id = Column(String, nullable=True)
    changes_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class ConversationSummary(Base):
    """Rolling summary of the turns that have fallen out of the verbatim history window."""
    __tablename__ = "conversation_summaries"

    id = Column(Integer, primary_key=True, index=True)
    thread_id = Column(String, nullable=False, unique=True)
    summary = Column(Text, nullable=False, default="")
    covers_through_message_id = Column(Integer, nullable=True)
    message_count = Column(Integer, default=0)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class LLMUsage(Base):
    """One row per OpenAI call — feeds GET /usage/summary."""
    __tablename__ = "llm_usage"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    model = Column(String, nullable=False)
    purpose = Column(String, nullable=True)
    input_tokens = Column(Integer, default=0)
    cached_input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    estimated_cost_usd = Column(Float, default=0.0)
    duration_ms = Column(Integer, nullable=True)
    agent_run_id = Column(Integer, nullable=True, index=True)   # the agent run that made the call


class BibleEntry(Base):
    """Daily Bible reading plan entries."""
    __tablename__ = "bible_entries"

    id = Column(Integer, primary_key=True, index=True)
    ref = Column(String, nullable=False)
    date = Column(Date, nullable=False)
    done = Column(Boolean, default=False)
    pushed = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class DailyBibleLog(Base):
    """Free-form daily Bible reading log (book + chapter)."""
    __tablename__ = "daily_bible_log"

    id         = Column(Integer, primary_key=True, index=True)
    date       = Column(Date, nullable=False, index=True)
    book       = Column(String, nullable=False)
    chapter    = Column(Integer, nullable=False)
    notes      = Column(Text, nullable=True)
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


def default_currency() -> str:
    """The currency a client is in when none is given: DEFAULT_CURRENCY (ZMW or USD),
    Kwacha when the variable is unset or names anything else."""
    cur = (os.getenv("DEFAULT_CURRENCY") or "ZMW").strip().upper()
    return cur if cur in ("ZMW", "USD") else "ZMW"


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
    lead_id = Column(Integer, nullable=True)          # the lead this client was converted from
    currency = Column(String, default=default_currency)   # USD | ZMW (DEFAULT_CURRENCY, else ZMW)
    tags = Column(String, nullable=True)              # comma-separated
    health = Column(String, default="green")          # green|amber|red, computed by crm.client_health


class Revenue(Base):
    """Individual revenue transactions."""
    __tablename__ = "revenue"

    id = Column(Integer, primary_key=True, index=True)
    amount = Column(Float, nullable=False)
    currency = Column(String, default="USD")   # USD | ZMW
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

    id                     = Column(Integer, primary_key=True, index=True)
    name                   = Column(String, nullable=False)
    is_active              = Column(Boolean, default=True)
    created_at             = Column(DateTime, default=datetime.utcnow)
    current_book           = Column(String, default="")
    current_chapter        = Column(Integer, default=1)
    daily_target_chapters  = Column(Integer, default=1)
    start_date             = Column(Date, nullable=True)
    target_completion_date = Column(Date, nullable=True)
    notes                  = Column(String, default="")

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


# ── COLD ARCHIVE (OBSIDIAN VAULT) ────────────────────────────

class ColdArchiveChunk(Base):
    """Chunks indexed from the Obsidian vault markdown files."""
    __tablename__ = "cold_archive_chunks"

    id          = Column(Integer, primary_key=True, index=True)
    file_path   = Column(String, nullable=False)   # relative path, e.g. "journal/2026-06.md"
    file_title  = Column(String, nullable=False)   # filename without extension
    content     = Column(Text, nullable=False)     # chunk text
    chunk_index = Column(Integer, nullable=False)
    word_count  = Column(Integer, nullable=True)
    last_synced = Column(DateTime, default=datetime.utcnow)
    created_at  = Column(DateTime, default=datetime.utcnow)


# ── MARKET INTELLIGENCE AGENT ────────────────────────────────

class ResearchProject(Base):
    """One row per research run of the market intelligence agent."""
    __tablename__ = "research_projects"

    id           = Column(Integer, primary_key=True, index=True)
    title        = Column(String, nullable=False)
    objective    = Column(Text, nullable=False)
    status       = Column(String, nullable=False, default="running")  # running|completed|failed
    error        = Column(Text, nullable=True)
    created_at   = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)
    cost_usd     = Column(Float, nullable=True)      # summed from llm_usage after the run
    llm_calls    = Column(Integer, nullable=True)
    objective_id = Column(Integer, nullable=True)    # research_objectives.id when a standing objective ran it

    findings = relationship("ResearchFinding", back_populates="project",
                            cascade="all, delete-orphan")


class ResearchFinding(Base):
    """A structured, evidence-backed opportunity surfaced by the agent.

    The agent only ever writes rows with status='surfaced'. Promotion into the
    opportunity pipeline is a separate explicit action, never a side effect.
    JSON payloads (discovery, market_analysis, founder_fit, evidence, scores)
    live in TEXT columns — identical on SQLite and Postgres.
    """
    __tablename__ = "research_findings"

    id                  = Column(Integer, primary_key=True, index=True)
    project_id          = Column(Integer, ForeignKey("research_projects.id", ondelete="CASCADE"),
                                 nullable=False, index=True)
    problem             = Column(Text, nullable=False)
    industry            = Column(String, nullable=True)
    customer_segment    = Column(String, nullable=True)
    persona             = Column(String, nullable=True)
    discovery           = Column(Text, nullable=True)   # JSON: pain, frequency, financial impact, workaround
    market_analysis     = Column(Text, nullable=True)   # JSON: existing solutions, gaps, trends
    founder_fit         = Column(Text, nullable=True)   # JSON: why solvable, difficulty, MVP, model
    evidence            = Column(Text, nullable=True)   # JSON array: {source_type, ref, title, excerpt}
    scores              = Column(Text, nullable=True)   # JSON: 7 axes /10 + overall
    overall_score       = Column(Float, nullable=True)
    first_customer_path = Column(Text, nullable=True)
    status              = Column(String, nullable=False, default="surfaced", index=True)
    notes               = Column(Text, nullable=True)
    created_at          = Column(DateTime, default=datetime.utcnow)
    recommendation      = Column(String, nullable=True)   # pursue|watch|park
    times_seen          = Column(Integer, default=1)
    dismissed_reason    = Column(Text, nullable=True)
    reviewed_at         = Column(DateTime, nullable=True)  # set on promote or dismiss

    project  = relationship("ResearchProject", back_populates="findings")
    pipeline = relationship("OpportunityPipeline", back_populates="finding", uselist=False,
                            cascade="all, delete-orphan")


class OpportunityPipeline(Base):
    """Explicitly promoted opportunities. One entry per finding, staged
    discovered → researching → validating → building → rejected."""
    __tablename__ = "opportunity_pipeline"

    id          = Column(Integer, primary_key=True, index=True)
    finding_id  = Column(Integer, ForeignKey("research_findings.id", ondelete="CASCADE"),
                         nullable=False, unique=True)
    stage       = Column(String, nullable=False, default="discovered", index=True)
    notes       = Column(Text, nullable=True)
    promoted_at = Column(DateTime, default=datetime.utcnow)
    updated_at  = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    next_action = Column(Text, nullable=True)
    due_date    = Column(Date, nullable=True)

    finding = relationship("ResearchFinding", back_populates="pipeline")


class ResearchObjective(Base):
    """A standing research objective that recurs every ``cadence_days``."""
    __tablename__ = "research_objectives"

    id                = Column(Integer, primary_key=True, index=True)
    objective         = Column(Text, nullable=False)
    cadence_days      = Column(Integer, nullable=False, default=14)
    enabled           = Column(Boolean, default=True)
    max_opportunities = Column(Integer, default=3)
    last_run_at       = Column(DateTime, nullable=True)
    last_project_id   = Column(Integer, nullable=True)
    created_at        = Column(DateTime, default=datetime.utcnow)
    updated_at        = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ResearchMemoryNote(Base):
    """The agent's lesson memory — one lesson per row.

    slug = the note's identity, summary = the one-line summary at the top,
    content = the markdown body. Upserted by slug so lessons update in place
    rather than duplicating. DB-backed (not files) because Render disk is ephemeral.
    """
    __tablename__ = "research_memory_notes"

    id               = Column(Integer, primary_key=True, index=True)
    slug             = Column(String, nullable=False, unique=True)
    summary          = Column(String, nullable=False)
    content          = Column(Text, nullable=False)
    times_reinforced = Column(Integer, default=1)
    created_at       = Column(DateTime, default=datetime.utcnow)
    updated_at       = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ── REBUILD: CHANGE LOG, CONFIRMATIONS, SUGGESTIONS, NOTES ───

class ChangeLog(Base):
    """Every write made by the assistant, the parser or a tool. One assistant turn is
    one batch. ``before_json``/``after_json`` are column snapshots used by undo."""
    __tablename__ = "change_log"

    id          = Column(Integer, primary_key=True, index=True)
    created_at  = Column(DateTime, default=datetime.utcnow, index=True)
    batch_id    = Column(String, nullable=False, index=True)
    source      = Column(String, nullable=False, default="assistant")  # assistant|parser|ui
    table_name  = Column(String, nullable=False)
    record_id   = Column(Integer, nullable=True)
    action      = Column(String, nullable=False)                        # create|update|delete
    before_json = Column(Text, nullable=True)
    after_json  = Column(Text, nullable=True)
    summary     = Column(String, nullable=True)
    undone_at   = Column(DateTime, nullable=True)


class PendingAction(Base):
    """A destructive action the assistant proposed and must not execute until the
    user explicitly confirms it (deletes)."""
    __tablename__ = "pending_actions"

    id           = Column(Integer, primary_key=True, index=True)
    created_at   = Column(DateTime, default=datetime.utcnow)
    thread_id    = Column(String, nullable=True)
    kind         = Column(String, nullable=False, default="delete")
    table_name   = Column(String, nullable=False)
    record_id    = Column(Integer, nullable=False)
    summary      = Column(String, nullable=True)
    payload_json = Column(Text, nullable=True)
    status       = Column(String, nullable=False, default="pending")  # pending|confirmed|cancelled|expired
    expires_at   = Column(DateTime, nullable=True)
    resolved_at  = Column(DateTime, nullable=True)
    batch_id     = Column(String, nullable=True)


class Suggestion(Base):
    """A low-confidence or ambiguous extraction. Nothing is written until accepted."""
    __tablename__ = "suggestions"

    id           = Column(Integer, primary_key=True, index=True)
    created_at   = Column(DateTime, default=datetime.utcnow)
    kind         = Column(String, nullable=False)       # todo_add|todo_complete|inbox_add|...
    payload_json = Column(Text, nullable=False)
    confidence   = Column(Float, nullable=True)
    reason       = Column(String, nullable=True)
    source_text  = Column(Text, nullable=True)
    status       = Column(String, nullable=False, default="pending")  # pending|accepted|dismissed
    source       = Column(String, nullable=True)        # parser|assistant
    batch_id     = Column(String, nullable=True)
    resolved_at  = Column(DateTime, nullable=True)


class Note(Base):
    """Free-form markdown notes: books, bible, journal, inbox, general."""
    __tablename__ = "notes"

    id         = Column(Integer, primary_key=True, index=True)
    title      = Column(String, nullable=False)
    kind       = Column(String, nullable=False, default="general")  # book|bible|journal|inbox|general
    body       = Column(Text, nullable=False, default="")
    tags       = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Embedding(Base):
    """Shared vector store for meaning-based retrieval across the vault, notes and
    long-term memory. Vectors are JSON in a TEXT column; similarity is computed in
    Python (the corpus is small; no pgvector). Keyed by content hash so re-indexing
    unchanged text never re-embeds it."""
    __tablename__ = "embeddings"

    id           = Column(Integer, primary_key=True, index=True)
    source_type  = Column(String, nullable=False, index=True)   # vault | note | memory
    source_id    = Column(Integer, nullable=False)
    chunk_index  = Column(Integer, default=0)
    content_hash = Column(String, nullable=False, index=True)
    model        = Column(String, nullable=False)
    vector_json  = Column(Text, nullable=False)
    updated_at   = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ExamDate(Base):
    """Exam dates per subject paper. ``paper`` NULL means the whole subject; a
    paper-level row wins over the subject-level row, which wins over Subject.exam_date."""
    __tablename__ = "exam_dates"

    id           = Column(Integer, primary_key=True, index=True)
    subject_code = Column(String, nullable=False)
    paper        = Column(String, nullable=True)
    exam_date    = Column(Date, nullable=False)
    updated_at   = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ── REBUILD: SYLLABUS + BRIEFING ─────────────────────────────

class SyllabusPoint(Base):
    """One syllabus topic per subject paper, mirroring the tracker sheet.
    status: not_started (red) → learned (yellow) → revised (blue) → bulletproof (green)."""
    __tablename__ = "syllabus_points"

    id               = Column(Integer, primary_key=True, index=True)
    subject_code     = Column(String, nullable=False, index=True)
    subject_name     = Column(String, nullable=False)
    level            = Column(String, nullable=True)        # AS | A2
    paper            = Column(String, nullable=True)        # P1, S1, P3 …
    topic_code       = Column(String, nullable=False)
    title            = Column(String, nullable=False)
    status           = Column(String, nullable=False, default="not_started")
    past_paper_score = Column(Float, nullable=True)
    last_revised     = Column(Date, nullable=True)
    weakness_notes   = Column(Text, nullable=True)
    updated_at       = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class BriefingSummary(Base):
    """The optional LLM summary for one day's briefing — at most one call per day."""
    __tablename__ = "briefing_summaries"

    id            = Column(Integer, primary_key=True, index=True)
    day           = Column(Date, nullable=False, unique=True)
    summary       = Column(Text, nullable=False, default="")
    greeting_line = Column(String, nullable=True)
    model         = Column(String, nullable=True)
    created_at    = Column(DateTime, default=datetime.utcnow)


# ── PHASE 5B: PROSPECTING AND OUTREACH ───────────────────────

class Lead(Base):
    """A prospect: a person or business Bandile intends to approach.

    status: new → qualified | disqualified → contacted → replied → converted | lost."""
    __tablename__ = "leads"

    id                  = Column(Integer, primary_key=True, index=True)
    name                = Column(String, nullable=False)
    company             = Column(String, nullable=True)
    role                = Column(String, nullable=True)
    email               = Column(String, nullable=True)     # lower-cased on write
    phone               = Column(String, nullable=True)     # digits and a leading '+' only
    whatsapp            = Column(String, nullable=True)     # same; defaults to phone
    linkedin_url        = Column(String, nullable=True)
    website             = Column(String, nullable=True)
    location            = Column(String, nullable=True)
    segment             = Column(String, nullable=True)     # free text, lower-cased
    source              = Column(String, nullable=False, default="manual")
    status              = Column(String, nullable=False, default="new")
    qualification_score = Column(Integer, nullable=True)    # 0-100
    qualification_json  = Column(Text, nullable=True)
    opportunity_id      = Column(Integer, nullable=True)    # research_findings.id
    client_id           = Column(Integer, nullable=True)    # clients.id, set on convert
    notes               = Column(Text, nullable=True)
    last_contact_at     = Column(DateTime, nullable=True)
    next_follow_up_at   = Column(Date, nullable=True)       # mirrors the open follow-up todo
    created_at          = Column(DateTime, default=datetime.utcnow)
    updated_at          = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class LeadImport(Base):
    """One row per CSV import of leads, with its counts and column mapping."""
    __tablename__ = "lead_imports"

    id           = Column(Integer, primary_key=True, index=True)
    filename     = Column(String, nullable=True)
    created      = Column(Integer, default=0)
    updated      = Column(Integer, default=0)
    skipped      = Column(Integer, default=0)
    total_rows   = Column(Integer, default=0)
    mapping_json = Column(Text, nullable=True)
    segment      = Column(String, nullable=True)
    created_at   = Column(DateTime, default=datetime.utcnow)


class OutreachMessage(Base):
    """A drafted, sent or received message for a lead. Nothing reaches `sent` without a
    human approval (a tap, or a confirmed pending action).

    status: draft → approved → sent | failed; cancelled; inbound rows are `received`."""
    __tablename__ = "outreach_messages"

    id                = Column(Integer, primary_key=True, index=True)
    lead_id           = Column(Integer, nullable=False)
    channel           = Column(String, nullable=False)       # whatsapp|email|linkedin|call|other
    direction         = Column(String, nullable=False, default="out")
    subject           = Column(String, nullable=True)        # email only
    body              = Column(Text, nullable=False)
    status            = Column(String, nullable=False, default="draft")
    version           = Column(Integer, default=1)
    supersedes_id     = Column(Integer, nullable=True)
    brief             = Column(Text, nullable=True)
    pending_action_id = Column(Integer, nullable=True)
    approved_at       = Column(DateTime, nullable=True)
    sent_at           = Column(DateTime, nullable=True)
    external_id       = Column(String, nullable=True)        # SMTP message-id
    error             = Column(Text, nullable=True)
    created_by        = Column(String, nullable=False, default="assistant")
    created_at        = Column(DateTime, default=datetime.utcnow)
    updated_at        = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    replied_at        = Column(DateTime, nullable=True)      # a reply to this message arrived by email


# ── PHASE 5C: CRM ────────────────────────────────────────────

class Contact(Base):
    """A person at a client."""
    __tablename__ = "contacts"

    id         = Column(Integer, primary_key=True, index=True)
    client_id  = Column(Integer, nullable=False)
    name       = Column(String, nullable=False)
    role       = Column(String, nullable=True)
    email      = Column(String, nullable=True)
    phone      = Column(String, nullable=True)
    whatsapp   = Column(String, nullable=True)
    is_primary = Column(Boolean, default=False)
    notes      = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Deal(Base):
    """Something being sold to a client. stage: lead → proposal → negotiation → won | lost."""
    __tablename__ = "deals"

    id             = Column(Integer, primary_key=True, index=True)
    client_id      = Column(Integer, nullable=False)
    title          = Column(String, nullable=False)
    stage          = Column(String, nullable=False, default="lead")
    amount         = Column(Float, nullable=True)
    currency       = Column(String, default="ZMW")
    probability    = Column(Integer, nullable=True)     # percent; defaults by stage
    expected_close = Column(Date, nullable=True)
    won_at         = Column(DateTime, nullable=True)
    lost_reason    = Column(Text, nullable=True)
    notes          = Column(Text, nullable=True)
    project_id     = Column(Integer, nullable=True)     # set when a won deal becomes a project
    created_at     = Column(DateTime, default=datetime.utcnow)
    updated_at     = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Project(Base):
    """Something being delivered: client work or an internal project (shared with the
    portfolio). status: planned | active | paused | delivered | closed | cancelled."""
    __tablename__ = "projects"

    id                = Column(Integer, primary_key=True, index=True)
    title             = Column(String, nullable=False)
    kind              = Column(String, nullable=False, default="client")   # client | internal
    client_id         = Column(Integer, nullable=True)
    deal_id           = Column(Integer, nullable=True)
    status            = Column(String, nullable=False, default="active")
    priority          = Column(Integer, default=3)       # 1 high … 5 low
    expected_value    = Column(Float, nullable=True)
    currency          = Column(String, default="ZMW")
    effort_hours_week = Column(Float, nullable=True)
    start_date        = Column(Date, nullable=True)
    due_date          = Column(Date, nullable=True)
    delivered_at      = Column(DateTime, nullable=True)
    next_action       = Column(Text, nullable=True)
    blocked_reason    = Column(Text, nullable=True)
    last_activity_at  = Column(DateTime, nullable=True)  # bumped by updates, communications, payments
    health            = Column(String, default="green")
    notes             = Column(Text, nullable=True)
    created_at        = Column(DateTime, default=datetime.utcnow)
    updated_at        = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Payment(Base):
    """Money a client owes or has paid. A paid payment always has one linked revenue row.
    status: due | paid | overdue | cancelled."""
    __tablename__ = "payments"

    id           = Column(Integer, primary_key=True, index=True)
    client_id    = Column(Integer, nullable=False)
    deal_id      = Column(Integer, nullable=True)
    project_id   = Column(Integer, nullable=True)
    title        = Column(String, nullable=True)
    amount       = Column(Float, nullable=False)
    currency     = Column(String, nullable=False, default="ZMW")
    status       = Column(String, nullable=False, default="due")
    due_date     = Column(Date, nullable=True)
    paid_at      = Column(Date, nullable=True)
    revenue_id   = Column(Integer, nullable=True)        # revenue.id, set when paid
    recurring_id = Column(Integer, nullable=True)        # set for generated instalments
    period_start = Column(Date, nullable=True)
    notes        = Column(Text, nullable=True)
    created_at   = Column(DateTime, default=datetime.utcnow)
    updated_at   = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class RecurringRevenue(Base):
    """A retainer or subscription that generates a due payment each period."""
    __tablename__ = "recurring_revenue"

    id           = Column(Integer, primary_key=True, index=True)
    client_id    = Column(Integer, nullable=False)
    title        = Column(String, nullable=False)
    amount       = Column(Float, nullable=False)
    currency     = Column(String, nullable=False, default="ZMW")
    interval     = Column(String, nullable=False, default="monthly")   # monthly|quarterly|annual
    day_of_month = Column(Integer, default=1)
    next_due     = Column(Date, nullable=False)
    active       = Column(Boolean, default=True)
    ended_at     = Column(Date, nullable=True)
    created_at   = Column(DateTime, default=datetime.utcnow)
    updated_at   = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Communication(Base):
    """One conversation with a client, contact or lead, as a line or two."""
    __tablename__ = "communications"

    id                  = Column(Integer, primary_key=True, index=True)
    client_id           = Column(Integer, nullable=True)
    contact_id          = Column(Integer, nullable=True)
    lead_id             = Column(Integer, nullable=True)
    channel             = Column(String, nullable=False)     # whatsapp|email|call|meeting|linkedin|other
    direction           = Column(String, nullable=False)     # out | in
    summary             = Column(Text, nullable=False)
    occurred_at         = Column(DateTime, nullable=False, default=datetime.utcnow)
    outreach_message_id = Column(Integer, nullable=True)
    source              = Column(String, default="conversation")
    created_at          = Column(DateTime, default=datetime.utcnow)


# ── PHASE 5E: PROJECT PORTFOLIO ──────────────────────────────

class ProjectUpdate(Base):
    """A dated line on a project: a note, a milestone, a blocker, an unblock or a status change."""
    __tablename__ = "project_updates"

    id          = Column(Integer, primary_key=True, index=True)
    project_id  = Column(Integer, nullable=False)
    kind        = Column(String, nullable=False, default="note")   # note|milestone|blocker|unblocked|status
    text        = Column(Text, nullable=False)
    occurred_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    source      = Column(String, default="conversation")
    created_at  = Column(DateTime, default=datetime.utcnow)


# ── PHASE 6B: AGENTS ─────────────────────────────────────────

class Agent(Base):
    """One row per registered agent. The registry in code is the source of truth; this
    table holds the toggle, schedule overrides and last-run facts."""
    __tablename__ = "agents"

    id               = Column(Integer, primary_key=True, index=True)
    key              = Column(String, nullable=False, unique=True)
    enabled          = Column(Boolean, nullable=False, default=True)
    interval_minutes = Column(Integer, nullable=True)      # overrides, nullable
    at_time          = Column(String, nullable=True)
    weekdays_json    = Column(Text, nullable=True)
    config_json      = Column(Text, nullable=True)
    last_run_at      = Column(DateTime, nullable=True)
    last_status      = Column(String, nullable=True)
    last_summary     = Column(String, nullable=True)
    runs_count       = Column(Integer, default=0)
    cost_usd_total   = Column(Float, default=0.0)
    created_at       = Column(DateTime, default=datetime.utcnow)
    updated_at       = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AgentRun(Base):
    """The activity log: one row per run. status: running|completed|skipped|failed|violation."""
    __tablename__ = "agent_runs"

    id            = Column(Integer, primary_key=True, index=True)
    agent_key     = Column(String, nullable=False)
    trigger       = Column(String, nullable=False)          # schedule|event|manual|in_process
    tick_id       = Column(Integer, nullable=True)
    started_at    = Column(DateTime, nullable=False, default=datetime.utcnow)
    finished_at   = Column(DateTime, nullable=True)
    status        = Column(String, nullable=False, default="running")
    summary       = Column(String, nullable=True)
    result_json   = Column(Text, nullable=True)
    error         = Column(Text, nullable=True)
    log_text      = Column(Text, nullable=True)
    llm_calls     = Column(Integer, default=0)
    cost_usd      = Column(Float, default=0.0)
    changes_count = Column(Integer, default=0)
    batch_id      = Column(String, nullable=True)
    over_time     = Column(Boolean, default=False)


class AgentEvent(Base):
    """Something happened that an agent may react to on the next tick."""
    __tablename__ = "agent_events"

    id           = Column(Integer, primary_key=True, index=True)
    kind         = Column(String, nullable=False)
    payload_json = Column(Text, nullable=True)
    created_at   = Column(DateTime, nullable=False, default=datetime.utcnow)
    consumed_at  = Column(DateTime, nullable=True)
    dropped      = Column(Boolean, default=False)
    source       = Column(String, nullable=True)


class MigrationMark(Base):
    """A data migration that must run once, not on every startup, leaves its key here
    (``migrations._helpers.claim_once``)."""
    __tablename__ = "migration_marks"

    key        = Column(String, primary_key=True)
    applied_at = Column(DateTime, nullable=True)


class JobTick(Base):
    """One row per tick. trigger: github|manual|in_process."""
    __tablename__ = "job_ticks"

    id              = Column(Integer, primary_key=True, index=True)
    trigger         = Column(String, nullable=False)
    started_at      = Column(DateTime, nullable=True)
    finished_at     = Column(DateTime, nullable=True)
    agents_run      = Column(Integer, nullable=True)
    agents_skipped  = Column(Integer, nullable=True)
    events_consumed = Column(Integer, nullable=True)
    errors          = Column(Integer, nullable=True)
    duration_ms     = Column(Integer, nullable=True)
    summary_json    = Column(Text, nullable=True)


# ── PHASE 6A: APPLICATIONS ───────────────────────────────────

class Application(Base):
    """A long-horizon application. ``deadline_source`` says where the deadline came from:
    user (Bandile stated it), document (read from text he pasted) or unknown (no deadline)."""
    __tablename__ = "applications"

    id                      = Column(Integer, primary_key=True, index=True)
    name                    = Column(String, nullable=False)
    institution             = Column(String, nullable=True)
    programme               = Column(String, nullable=True)
    kind                    = Column(String, nullable=False, default="university")
    deadline                = Column(Date, nullable=True)
    deadline_source         = Column(String, nullable=True)     # user | document | unknown
    decision_date           = Column(Date, nullable=True)
    status                  = Column(String, nullable=False, default="researching")
    priority                = Column(Integer, default=2)        # 1 high … 3 low
    portal_url              = Column(String, nullable=True)
    notes                   = Column(Text, nullable=True)
    requirements_updated_at = Column(DateTime, nullable=True)   # cache control for the gap review
    gaps_reviewed_at        = Column(DateTime, nullable=True)
    created_at              = Column(DateTime, default=datetime.utcnow)
    updated_at              = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ApplicationRequirement(Base):
    """One item of an application's checklist: missing → in_progress → ready → submitted,
    or not_required."""
    __tablename__ = "application_requirements"

    id             = Column(Integer, primary_key=True, index=True)
    application_id = Column(Integer, nullable=False)
    title          = Column(String, nullable=False)
    kind           = Column(String, nullable=False, default="other")
    status         = Column(String, nullable=False, default="missing")
    due_date       = Column(Date, nullable=True)
    due_source     = Column(String, nullable=True)             # user | document
    details        = Column(Text, nullable=True)
    todo_id        = Column(Integer, nullable=True)
    note_id        = Column(Integer, nullable=True)
    sort_order     = Column(Integer, default=0)
    created_at     = Column(DateTime, default=datetime.utcnow)
    updated_at     = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ApplicationGap(Base):
    """Something weak in the profile relative to an application, from a review.
    status: open | addressed | accepted_risk | superseded (replaced by a newer review)."""
    __tablename__ = "application_gaps"

    id               = Column(Integer, primary_key=True, index=True)
    application_id   = Column(Integer, nullable=False)
    gap              = Column(String, nullable=False)
    severity         = Column(String, nullable=False)          # high | medium | low
    evidence         = Column(Text, nullable=True)
    suggested_action = Column(Text, nullable=True)
    status           = Column(String, nullable=False, default="open")
    review_batch     = Column(String, nullable=True)
    created_at       = Column(DateTime, default=datetime.utcnow)
    updated_at       = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ── PHASE 5D: PERSONAL-BRAND ANALYTICS ───────────────────────
# Real platform data only: a metric the platform does not give is NULL, never 0.

class SocialAccount(Base):
    """A connected platform account. access: api_key | export | oauth."""
    __tablename__ = "social_accounts"

    id             = Column(Integer, primary_key=True, index=True)
    platform       = Column(String, nullable=False)        # youtube|linkedin|x|instagram|tiktok
    handle         = Column(String, nullable=True)
    external_id    = Column(String, nullable=True)         # e.g. the YouTube channel id
    access         = Column(String, nullable=False, default="api_key")
    config_json    = Column(Text, nullable=True)           # adapter config; never a secret
    status         = Column(String, nullable=False, default="connected")   # connected|error|disconnected
    last_synced_at = Column(DateTime, nullable=True)
    last_error     = Column(Text, nullable=True)
    created_at     = Column(DateTime, default=datetime.utcnow)
    updated_at     = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ContentPost(Base):
    """A post or video on a platform. kind: video|short|post|article|thread."""
    __tablename__ = "content_posts"
    __table_args__ = (Index("ux_content_posts_account_external", "account_id", "external_id", unique=True),)

    id          = Column(Integer, primary_key=True, index=True)
    account_id  = Column(Integer, nullable=False)
    external_id = Column(String, nullable=False)
    url         = Column(String, nullable=True)
    title       = Column(String, nullable=True)
    kind        = Column(String, nullable=True)
    posted_at   = Column(DateTime, nullable=True)
    topic_tags  = Column(String, nullable=True)            # comma list
    idea_id     = Column(Integer, nullable=True)           # content_ideas.id
    created_at  = Column(DateTime, default=datetime.utcnow)
    updated_at  = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ContentMetric(Base):
    """An append-only snapshot of a post's numbers. source: api | export."""
    __tablename__ = "content_metrics"
    __table_args__ = (Index("ix_content_metrics_post_captured", "post_id", "captured_at"),)

    id           = Column(Integer, primary_key=True, index=True)
    post_id      = Column(Integer, nullable=False)
    captured_at  = Column(DateTime, nullable=False, default=datetime.utcnow)
    views        = Column(Integer, nullable=True)
    likes        = Column(Integer, nullable=True)
    comments     = Column(Integer, nullable=True)
    shares       = Column(Integer, nullable=True)
    saves        = Column(Integer, nullable=True)
    impressions  = Column(Integer, nullable=True)
    watch_time_s = Column(Integer, nullable=True)
    source       = Column(String, nullable=True)
    raw_json     = Column(Text, nullable=True)


class AccountMetric(Base):
    """An append-only snapshot of an account's numbers."""
    __tablename__ = "account_metrics"

    id          = Column(Integer, primary_key=True, index=True)
    account_id  = Column(Integer, nullable=False)
    captured_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    followers   = Column(Integer, nullable=True)
    following   = Column(Integer, nullable=True)
    posts_count = Column(Integer, nullable=True)
    total_views = Column(Integer, nullable=True)
    source      = Column(String, nullable=True)
    raw_json    = Column(Text, nullable=True)


class ContentIdea(Base):
    """The idea backlog and the content calendar. status: idea → drafted → scheduled →
    posted | dropped."""
    __tablename__ = "content_ideas"
    __table_args__ = (Index("ix_content_ideas_status_scheduled", "status", "scheduled_for"),)

    id             = Column(Integer, primary_key=True, index=True)
    title          = Column(String, nullable=False)
    angle          = Column(Text, nullable=True)
    platform       = Column(String, nullable=True)         # null = any
    format         = Column(String, nullable=True)         # short|video|post|article|thread
    status         = Column(String, nullable=False, default="idea")
    scheduled_for  = Column(Date, nullable=True)
    posted_post_id = Column(Integer, nullable=True)        # content_posts.id when linked
    source         = Column(String, default="conversation")
    score          = Column(Integer, nullable=True)        # optional 1-5
    created_at     = Column(DateTime, default=datetime.utcnow)
    updated_at     = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class BrandInsight(Base):
    """Cached weekly insight text: one row per platform per ISO week."""
    __tablename__ = "brand_insights"
    __table_args__ = (Index("ux_brand_insights_week_platform", "week_start", "platform", unique=True),)

    id         = Column(Integer, primary_key=True, index=True)
    week_start = Column(Date, nullable=False)
    platform   = Column(String, nullable=False)
    text       = Column(Text, nullable=False)
    model      = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


# ── PHASE 6C: INTEGRATIONS ───────────────────────────────────

class Integration(Base):
    """One row per external provider. ``secret_json`` holds tokens the provider issued at
    runtime; it is never serialised by any endpoint."""
    __tablename__ = "integrations"

    id            = Column(Integer, primary_key=True, index=True)
    provider      = Column(String, nullable=False, unique=True)   # google_calendar|email|telegram|youtube
    status        = Column(String, nullable=False, default="disconnected")   # disconnected|connected|error|pending
    account_label = Column(String, nullable=True)
    config_json   = Column(Text, nullable=True)            # non-secret settings
    secret_json   = Column(Text, nullable=True)            # tokens — never returned
    scopes        = Column(String, nullable=True)
    connected_at  = Column(DateTime, nullable=True)
    last_sync_at  = Column(DateTime, nullable=True)
    last_error    = Column(Text, nullable=True)
    created_at    = Column(DateTime, default=datetime.utcnow)
    updated_at    = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CalendarEvent(Base):
    """A cached calendar event. Times are naive UTC; ``start_date`` is the local date."""
    __tablename__ = "calendar_events"
    __table_args__ = (Index("ux_calendar_events_provider_external", "provider", "external_id", unique=True),)

    id                = Column(Integer, primary_key=True, index=True)
    provider          = Column(String, default="google")
    calendar_id       = Column(String, nullable=True)
    external_id       = Column(String, nullable=False)
    title             = Column(String, nullable=True)
    description       = Column(Text, nullable=True)        # at most 1,000 characters
    location          = Column(String, nullable=True)
    start_at          = Column(DateTime, nullable=True)
    end_at            = Column(DateTime, nullable=True)
    all_day           = Column(Boolean, default=False)
    start_date        = Column(Date, nullable=True, index=True)
    status            = Column(String, default="confirmed")
    html_link         = Column(String, nullable=True)
    created_by_us     = Column(Boolean, default=False)     # events Founder OS created
    remote_updated_at = Column(DateTime, nullable=True)
    synced_at         = Column(DateTime, nullable=True)
    deleted_at        = Column(DateTime, nullable=True)    # soft delete when gone upstream


class EmailMessage(Base):
    """An email from or to a known lead, contact or client: headers and a snippet of at
    most 500 characters. Bodies are never stored; mail from unknown senders is never stored."""
    __tablename__ = "email_messages"

    id                  = Column(Integer, primary_key=True, index=True)
    message_id          = Column(String, nullable=False, unique=True)
    thread_key          = Column(String, nullable=True)
    direction           = Column(String, nullable=False)   # in | out
    from_addr           = Column(String, nullable=True)
    to_addr             = Column(String, nullable=True)
    subject             = Column(String, nullable=True)
    snippet             = Column(Text, nullable=True)
    received_at         = Column(DateTime, nullable=True, index=True)
    lead_id             = Column(Integer, nullable=True)
    client_id           = Column(Integer, nullable=True)
    contact_id          = Column(Integer, nullable=True)
    outreach_message_id = Column(Integer, nullable=True)
    processed_at        = Column(DateTime, nullable=True)
    created_at          = Column(DateTime, default=datetime.utcnow)


# ── PHASE 6D: NOTIFICATIONS ──────────────────────────────────

class Notification(Base):
    """One message to Bandile: the outbox row and its outcome. Nothing sends directly;
    ``notify.dispatch`` decides each queued row."""
    __tablename__ = "notifications"

    id                = Column(Integer, primary_key=True, index=True)
    created_at        = Column(DateTime, default=datetime.utcnow)
    channel           = Column(String, nullable=False, default="telegram")
    kind              = Column(String, nullable=False)     # briefing|evening|alert|deadline|reminder|reply|test
    title             = Column(String, nullable=True)
    body              = Column(Text, nullable=False)       # plain text
    dedupe_key        = Column(String, nullable=True, unique=True)
    send_at           = Column(DateTime, nullable=True)    # naive UTC; null = now
    status            = Column(String, nullable=False, default="queued")   # queued|sent|failed|suppressed|cancelled
    sent_at           = Column(DateTime, nullable=True)
    error             = Column(Text, nullable=True)
    external_id       = Column(String, nullable=True)
    agent_run_id      = Column(Integer, nullable=True)
    source            = Column(String, nullable=True)
    suppressed_reason = Column(String, nullable=True)      # quiet_hours|daily_cap|channel_disabled|not_paired


class NotificationSettings(Base):
    """Exactly one row (id=1). Times are local, "HH:MM"."""
    __tablename__ = "notification_settings"

    id                 = Column(Integer, primary_key=True)
    enabled            = Column(Boolean, default=True)
    morning_time       = Column(String, default="06:00")
    evening_time       = Column(String, nullable=True)     # null = no evening review
    quiet_start        = Column(String, default="22:00")
    quiet_end          = Column(String, default="05:00")
    alert_min_severity = Column(String, default="warn")    # info|warn|critical
    daily_cap          = Column(Integer, default=12)
    channels_json      = Column(Text, nullable=True)       # {"telegram": true}
    deadline_days_json = Column(Text, nullable=True)       # [30, 14, 7, 3, 1]
    muted_until        = Column(DateTime, nullable=True)   # naive UTC; /mute sets it
    updated_at         = Column(DateTime, default=datetime.utcnow)
