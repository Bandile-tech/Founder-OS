"""
Session C — tests for vault_sync.py and the query_cold_archive orchestrator tool.

All git operations are mocked — no real network calls.
Uses an in-memory SQLite database isolated per test.
"""

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime

import pytest

# ── Ensure backend/ is on the path ───────────────────────────
BACKEND = str(Path(__file__).parent.parent / "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)


# ── In-memory SQLite DB fixture ──────────────────────────────

@pytest.fixture()
def db():
    """Isolated in-memory SQLite session for each test."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from models import Base

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


# ── Helper: patch the engine dialect seen by search_vault ────

def _sqlite_engine_patch(monkeypatch):
    import database
    mock_engine = MagicMock()
    mock_engine.dialect.name = "sqlite"
    monkeypatch.setattr(database, "engine", mock_engine)


# ══════════════════════════════════════════════════════════════
# Test 1 — index_vault creates chunks for .md files
# ══════════════════════════════════════════════════════════════

def test_index_vault_creates_chunks(db, monkeypatch):
    import vault_sync

    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "note_one.md").write_text("Hello world from note one.", encoding="utf-8")
        (Path(tmpdir) / "note_two.md").write_text("Second note content here.", encoding="utf-8")

        monkeypatch.setattr(vault_sync, "VAULT_LOCAL_PATH", tmpdir)
        result = vault_sync.index_vault(db)

    assert result["files_processed"] == 2
    assert result["chunks_created"] >= 2

    from models import ColdArchiveChunk
    chunks = db.query(ColdArchiveChunk).all()
    titles = {c.file_title for c in chunks}
    assert "note_one" in titles
    assert "note_two" in titles


# ══════════════════════════════════════════════════════════════
# Test 2 — index_vault skips .obsidian/ folder
# ══════════════════════════════════════════════════════════════

def test_index_vault_skips_obsidian_folder(db, monkeypatch):
    import vault_sync

    with tempfile.TemporaryDirectory() as tmpdir:
        obsidian_dir = Path(tmpdir) / ".obsidian"
        obsidian_dir.mkdir()
        (obsidian_dir / "config.md").write_text("Obsidian config", encoding="utf-8")
        (Path(tmpdir) / "real_note.md").write_text("This is a real note.", encoding="utf-8")

        monkeypatch.setattr(vault_sync, "VAULT_LOCAL_PATH", tmpdir)
        result = vault_sync.index_vault(db)

    assert result["files_processed"] == 1

    from models import ColdArchiveChunk
    titles = {c.file_title for c in db.query(ColdArchiveChunk).all()}
    assert "config" not in titles
    assert "real_note" in titles


# ══════════════════════════════════════════════════════════════
# Test 3 — search_vault returns matches
# ══════════════════════════════════════════════════════════════

def test_search_vault_returns_matches(db, monkeypatch):
    import vault_sync
    from models import ColdArchiveChunk

    now = datetime.utcnow()
    db.add(ColdArchiveChunk(
        file_path="journal/2026-06.md",
        file_title="2026-06",
        content="Today I worked on the sprint training routine.",
        chunk_index=0,
        word_count=9,
        last_synced=now,
        created_at=now,
    ))
    db.commit()

    _sqlite_engine_patch(monkeypatch)
    results = vault_sync.search_vault("sprint", db)
    assert len(results) >= 1
    assert any("sprint" in r["content"].lower() for r in results)


# ══════════════════════════════════════════════════════════════
# Test 4 — title matches returned before body-only matches
# ══════════════════════════════════════════════════════════════

def test_search_vault_title_match_first(db, monkeypatch):
    import vault_sync
    from models import ColdArchiveChunk

    now = datetime.utcnow()
    db.add(ColdArchiveChunk(
        file_path="notes/proverbs.md",
        file_title="Proverbs",
        content="Wisdom is the principal thing.",
        chunk_index=0,
        word_count=5,
        last_synced=now,
        created_at=now,
    ))
    db.add(ColdArchiveChunk(
        file_path="notes/wisdom_notes.md",
        file_title="wisdom_notes",
        content="The book of Proverbs has great lessons.",
        chunk_index=0,
        word_count=7,
        last_synced=now,
        created_at=now,
    ))
    db.commit()

    _sqlite_engine_patch(monkeypatch)
    results = vault_sync.search_vault("proverbs", db)
    assert len(results) >= 2
    # Title-matched chunk ("proverbs" in file_title) must come first
    assert results[0]["file_title"].lower() == "proverbs"


# ══════════════════════════════════════════════════════════════
# Test 5 — case-insensitive search
# ══════════════════════════════════════════════════════════════

def test_search_vault_case_insensitive(db, monkeypatch):
    import vault_sync
    from models import ColdArchiveChunk

    now = datetime.utcnow()
    db.add(ColdArchiveChunk(
        file_path="bible/proverbs.md",
        file_title="Proverbs",
        content="Proverbs 3:5 — Trust in the LORD with all your heart.",
        chunk_index=0,
        word_count=12,
        last_synced=now,
        created_at=now,
    ))
    db.commit()

    _sqlite_engine_patch(monkeypatch)

    results = vault_sync.search_vault("proverbs", db)
    assert len(results) >= 1

    results_upper = vault_sync.search_vault("PROVERBS", db)
    assert len(results_upper) >= 1


# ══════════════════════════════════════════════════════════════
# Test 6 — query_cold_archive tool returns formatted results
# ══════════════════════════════════════════════════════════════

def test_query_cold_archive_tool(db, monkeypatch):
    import vault_sync
    from models import ColdArchiveChunk

    now = datetime.utcnow()
    db.add(ColdArchiveChunk(
        file_path="projects/aether.md",
        file_title="aether",
        content="Aether is my AI services business targeting $10,000 revenue.",
        chunk_index=0,
        word_count=10,
        last_synced=now,
        created_at=now,
    ))
    db.commit()

    _sqlite_engine_patch(monkeypatch)

    import orchestrator_tools
    result, summary = orchestrator_tools.query_cold_archive(db, query="aether")

    assert "results" in result
    assert len(result["results"]) >= 1
    r0 = result["results"][0]
    assert "file_title" in r0
    assert "aether" in r0["excerpt"].lower()
    assert "match" in summary.lower()


# ══════════════════════════════════════════════════════════════
# Test 7 — sync_vault is graceful when git fails (bad repo)
# ══════════════════════════════════════════════════════════════

def test_vault_sync_graceful_on_bad_repo(db, monkeypatch, tmp_path):
    import vault_sync

    # Ensure the clone path does NOT exist so we trigger the clone branch
    fake_path = str(tmp_path / "nonexistent-vault")
    monkeypatch.setattr(vault_sync, "VAULT_LOCAL_PATH", fake_path)
    monkeypatch.setattr(vault_sync, "VAULT_REPO_URL", "https://invalid.example.com/bad.git")

    # Mock subprocess.run to simulate git failure (returncode != 0)
    mock_result = MagicMock()
    mock_result.returncode = 128
    mock_result.stderr = "fatal: repository 'https://invalid.example.com/bad.git/' not found"
    mock_result.stdout = ""

    with patch("vault_sync.subprocess.run", return_value=mock_result):
        result = vault_sync.sync_vault(db)

    # Must not raise; must return a dict with error info
    assert isinstance(result, dict)
    assert result["files_processed"] == 0
    assert result["chunks_created"] == 0
    assert len(result["errors"]) >= 1
