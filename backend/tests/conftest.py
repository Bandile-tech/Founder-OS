"""
Shared pytest configuration for backend tests.

Creates a single in-memory SQLite database (StaticPool) shared across
all test files.  Sets the FastAPI dependency override once so that both
test_changes.py and test_academic.py use the same DB when run together.
"""

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

# ── App database: always a throwaway ─────────────────────────────────────────
# Must run before anything imports `database`/`main`: main runs create_all() at import
# and main.startup() (which a test calls) migrates and seeds through SessionLocal.
# Without this the app engine fell back to sqlite:///./founder_os.db (the developer's
# local data) or to whatever DATABASE_URL the shell or backend/.env held (production).
# DATABASE_URL is always overridden; a Postgres run reuses its throwaway TEST_DATABASE_URL.
if os.getenv("TEST_DATABASE_URL"):
    APP_TEST_DATABASE_URL = os.environ["TEST_DATABASE_URL"]
else:
    _app_db_dir = tempfile.mkdtemp(prefix="founder-os-tests-")

    def _remove_app_db():
        db_mod = sys.modules.get("database")
        if db_mod is not None:
            db_mod.engine.dispose()   # Windows will not delete a SQLite file that is still open
        shutil.rmtree(_app_db_dir, ignore_errors=True)

    atexit.register(_remove_app_db)
    APP_TEST_DATABASE_URL = "sqlite:///" + Path(_app_db_dir, "app.db").as_posix()
os.environ["DATABASE_URL"] = APP_TEST_DATABASE_URL

# ── The user's zone: this machine's ──────────────────────────────────────────
# The app takes "today" from FOUNDER_OS_TZ (default Africa/Lusaka); the tests build their
# fixtures from date.today(), which is this machine's day. On a UTC machine the two differ
# for two hours a night, so the suite runs with the app in the machine's own zone. A test
# about zones sets FOUNDER_OS_TZ itself.
try:
    import tzlocal
    os.environ["FOUNDER_OS_TZ"] = tzlocal.get_localzone_name() or "UTC"
except Exception:   # noqa: BLE001 — no zone name to be had: leave whatever is set
    os.environ.setdefault("FOUNDER_OS_TZ", "UTC")
if "database" in sys.modules or "main" in sys.modules:
    raise RuntimeError("tests/conftest.py must run before the app is imported")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi.testclient import TestClient

from database import Base, get_db
from main import app

# ── Test engine ──────────────────────────────────────────────────────────────
# Default: one shared in-memory SQLite database. Set TEST_DATABASE_URL to a Postgres
# URL to run the same suite against Postgres (every test still drops and recreates
# the schema, so point it at a throwaway database).
_TEST_URL = os.getenv("TEST_DATABASE_URL")
if _TEST_URL:
    # Short lock/statement timeouts so a test that leaves a transaction open fails fast
    # (with a clear error) instead of hanging the whole run on drop_all().
    engine = create_engine(
        _TEST_URL, pool_pre_ping=True,
        connect_args={"options": "-c lock_timeout=8000 -c statement_timeout=60000"},
    )
else:
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


def _clear_stale_pg_connections():
    """On Postgres a session a test forgot to close keeps a lock that makes drop_all() wait
    forever. Terminate every other connection of ours to the test database first.

    Only this role's own client connections: the database also holds backends that belong
    to the server (an autovacuum worker, say), and a role that is not a superuser may not
    terminate those, so asking for them fails the whole test."""
    if not _TEST_URL:
        return
    from sqlalchemy import text
    engine.dispose()
    with engine.connect() as conn:
        conn.execute(text(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid() "
            "AND usename = current_user AND backend_type = 'client backend'"
        ))
        conn.commit()


@pytest.fixture(autouse=True)
def fresh_db():
    """Drop and recreate all tables before each test for complete isolation."""
    _clear_stale_pg_connections()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    # Reset the 60s radar cache so a prior test's response can't leak across the
    # table reset.
    try:
        import main
        main._radar_cache.update({"key": None, "ts": 0.0, "payload": None})
    except Exception:
        pass
    yield


# ── No network, ever ─────────────────────────────────────────────────────────
# Tests must not reach OpenAI or any other host. A test that forgets to patch an HTTP
# seam fails here, loudly, instead of quietly making a request. Loopback stays open
# (the in-process test client does not use sockets; a local Postgres does).
_LOOPBACK = {"127.0.0.1", "::1", "localhost", "0.0.0.0", ""}


class NetworkAccessInTest(RuntimeError):
    pass


@pytest.fixture(autouse=True, scope="session")
def _no_network():
    import socket

    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo

    def guarded_connect(self, address):
        host = address[0] if isinstance(address, tuple) else None
        if isinstance(host, str) and host not in _LOOPBACK:
            raise NetworkAccessInTest(f"a test tried to connect to {host}")
        return real_connect(self, address)

    def guarded_getaddrinfo(host, *args, **kwargs):
        if isinstance(host, bytes):
            host = host.decode("ascii", "ignore")
        if isinstance(host, str) and host not in _LOOPBACK:
            raise NetworkAccessInTest(f"a test tried to resolve {host}")
        return real_getaddrinfo(host, *args, **kwargs)

    socket.socket.connect = guarded_connect
    socket.getaddrinfo = guarded_getaddrinfo
    yield
    socket.socket.connect = real_connect
    socket.getaddrinfo = real_getaddrinfo
