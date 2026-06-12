"""
Tests for Feature 1: DailyBibleLog endpoints and brain dump integration.
"""
import sys
from pathlib import Path
from datetime import date

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.conftest import client
from models import ReadingPlan, DailyBibleLog
from tests.conftest import TestSessionLocal


# ── Endpoint tests ────────────────────────────────────────────────────────────

class TestBibleLogEndpoints:
    def test_create_entry(self):
        resp = client.post("/bible-log", json={"book": "Proverbs", "chapter": 12})
        assert resp.status_code == 201
        data = resp.json()
        assert data["book"] == "Proverbs"
        assert data["chapter"] == 12
        assert data["notes"] is None
        assert data["date"] == str(date.today())

    def test_create_entry_with_notes(self):
        resp = client.post("/bible-log", json={"book": "Matthew", "chapter": 5, "notes": "Sermon on the Mount"})
        assert resp.status_code == 201
        assert resp.json()["notes"] == "Sermon on the Mount"

    def test_create_with_explicit_date(self):
        resp = client.post("/bible-log", json={"book": "Genesis", "chapter": 1, "entry_date": "2026-01-01"})
        assert resp.status_code == 201
        assert resp.json()["date"] == "2026-01-01"

    def test_get_today(self):
        client.post("/bible-log", json={"book": "Proverbs", "chapter": 12})
        client.post("/bible-log", json={"book": "Matthew", "chapter": 5})
        resp = client.get("/bible-log/today")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_get_today_excludes_other_dates(self):
        client.post("/bible-log", json={"book": "Genesis", "chapter": 1, "entry_date": "2020-01-01"})
        client.post("/bible-log", json={"book": "Proverbs", "chapter": 12})
        resp = client.get("/bible-log/today")
        assert len(resp.json()) == 1
        assert resp.json()[0]["book"] == "Proverbs"

    def test_get_recent_grouped_by_date(self):
        client.post("/bible-log", json={"book": "Proverbs", "chapter": 12})
        client.post("/bible-log", json={"book": "Matthew", "chapter": 5})
        resp = client.get("/bible-log/recent?days=30")
        assert resp.status_code == 200
        groups = resp.json()
        assert len(groups) == 1  # both on same day (today)
        assert len(groups[0]["entries"]) == 2

    def test_delete_entry(self):
        resp = client.post("/bible-log", json={"book": "Proverbs", "chapter": 12})
        entry_id = resp.json()["id"]
        del_resp = client.delete(f"/bible-log/{entry_id}")
        assert del_resp.status_code == 204
        today_resp = client.get("/bible-log/today")
        assert len(today_resp.json()) == 0

    def test_delete_nonexistent_returns_404(self):
        resp = client.delete("/bible-log/99999")
        assert resp.status_code == 404


# ── Brain dump integration tests ──────────────────────────────────────────────

class TestBibleLogBrainDump:
    """
    These tests use /parse with a mocked parse response to test
    apply_parse_updates() logic without hitting OpenAI.
    """

    def _seed_parse(self, parsed: dict):
        """Directly call apply_parse_updates via the internal helper."""
        from main import apply_parse_updates
        db = TestSessionLocal()
        try:
            result = apply_parse_updates(db, parsed, date.today())
            return result
        finally:
            db.close()

    def test_bible_log_creates_row(self):
        result = self._seed_parse({"bible_log": [{"book": "Proverbs", "chapter": 12, "notes": None}]})
        assert result["bible_log_entries_created"] == 1
        db = TestSessionLocal()
        try:
            rows = db.query(DailyBibleLog).all()
            assert len(rows) == 1
            assert rows[0].book == "Proverbs"
            assert rows[0].chapter == 12
        finally:
            db.close()

    def test_bible_log_does_not_affect_book_queue(self):
        """Regression: bible log must NOT touch reading queue books."""
        from models import Book
        db = TestSessionLocal()
        try:
            db.add(Book(title="Proverbs commentary", status="queue", position=1))
            db.commit()
        finally:
            db.close()
        result = self._seed_parse({"bible_log": [{"book": "Proverbs", "chapter": 12, "notes": None}]})
        assert result["bible_log_entries_created"] == 1
        # reading_updates_applied must be empty — no Book was touched
        assert result["reading_updates_applied"] == []

    def test_bible_log_does_not_create_todo(self):
        """Regression: bible log must NOT create a todo."""
        result = self._seed_parse({
            "bible_log": [{"book": "Proverbs", "chapter": 12, "notes": None}],
            "todos_add": [],
        })
        assert result["todos_added"] == []

    def test_bible_log_advances_matching_plan(self):
        """With active plan current_book=Proverbs → chapter updated to 12."""
        db = TestSessionLocal()
        try:
            db.add(ReadingPlan(name="Bible 2026", current_book="Proverbs", current_chapter=10, is_active=True))
            db.commit()
        finally:
            db.close()

        self._seed_parse({"bible_log": [{"book": "Proverbs", "chapter": 12, "notes": None}]})

        db = TestSessionLocal()
        try:
            plan = db.query(ReadingPlan).filter_by(name="Bible 2026").first()
            assert plan.current_chapter == 12
        finally:
            db.close()

    def test_bible_log_does_not_advance_non_matching_plan(self):
        """With active plan current_book=Matthew → plan NOT updated."""
        db = TestSessionLocal()
        try:
            db.add(ReadingPlan(name="NT Plan", current_book="Matthew", current_chapter=3, is_active=True))
            db.commit()
        finally:
            db.close()

        self._seed_parse({"bible_log": [{"book": "Proverbs", "chapter": 12, "notes": None}]})

        db = TestSessionLocal()
        try:
            plan = db.query(ReadingPlan).filter_by(name="NT Plan").first()
            assert plan.current_chapter == 3  # unchanged
        finally:
            db.close()

    def test_bible_log_case_insensitive_plan_match(self):
        """Plan match is case-insensitive: current_book='proverbs' matches 'Proverbs'."""
        db = TestSessionLocal()
        try:
            db.add(ReadingPlan(name="Plan", current_book="proverbs", current_chapter=1, is_active=True))
            db.commit()
        finally:
            db.close()

        self._seed_parse({"bible_log": [{"book": "Proverbs", "chapter": 5, "notes": None}]})

        db = TestSessionLocal()
        try:
            plan = db.query(ReadingPlan).filter_by(name="Plan").first()
            assert plan.current_chapter == 5
        finally:
            db.close()

    def test_bible_log_only_matches_active_plans(self):
        """Inactive plan with matching book must NOT be updated."""
        db = TestSessionLocal()
        try:
            db.add(ReadingPlan(name="Old Plan", current_book="Proverbs", current_chapter=1, is_active=False))
            db.commit()
        finally:
            db.close()

        self._seed_parse({"bible_log": [{"book": "Proverbs", "chapter": 20, "notes": None}]})

        db = TestSessionLocal()
        try:
            plan = db.query(ReadingPlan).filter_by(name="Old Plan").first()
            assert plan.current_chapter == 1  # unchanged
        finally:
            db.close()
