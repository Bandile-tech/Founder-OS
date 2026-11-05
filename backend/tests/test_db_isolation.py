"""A test run must never touch a real database (backend/founder_os.db, or production).

conftest.py sets DATABASE_URL before the app is imported: the app engine (main's
create_all at import, and SessionLocal, which main.startup() uses) points at a throwaway
SQLite file in a temp directory, or at TEST_DATABASE_URL when a Postgres run sets it.
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import database
import tests.conftest as conftest

BACKEND = Path(__file__).parent.parent


def test_app_engine_uses_a_throwaway_database():
    url = database.DATABASE_URL
    assert url == os.environ["DATABASE_URL"] == conftest.APP_TEST_DATABASE_URL
    test_url = os.getenv("TEST_DATABASE_URL")
    if test_url:
        assert url == test_url
        return
    assert url.startswith("sqlite:///")
    db_file = Path(url[len("sqlite:///"):]).resolve()
    assert db_file != (BACKEND / "founder_os.db").resolve()
    assert db_file != (BACKEND.parent / "founder_os.db").resolve()
    assert db_file.parent.name.startswith("founder-os-tests-")
    assert db_file.parent.parent.resolve() == Path(tempfile.gettempdir()).resolve()


def test_session_local_is_bound_to_the_throwaway_database():
    assert str(database.SessionLocal.kw["bind"].url) == str(database.engine.url)
    assert database.engine.url.render_as_string(hide_password=False) == database.DATABASE_URL
