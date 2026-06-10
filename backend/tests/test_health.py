"""
pytest suite for Phase 3 — Health Module.

Covers:
  - DailyHealth CRUD: create via GET today, read, PATCH
  - WeeklyHealth CRUD: create via GET current, read, PATCH
  - GET /health/daily/today creates row if missing
  - GET /health/weekly/current creates row if missing
  - PATCH updates only non-null fields (partial update)
  - GET /health/daily/recent returns correct window
  - GET /health/weekly/history returns correct window
  - GET /health/lifts/progression: best-by-weight tie-break by reps
  - apply_parse_updates() with health_updates populates DailyHealth
  - Radar GET /radar returns "health" key (not "physical")
"""

import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import models
from main import app, _current_week_start
from tests.conftest import TestSessionLocal, client


def _db():
    return TestSessionLocal()


def _fake_parse_response(overrides: dict) -> dict:
    base = {
        "summary": "test",
        "kpi_updates": [],
        "todos_add": [],
        "todos_complete": [],
        "roadmap_complete": [],
        "habits_done": [],
        "annual_updates": [],
        "revenue_updates": [],
        "log_entry": "test log",
        "advisory": None,
    }
    base.update(overrides)
    return base


# ════════════════════════════════════════════════════════════════
# 1. GET /health/daily/today — creates row if missing
# ════════════════════════════════════════════════════════════════

class TestDailyHealthToday:

    def test_get_today_creates_row(self):
        db = _db()
        assert db.query(models.DailyHealth).count() == 0
        db.close()

        resp = client.get("/health/daily/today")
        assert resp.status_code == 200
        data = resp.json()
        assert data["date"] == str(date.today())
        assert data["sleep_hours"] is None
        assert data["mobility_done"] is False
        assert data["session_done"] is False

        db = _db()
        assert db.query(models.DailyHealth).count() == 1
        db.close()

    def test_get_today_idempotent(self):
        client.get("/health/daily/today")
        client.get("/health/daily/today")

        db = _db()
        assert db.query(models.DailyHealth).count() == 1
        db.close()


# ════════════════════════════════════════════════════════════════
# 2. PATCH /health/daily/{date}
# ════════════════════════════════════════════════════════════════

class TestDailyHealthPatch:

    def test_patch_sleep_hours(self):
        client.get("/health/daily/today")
        today = str(date.today())
        resp = client.patch(f"/health/daily/{today}", json={"sleep_hours": 7.5})
        assert resp.status_code == 200
        assert resp.json()["sleep_hours"] == 7.5

    def test_patch_partial_leaves_other_fields(self):
        client.get("/health/daily/today")
        today = str(date.today())
        client.patch(f"/health/daily/{today}", json={"sleep_hours": 8.0, "mobility_done": True})
        resp = client.patch(f"/health/daily/{today}", json={"session_done": True})
        data = resp.json()
        # Previously set fields are unchanged
        assert data["sleep_hours"] == 8.0
        assert data["mobility_done"] is True
        # New field applied
        assert data["session_done"] is True

    def test_patch_lift_fields(self):
        client.get("/health/daily/today")
        today = str(date.today())
        resp = client.patch(f"/health/daily/{today}", json={
            "session_done": True,
            "main_lift": "Bench Press",
            "top_set_weight": 80.0,
            "top_set_reps": 5,
        })
        data = resp.json()
        assert data["main_lift"] == "Bench Press"
        assert data["top_set_weight"] == 80.0
        assert data["top_set_reps"] == 5

    def test_patch_creates_row_if_missing(self):
        yesterday = str(date.today() - timedelta(days=1))
        resp = client.patch(f"/health/daily/{yesterday}", json={"sleep_hours": 6.0})
        assert resp.status_code == 200
        assert resp.json()["sleep_hours"] == 6.0


# ════════════════════════════════════════════════════════════════
# 3. GET /health/daily/recent
# ════════════════════════════════════════════════════════════════

class TestDailyHealthRecent:

    def test_recent_returns_last_n_days(self):
        today = date.today()
        db = _db()
        for i in range(5):
            db.add(models.DailyHealth(date=today - timedelta(days=i)))
        db.commit()
        db.close()

        resp = client.get("/health/daily/recent?days=3")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 3

    def test_recent_ordered_newest_first(self):
        today = date.today()
        db = _db()
        for i in range(3):
            db.add(models.DailyHealth(date=today - timedelta(days=i)))
        db.commit()
        db.close()

        resp = client.get("/health/daily/recent?days=14")
        dates = [r["date"] for r in resp.json()]
        assert dates == sorted(dates, reverse=True)


# ════════════════════════════════════════════════════════════════
# 4. GET /health/weekly/current — creates row if missing
# ════════════════════════════════════════════════════════════════

class TestWeeklyHealthCurrent:

    def test_get_current_creates_row(self):
        db = _db()
        assert db.query(models.WeeklyHealth).count() == 0
        db.close()

        resp = client.get("/health/weekly/current")
        assert resp.status_code == 200
        data = resp.json()
        assert data["week_start_date"] == str(_current_week_start())
        assert data["bodyweight_kg"] is None
        assert data["energy_level"] is None

        db = _db()
        assert db.query(models.WeeklyHealth).count() == 1
        db.close()

    def test_get_current_idempotent(self):
        client.get("/health/weekly/current")
        client.get("/health/weekly/current")
        db = _db()
        assert db.query(models.WeeklyHealth).count() == 1
        db.close()


# ════════════════════════════════════════════════════════════════
# 5. PATCH /health/weekly/{week_start}
# ════════════════════════════════════════════════════════════════

class TestWeeklyHealthPatch:

    def test_patch_bodyweight(self):
        ws = str(_current_week_start())
        client.get("/health/weekly/current")
        resp = client.patch(f"/health/weekly/{ws}", json={"bodyweight_kg": 72.5})
        assert resp.status_code == 200
        assert resp.json()["bodyweight_kg"] == 72.5

    def test_patch_partial_leaves_other_fields(self):
        ws = str(_current_week_start())
        client.get("/health/weekly/current")
        client.patch(f"/health/weekly/{ws}", json={"bodyweight_kg": 72.0, "energy_level": 4})
        resp = client.patch(f"/health/weekly/{ws}", json={"protein_target_hit": True})
        data = resp.json()
        assert data["bodyweight_kg"] == 72.0
        assert data["energy_level"] == 4
        assert data["protein_target_hit"] is True

    def test_patch_all_fields(self):
        ws = str(_current_week_start())
        client.get("/health/weekly/current")
        resp = client.patch(f"/health/weekly/{ws}", json={
            "bodyweight_kg": 73.2,
            "protein_target_hit": True,
            "any_lift_progressed": True,
            "energy_level": 5,
        })
        data = resp.json()
        assert data["protein_target_hit"] is True
        assert data["any_lift_progressed"] is True
        assert data["energy_level"] == 5


# ════════════════════════════════════════════════════════════════
# 6. GET /health/weekly/history
# ════════════════════════════════════════════════════════════════

class TestWeeklyHealthHistory:

    def test_history_returns_correct_weeks(self):
        base = _current_week_start()
        db = _db()
        for i in range(5):
            db.add(models.WeeklyHealth(week_start_date=base - timedelta(weeks=i)))
        db.commit()
        db.close()

        resp = client.get("/health/weekly/history?weeks=3")
        assert resp.status_code == 200
        assert len(resp.json()) == 3

    def test_history_ordered_newest_first(self):
        base = _current_week_start()
        db = _db()
        for i in range(4):
            db.add(models.WeeklyHealth(week_start_date=base - timedelta(weeks=i)))
        db.commit()
        db.close()

        resp = client.get("/health/weekly/history?weeks=8")
        dates = [r["week_start_date"] for r in resp.json()]
        assert dates == sorted(dates, reverse=True)


# ════════════════════════════════════════════════════════════════
# 7. GET /health/lifts/progression — best-weight with reps tie-break
# ════════════════════════════════════════════════════════════════

class TestLiftProgression:

    def _seed_lift(self, db, lift, weight, reps, days_ago=0):
        d = date.today() - timedelta(days=days_ago)
        row = models.DailyHealth(
            date=d, session_done=True,
            main_lift=lift, top_set_weight=weight, top_set_reps=reps,
        )
        db.add(row)
        db.commit()
        return row

    def test_returns_all_five_lifts(self):
        resp = client.get("/health/lifts/progression")
        assert resp.status_code == 200
        names = {r["lift_name"] for r in resp.json()}
        assert names == {"Bench Press", "Pull-ups", "Squat", "Incline DB Press", "Barbell Row"}

    def test_no_data_returns_nulls(self):
        resp = client.get("/health/lifts/progression")
        for r in resp.json():
            assert r["best_weight"] is None
            assert r["sessions_logged"] == 0

    def test_best_weight_selected(self):
        db = _db()
        self._seed_lift(db, "Bench Press", 70.0, 5, days_ago=2)
        self._seed_lift(db, "Bench Press", 80.0, 3, days_ago=1)
        self._seed_lift(db, "Bench Press", 75.0, 5, days_ago=0)
        db.close()

        resp = client.get("/health/lifts/progression")
        bench = next(r for r in resp.json() if r["lift_name"] == "Bench Press")
        assert bench["best_weight"] == 80.0
        assert bench["sessions_logged"] == 3

    def test_tie_break_by_reps(self):
        db = _db()
        # Same weight, different reps — higher reps should win
        self._seed_lift(db, "Squat", 100.0, 3, days_ago=2)
        self._seed_lift(db, "Squat", 100.0, 5, days_ago=1)
        db.close()

        resp = client.get("/health/lifts/progression")
        squat = next(r for r in resp.json() if r["lift_name"] == "Squat")
        assert squat["best_weight"] == 100.0
        assert squat["best_reps"] == 5

    def test_sessions_logged_count(self):
        db = _db()
        for i in range(4):
            self._seed_lift(db, "Pull-ups", 0.0, i + 1, days_ago=i)
        db.close()

        resp = client.get("/health/lifts/progression")
        pullups = next(r for r in resp.json() if r["lift_name"] == "Pull-ups")
        assert pullups["sessions_logged"] == 4


# ════════════════════════════════════════════════════════════════
# 8. apply_parse_updates() with health_updates
# ════════════════════════════════════════════════════════════════

class TestParseHealthUpdates:

    def test_health_fields_populated_from_parse(self):
        payload = _fake_parse_response({
            "health_updates": {
                "sleep_hours": 7.0,
                "mobility_done": True,
                "session_done": True,
                "main_lift": "Bench Press",
                "top_set_weight": 80.0,
                "top_set_reps": 5,
            }
        })
        with patch("main.get_parse_response", return_value=payload):
            resp = client.post("/parse", json={"text": "slept 7h, mobility done, 80kg x5 bench"})

        assert resp.status_code == 200
        db = _db()
        row = db.query(models.DailyHealth).filter(
            models.DailyHealth.date == date.today()
        ).first()
        assert row is not None
        assert row.sleep_hours == 7.0
        assert row.mobility_done is True
        assert row.session_done is True
        assert row.main_lift == "Bench Press"
        assert row.top_set_weight == 80.0
        assert row.top_set_reps == 5
        db.close()

    def test_parse_without_health_updates_is_fine(self):
        payload = _fake_parse_response({})
        with patch("main.get_parse_response", return_value=payload):
            resp = client.post("/parse", json={"text": "nothing health related"})
        assert resp.status_code == 200

    def test_partial_health_update_leaves_existing_fields(self):
        # Pre-set sleep via direct PATCH
        today = str(date.today())
        client.patch(f"/health/daily/{today}", json={"sleep_hours": 8.0})

        # Parse only sets mobility
        payload = _fake_parse_response({
            "health_updates": {"mobility_done": True}
        })
        with patch("main.get_parse_response", return_value=payload):
            client.post("/parse", json={"text": "did mobility"})

        db = _db()
        row = db.query(models.DailyHealth).filter(
            models.DailyHealth.date == date.today()
        ).first()
        assert row.sleep_hours == 8.0   # unchanged
        assert row.mobility_done is True
        db.close()


# ════════════════════════════════════════════════════════════════
# 9. Radar — "health" key present, "physical" absent
# ════════════════════════════════════════════════════════════════

class TestRadarHealthAxis:

    def test_radar_returns_health_not_physical(self):
        resp = client.get("/radar")
        assert resp.status_code == 200
        scores = resp.json()["scores"]
        assert "health" in scores
        assert "physical" not in scores

    def test_radar_health_is_neutral_when_no_data(self):
        resp = client.get("/radar")
        scores = resp.json()["scores"]
        # With no DailyHealth rows, returns 50 (neutral baseline)
        assert scores["health"] == 50

    def test_radar_health_score_computed_from_data(self):
        today = date.today()
        db = _db()
        # Seed 4 days of data: all mobility done, all sessions done, 8h sleep
        for i in range(4):
            db.add(models.DailyHealth(
                date=today - timedelta(days=i),
                mobility_done=True,
                session_done=True,
                sleep_hours=8.0,
            ))
        db.commit()
        db.close()

        resp = client.get("/radar")
        scores = resp.json()["scores"]
        # With good data, health score should be above 50
        assert scores["health"] > 50
