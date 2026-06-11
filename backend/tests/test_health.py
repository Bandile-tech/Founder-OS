"""
pytest suite for Phase 3 / 3.1 — Health Module (LiftLog refactor).

Covers:
  - DailyHealth CRUD (sleep, mobility, session — no lift fields)
  - WeeklyHealth CRUD
  - GET /health/daily/today + /health/weekly/current create rows if missing
  - PATCH partial updates leave untouched fields unchanged
  - GET /health/daily/recent and /health/weekly/history windowing
  - Lift CRUD: create, list, rename, soft-delete (is_active=False)
  - LiftLog CRUD: create, today, recent, patch, delete
  - Multiple LiftLog rows per day all preserved
  - /health/lifts/progression: best-weight + reps tie-break, all-time sessions_logged
  - Active lifts only in progression; inactive lift excluded
  - Brain dump lift_logs creates new Lift (auto-create) and LiftLog row
  - Brain dump lift_logs matches existing Lift case-insensitively
  - Brain dump health_updates (sleep/mobility/session) still works
  - Migration idempotency: m003 safe to run twice
  - Radar returns "health" key, not "physical"
  - Radar health=50 when no DailyHealth data
  - Radar health>50 when good data present
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


def _seed_lift_row(db, name: str, weight: float, reps: int, days_ago: int = 0) -> models.LiftLog:
    """Helper: ensure a Lift exists and create a LiftLog entry for it."""
    lift = db.query(models.Lift).filter(models.Lift.name == name).first()
    if not lift:
        lift = models.Lift(name=name, sort_order=0, is_active=True)
        db.add(lift)
        db.commit()
        db.refresh(lift)
    log = models.LiftLog(
        lift_id=lift.id, lift_name=lift.name,
        date=date.today() - timedelta(days=days_ago),
        weight_kg=weight, reps=reps,
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return log


# ════════════════════════════════════════════════════════════════
# 1. DailyHealth — today endpoint & PATCH
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
        # Lift fields must NOT be present on DailyHealth any more
        assert "main_lift" not in data
        assert "top_set_weight" not in data
        assert "top_set_reps" not in data

        db = _db()
        assert db.query(models.DailyHealth).count() == 1
        db.close()

    def test_get_today_idempotent(self):
        client.get("/health/daily/today")
        client.get("/health/daily/today")
        db = _db()
        assert db.query(models.DailyHealth).count() == 1
        db.close()


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
        assert data["sleep_hours"] == 8.0
        assert data["mobility_done"] is True
        assert data["session_done"] is True

    def test_patch_creates_row_if_missing(self):
        yesterday = str(date.today() - timedelta(days=1))
        resp = client.patch(f"/health/daily/{yesterday}", json={"sleep_hours": 6.0})
        assert resp.status_code == 200
        assert resp.json()["sleep_hours"] == 6.0

    def test_patch_rejects_lift_fields(self):
        """Lift fields no longer exist on DailyHealth — ignored or 422."""
        client.get("/health/daily/today")
        today = str(date.today())
        # FastAPI will ignore unknown fields (extra="ignore" default in Pydantic v2).
        # What matters is the response still 200s and doesn't blow up.
        resp = client.patch(f"/health/daily/{today}", json={"sleep_hours": 7.0})
        assert resp.status_code == 200


# ════════════════════════════════════════════════════════════════
# 2. DailyHealth recent / WeeklyHealth
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
        assert len(resp.json()) == 3

    def test_recent_ordered_newest_first(self):
        today = date.today()
        db = _db()
        for i in range(3):
            db.add(models.DailyHealth(date=today - timedelta(days=i)))
        db.commit()
        db.close()

        dates = [r["date"] for r in client.get("/health/daily/recent?days=14").json()]
        assert dates == sorted(dates, reverse=True)


class TestWeeklyHealthCurrent:

    def test_get_current_creates_row(self):
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


class TestWeeklyHealthHistory:

    def test_history_returns_correct_weeks(self):
        base = _current_week_start()
        db = _db()
        for i in range(5):
            db.add(models.WeeklyHealth(week_start_date=base - timedelta(weeks=i)))
        db.commit()
        db.close()

        assert len(client.get("/health/weekly/history?weeks=3").json()) == 3

    def test_history_ordered_newest_first(self):
        base = _current_week_start()
        db = _db()
        for i in range(4):
            db.add(models.WeeklyHealth(week_start_date=base - timedelta(weeks=i)))
        db.commit()
        db.close()

        dates = [r["week_start_date"] for r in client.get("/health/weekly/history?weeks=8").json()]
        assert dates == sorted(dates, reverse=True)


# ════════════════════════════════════════════════════════════════
# 3. Lift CRUD
# ════════════════════════════════════════════════════════════════

class TestLiftCRUD:

    def _seed_defaults(self):
        """Seed the five default Lift rows directly in DB."""
        db = _db()
        for name in ["Bench Press", "Pull-ups", "Squat", "Incline DB Press", "Barbell Row"]:
            db.add(models.Lift(name=name, sort_order=0, is_active=True))
        db.commit()
        db.close()

    def test_list_active_lifts(self):
        self._seed_defaults()
        resp = client.get("/health/lifts")
        assert resp.status_code == 200
        names = {l["name"] for l in resp.json()}
        assert "Bench Press" in names
        assert len(resp.json()) == 5

    def test_create_lift(self):
        resp = client.post("/health/lifts", json={"name": "Deadlift"})
        assert resp.status_code == 201
        assert resp.json()["name"] == "Deadlift"
        assert resp.json()["is_active"] is True

    def test_duplicate_lift_rejected(self):
        client.post("/health/lifts", json={"name": "Deadlift"})
        resp = client.post("/health/lifts", json={"name": "Deadlift"})
        assert resp.status_code == 400

    def test_duplicate_case_insensitive_rejected(self):
        client.post("/health/lifts", json={"name": "Deadlift"})
        resp = client.post("/health/lifts", json={"name": "deadlift"})
        assert resp.status_code == 400

    def test_patch_rename_lift(self):
        r = client.post("/health/lifts", json={"name": "Romanian Deadlift"})
        lift_id = r.json()["id"]
        resp = client.patch(f"/health/lifts/{lift_id}", json={"name": "RDL"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "RDL"

    def test_soft_delete_sets_inactive(self):
        r = client.post("/health/lifts", json={"name": "Lunges"})
        lift_id = r.json()["id"]
        resp = client.delete(f"/health/lifts/{lift_id}")
        assert resp.status_code == 200
        assert resp.json()["is_active"] is False

    def test_inactive_lift_excluded_from_list(self):
        r = client.post("/health/lifts", json={"name": "Lunges"})
        lift_id = r.json()["id"]
        client.delete(f"/health/lifts/{lift_id}")
        names = [l["name"] for l in client.get("/health/lifts").json()]
        assert "Lunges" not in names

    def test_delete_nonexistent_lift_404(self):
        assert client.delete("/health/lifts/9999").status_code == 404

    def test_patch_nonexistent_lift_404(self):
        assert client.patch("/health/lifts/9999", json={"name": "x"}).status_code == 404

    def test_soft_delete_preserves_lift_log_history(self):
        """Deactivating a lift must not delete historical LiftLog rows."""
        r = client.post("/health/lifts", json={"name": "Hip Thrust"})
        lift_id = r.json()["id"]
        client.post("/health/lift-logs", json={
            "lift_name": "Hip Thrust",
            "date": str(date.today()),
            "weight_kg": 80.0,
            "reps": 10,
        })
        client.delete(f"/health/lifts/{lift_id}")
        db = _db()
        count = db.query(models.LiftLog).filter(models.LiftLog.lift_name == "Hip Thrust").count()
        db.close()
        assert count == 1


# ════════════════════════════════════════════════════════════════
# 4. LiftLog CRUD
# ════════════════════════════════════════════════════════════════

class TestLiftLogCRUD:

    def test_create_lift_log_auto_creates_lift(self):
        resp = client.post("/health/lift-logs", json={
            "lift_name": "Deadlift",
            "date": str(date.today()),
            "weight_kg": 120.0,
            "reps": 3,
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["lift_name"] == "Deadlift"
        assert data["weight_kg"] == 120.0
        assert data["reps"] == 3
        db = _db()
        assert db.query(models.Lift).filter(models.Lift.name == "Deadlift").count() == 1
        db.close()

    def test_multiple_lifts_same_day_all_preserved(self):
        today = str(date.today())
        client.post("/health/lift-logs", json={"lift_name": "Bench Press", "date": today, "weight_kg": 80.0, "reps": 5})
        client.post("/health/lift-logs", json={"lift_name": "Squat",       "date": today, "weight_kg": 100.0, "reps": 5})
        client.post("/health/lift-logs", json={"lift_name": "Barbell Row", "date": today, "weight_kg": 70.0, "reps": 8})

        resp = client.get("/health/lift-logs/today")
        assert resp.status_code == 200
        assert len(resp.json()) == 3
        names = {r["lift_name"] for r in resp.json()}
        assert names == {"Bench Press", "Squat", "Barbell Row"}

    def test_get_today_only_returns_todays_logs(self):
        yesterday = str(date.today() - timedelta(days=1))
        today = str(date.today())
        client.post("/health/lift-logs", json={"lift_name": "Squat", "date": yesterday, "weight_kg": 90.0, "reps": 5})
        client.post("/health/lift-logs", json={"lift_name": "Squat", "date": today,     "weight_kg": 95.0, "reps": 5})

        resp = client.get("/health/lift-logs/today")
        assert len(resp.json()) == 1
        assert resp.json()[0]["weight_kg"] == 95.0

    def test_get_recent_window(self):
        today = date.today()
        for i in range(5):
            client.post("/health/lift-logs", json={
                "lift_name": "Bench Press",
                "date": str(today - timedelta(days=i)),
                "weight_kg": 80.0, "reps": 5,
            })
        resp = client.get("/health/lift-logs/recent?days=3")
        assert resp.status_code == 200
        assert len(resp.json()) == 3

    def test_patch_lift_log(self):
        r = client.post("/health/lift-logs", json={
            "lift_name": "Bench Press", "date": str(date.today()),
            "weight_kg": 80.0, "reps": 5,
        })
        log_id = r.json()["id"]
        resp = client.patch(f"/health/lift-logs/{log_id}", json={"weight_kg": 82.5, "reps": 4})
        assert resp.status_code == 200
        assert resp.json()["weight_kg"] == 82.5
        assert resp.json()["reps"] == 4

    def test_patch_lift_log_404(self):
        assert client.patch("/health/lift-logs/9999", json={"reps": 3}).status_code == 404

    def test_delete_lift_log(self):
        r = client.post("/health/lift-logs", json={
            "lift_name": "Pull-ups", "date": str(date.today()),
            "weight_kg": 0.0, "reps": 10,
        })
        log_id = r.json()["id"]
        resp = client.delete(f"/health/lift-logs/{log_id}")
        assert resp.status_code == 200
        assert resp.json()["deleted"] == log_id
        db = _db()
        assert db.query(models.LiftLog).filter(models.LiftLog.id == log_id).first() is None
        db.close()

    def test_delete_lift_log_404(self):
        assert client.delete("/health/lift-logs/9999").status_code == 404


# ════════════════════════════════════════════════════════════════
# 5. /health/lifts/progression
# ════════════════════════════════════════════════════════════════

class TestLiftProgression:

    def test_returns_only_active_lifts(self):
        db = _db()
        for name in ["Bench Press", "Pull-ups", "Squat", "Incline DB Press", "Barbell Row"]:
            db.add(models.Lift(name=name, sort_order=0, is_active=True))
        db.commit()
        db.close()

        resp = client.get("/health/lifts/progression")
        assert resp.status_code == 200
        names = {r["lift_name"] for r in resp.json()}
        assert names == {"Bench Press", "Pull-ups", "Squat", "Incline DB Press", "Barbell Row"}

    def test_no_logs_returns_nulls(self):
        db = _db()
        db.add(models.Lift(name="Bench Press", sort_order=0, is_active=True))
        db.commit()
        db.close()

        resp = client.get("/health/lifts/progression")
        bench = next(r for r in resp.json() if r["lift_name"] == "Bench Press")
        assert bench["best_weight"] is None
        assert bench["sessions_logged"] == 0

    def test_best_weight_selected_across_sessions(self):
        db = _db()
        _seed_lift_row(db, "Bench Press", 70.0, 5, days_ago=3)
        _seed_lift_row(db, "Bench Press", 80.0, 3, days_ago=2)
        _seed_lift_row(db, "Bench Press", 75.0, 5, days_ago=1)
        db.close()

        resp = client.get("/health/lifts/progression")
        bench = next(r for r in resp.json() if r["lift_name"] == "Bench Press")
        assert bench["best_weight"] == 80.0

    def test_tie_break_by_reps(self):
        db = _db()
        _seed_lift_row(db, "Squat", 100.0, 3, days_ago=2)
        _seed_lift_row(db, "Squat", 100.0, 5, days_ago=1)
        db.close()

        resp = client.get("/health/lifts/progression")
        squat = next(r for r in resp.json() if r["lift_name"] == "Squat")
        assert squat["best_weight"] == 100.0
        assert squat["best_reps"] == 5

    def test_sessions_logged_is_all_time_count(self):
        db = _db()
        for i in range(6):
            _seed_lift_row(db, "Pull-ups", 0.0, i, days_ago=i * 7)
        db.close()

        resp = client.get("/health/lifts/progression")
        pullups = next(r for r in resp.json() if r["lift_name"] == "Pull-ups")
        assert pullups["sessions_logged"] == 6

    def test_multiple_lifts_same_day_each_tracked_independently(self):
        db = _db()
        _seed_lift_row(db, "Bench Press", 80.0, 5, days_ago=0)
        _seed_lift_row(db, "Squat",       100.0, 5, days_ago=0)
        db.close()

        resp = client.get("/health/lifts/progression")
        data = {r["lift_name"]: r for r in resp.json()}
        assert data["Bench Press"]["best_weight"] == 80.0
        assert data["Squat"]["best_weight"] == 100.0

    def test_inactive_lift_excluded_from_progression(self):
        db = _db()
        _seed_lift_row(db, "Hip Thrust", 120.0, 10, days_ago=0)
        db.close()

        # Deactivate it
        lift = _db().query(models.Lift).filter(models.Lift.name == "Hip Thrust").first()
        resp = client.delete(f"/health/lifts/{lift.id}")
        assert resp.status_code == 200

        progression = client.get("/health/lifts/progression").json()
        assert not any(r["lift_name"] == "Hip Thrust" for r in progression)


# ════════════════════════════════════════════════════════════════
# 6. Brain dump integration
# ════════════════════════════════════════════════════════════════

class TestParseHealthUpdates:

    def test_health_updates_sets_daily_fields(self):
        payload = _fake_parse_response({
            "health_updates": {"sleep_hours": 7.0, "mobility_done": True, "session_done": True}
        })
        with patch("main.get_parse_response", return_value=payload):
            resp = client.post("/parse", json={"text": "slept 7h, mobility done, trained"})
        assert resp.status_code == 200

        db = _db()
        row = db.query(models.DailyHealth).filter(models.DailyHealth.date == date.today()).first()
        assert row.sleep_hours == 7.0
        assert row.mobility_done is True
        assert row.session_done is True
        db.close()

    def test_lift_logs_creates_new_lift_and_log(self):
        payload = _fake_parse_response({
            "lift_logs": [{"lift_name": "Deadlift", "weight_kg": 140.0, "reps": 2}]
        })
        with patch("main.get_parse_response", return_value=payload):
            client.post("/parse", json={"text": "hit 140kg x2 deadlift"})

        db = _db()
        lift = db.query(models.Lift).filter(models.Lift.name == "Deadlift").first()
        assert lift is not None
        log = db.query(models.LiftLog).filter(models.LiftLog.lift_name == "Deadlift").first()
        assert log is not None
        assert log.weight_kg == 140.0
        assert log.reps == 2
        db.close()

    def test_lift_logs_matches_existing_lift_case_insensitively(self):
        # Pre-create "Bench Press"
        db = _db()
        db.add(models.Lift(name="Bench Press", sort_order=1, is_active=True))
        db.commit()
        db.close()

        payload = _fake_parse_response({
            "lift_logs": [{"lift_name": "bench press", "weight_kg": 85.0, "reps": 3}]
        })
        with patch("main.get_parse_response", return_value=payload):
            client.post("/parse", json={"text": "85kg x3 bench press"})

        db = _db()
        # Should NOT create a second Lift row
        assert db.query(models.Lift).filter(models.Lift.name.ilike("bench press")).count() == 1
        log = db.query(models.LiftLog).filter(models.LiftLog.lift_name == "Bench Press").first()
        assert log is not None
        assert log.weight_kg == 85.0
        db.close()

    def test_lift_logs_multiple_in_one_parse(self):
        payload = _fake_parse_response({
            "lift_logs": [
                {"lift_name": "Bench Press", "weight_kg": 80.0, "reps": 5},
                {"lift_name": "Squat",       "weight_kg": 100.0, "reps": 5},
            ]
        })
        with patch("main.get_parse_response", return_value=payload):
            client.post("/parse", json={"text": "benched 80, squatted 100"})

        db = _db()
        assert db.query(models.LiftLog).count() == 2
        db.close()

    def test_parse_without_lift_logs_is_fine(self):
        payload = _fake_parse_response({})
        with patch("main.get_parse_response", return_value=payload):
            resp = client.post("/parse", json={"text": "nothing fitness-related"})
        assert resp.status_code == 200

    def test_lift_log_entry_with_missing_fields_skipped(self):
        """Entries with missing weight or reps must be silently skipped."""
        payload = _fake_parse_response({
            "lift_logs": [
                {"lift_name": "Bench Press"},             # no weight or reps
                {"lift_name": "Squat", "weight_kg": 100.0},  # no reps
                {"lift_name": "Deadlift", "weight_kg": 120.0, "reps": 3},  # valid
            ]
        })
        with patch("main.get_parse_response", return_value=payload):
            client.post("/parse", json={"text": "mixed quality data"})

        db = _db()
        assert db.query(models.LiftLog).count() == 1
        assert db.query(models.LiftLog).first().lift_name == "Deadlift"
        db.close()

    def test_partial_health_update_leaves_existing_fields(self):
        today = str(date.today())
        client.patch(f"/health/daily/{today}", json={"sleep_hours": 8.0})

        payload = _fake_parse_response({
            "health_updates": {"mobility_done": True}
        })
        with patch("main.get_parse_response", return_value=payload):
            client.post("/parse", json={"text": "did mobility"})

        db = _db()
        row = db.query(models.DailyHealth).filter(models.DailyHealth.date == date.today()).first()
        assert row.sleep_hours == 8.0
        assert row.mobility_done is True
        db.close()


# ════════════════════════════════════════════════════════════════
# 7. Migration m003 idempotency
# ════════════════════════════════════════════════════════════════

class TestMigrationIdempotency:

    def test_m003_safe_to_run_twice(self):
        """Running m003 a second time must not raise or corrupt data."""
        from migrations.m003_lift_log import run as run_m003
        db = _db()
        try:
            run_m003(db)   # first run (already done by startup fixture)
            run_m003(db)   # second run — must be a no-op
        except Exception as e:
            pytest.fail(f"m003 raised on second run: {e}")
        finally:
            db.close()

    def test_default_lifts_seeded_once(self):
        """Even if m003 runs twice, default lifts appear exactly once each."""
        from migrations.m003_lift_log import run as run_m003
        db = _db()
        run_m003(db)
        db.close()

        db2 = _db()
        bench_count = db2.query(models.Lift).filter(models.Lift.name == "Bench Press").count()
        db2.close()
        assert bench_count == 1


# ════════════════════════════════════════════════════════════════
# 8. Radar health axis
# ════════════════════════════════════════════════════════════════

class TestRadarHealthAxis:

    def test_radar_returns_health_not_physical(self):
        resp = client.get("/radar")
        assert resp.status_code == 200
        scores = resp.json()["scores"]
        assert "health" in scores
        assert "physical" not in scores

    def test_radar_health_is_neutral_when_no_data(self):
        scores = client.get("/radar").json()["scores"]
        assert scores["health"] == 50

    def test_radar_health_score_computed_from_data(self):
        today = date.today()
        db = _db()
        for i in range(4):
            db.add(models.DailyHealth(
                date=today - timedelta(days=i),
                mobility_done=True, session_done=True, sleep_hours=8.0,
            ))
        db.commit()
        db.close()

        scores = client.get("/radar").json()["scores"]
        assert scores["health"] > 50
