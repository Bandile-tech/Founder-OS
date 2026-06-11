import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base, Session

# ── URL resolution ────────────────────────────────────────────
# Production: set DATABASE_URL in environment (Render env var pointing
# at Supabase Postgres).  Render/Supabase may supply the legacy
# "postgres://" prefix; SQLAlchemy 2.x requires "postgresql://".
# Local dev: fall back to a file-based SQLite DB.

_raw_url = os.getenv("DATABASE_URL", "sqlite:///./founder_os.db")

# Defensive fix for legacy Render/Heroku postgres:// prefix
if _raw_url.startswith("postgres://"):
    _raw_url = _raw_url.replace("postgres://", "postgresql://", 1)

DATABASE_URL = _raw_url
_is_postgres = DATABASE_URL.startswith("postgresql")

# ── Engine ────────────────────────────────────────────────────
if _is_postgres:
    engine = create_engine(
        DATABASE_URL,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,  # discard stale connections
    )
else:
    # SQLite: no pool config, thread check disabled for FastAPI
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False},
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_all_todos(db):
    import models
    return db.query(models.Todo).all()
