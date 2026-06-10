"""
Shared pytest configuration for backend tests.

Creates a single in-memory SQLite database (StaticPool) shared across
all test files.  Sets the FastAPI dependency override once so that both
test_changes.py and test_academic.py use the same DB when run together.
"""

import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi.testclient import TestClient

from database import Base, get_db
from main import app

# ── Single shared in-memory engine ───────────────────────────────────────────
engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Create all tables once (both legacy and new academic tables)
Base.metadata.create_all(bind=engine)


def get_test_db():
    db = TestSessionLocal()
    try:
        yield db
    finally:
        db.close()


# Wire the override at import time so it's active for every test regardless
# of which file is collected first.
app.dependency_overrides[get_db] = get_test_db

# Single shared client — startup fires on first request (no context manager).
client = TestClient(app, raise_server_exceptions=True)
# Trigger startup eagerly before any test's fresh_db has a chance to run.
try:
    client.get("/habits")
except Exception:
    pass


@pytest.fixture(autouse=True)
def fresh_db():
    """Drop and recreate all tables before each test for complete isolation."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
