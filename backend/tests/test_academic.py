"""
pytest suite for Phase 2 — Academic Roadmap.

Covers:
  - Subject CRUD + cascade delete
  - Topic CRUD + cascade delete
  - Subtopic CRUD (incl. auto last_reviewed_date)
  - /subjects/progress aggregation (mastery_pct, weighted_pct, null when empty)
  - /subjects/weakest ordering
  - Cascade delete: subject → topics → subtopics
  - Pre-seed migration idempotency
"""

import sys
from pathlib import Path
from datetime import date

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import models
from main import app
# Engine, session, client, override, and fresh_db fixture come from conftest.py
from tests.conftest import TestSessionLocal, client


# ── Helpers ──────────────────────────────────────────────────

def _db():
    return TestSessionLocal()


def _make_subject(name="Maths 9709", code="9709", exam_date="2026-11-01"):
    resp = client.post("/subjects", json={"name": name, "code": code, "exam_date": exam_date})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _make_topic(subject_id: int, name="Pure Maths 1", weight=8):
    resp = client.post(f"/subjects/{subject_id}/topics",
                       json={"name": name, "syllabus_weight": weight})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _make_subtopic(topic_id: int, name="Quadratics", mastery=0):
    resp = client.post(f"/topics/{topic_id}/subtopics",
                       json={"name": name, "mastery_level": mastery})
    assert resp.status_code == 201, resp.text
    return resp.json()


# ════════════════════════════════════════════════════════════
# Subject CRUD
# ════════════════════════════════════════════════════════════

class TestSubjectCRUD:

    def test_create_subject(self):
        subj = _make_subject()
        assert subj["name"] == "Maths 9709"
        assert subj["code"] == "9709"
        assert subj["topics"] == []

    def test_list_subjects(self):
        _make_subject("Maths 9709", "9709")
        _make_subject("Business 9609", "9609")
        resp = client.get("/subjects")
        assert resp.status_code == 200
        codes = [s["code"] for s in resp.json()]
        assert "9709" in codes
        assert "9609" in codes

    def test_duplicate_code_rejected(self):
        _make_subject()
        resp = client.post("/subjects", json={"name": "Maths Again", "code": "9709"})
        assert resp.status_code == 400

    def test_update_subject(self):
        subj = _make_subject()
        resp = client.patch(f"/subjects/{subj['id']}", json={"name": "Maths 9709 Updated"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "Maths 9709 Updated"

    def test_delete_subject(self):
        subj = _make_subject()
        resp = client.delete(f"/subjects/{subj['id']}")
        assert resp.status_code == 204
        resp2 = client.get("/subjects")
        assert all(s["id"] != subj["id"] for s in resp2.json())

    def test_update_nonexistent_subject_404(self):
        resp = client.patch("/subjects/9999", json={"name": "X"})
        assert resp.status_code == 404

    def test_delete_nonexistent_subject_404(self):
        resp = client.delete("/subjects/9999")
        assert resp.status_code == 404


# ════════════════════════════════════════════════════════════
# Topic CRUD
# ════════════════════════════════════════════════════════════

class TestTopicCRUD:

    def test_create_topic(self):
        subj = _make_subject()
        topic = _make_topic(subj["id"])
        assert topic["name"] == "Pure Maths 1"
        assert topic["syllabus_weight"] == 8
        assert topic["subject_id"] == subj["id"]
        assert topic["subtopics"] == []

    def test_topic_appears_in_subject_list(self):
        subj = _make_subject()
        _make_topic(subj["id"], "Pure Maths 1")
        _make_topic(subj["id"], "Statistics 1", weight=7)
        resp = client.get("/subjects")
        subj_data = next(s for s in resp.json() if s["id"] == subj["id"])
        assert len(subj_data["topics"]) == 2

    def test_update_topic(self):
        subj = _make_subject()
        topic = _make_topic(subj["id"])
        resp = client.patch(f"/topics/{topic['id']}",
                            json={"name": "Further Pure", "syllabus_weight": 9})
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Further Pure"
        assert data["syllabus_weight"] == 9

    def test_delete_topic(self):
        subj = _make_subject()
        topic = _make_topic(subj["id"])
        resp = client.delete(f"/topics/{topic['id']}")
        assert resp.status_code == 204
        resp2 = client.get("/subjects")
        subj_data = next(s for s in resp2.json() if s["id"] == subj["id"])
        assert subj_data["topics"] == []

    def test_create_topic_for_missing_subject_404(self):
        resp = client.post("/subjects/9999/topics", json={"name": "X"})
        assert resp.status_code == 404

    def test_update_nonexistent_topic_404(self):
        resp = client.patch("/topics/9999", json={"name": "X"})
        assert resp.status_code == 404


# ════════════════════════════════════════════════════════════
# Subtopic CRUD
# ════════════════════════════════════════════════════════════

class TestSubtopicCRUD:

    def test_create_subtopic(self):
        subj = _make_subject()
        topic = _make_topic(subj["id"])
        st = _make_subtopic(topic["id"], "Quadratics", mastery=0)
        assert st["name"] == "Quadratics"
        assert st["mastery_level"] == 0
        assert st["last_reviewed_date"] is None

    def test_update_mastery_sets_last_reviewed_date(self):
        subj = _make_subject()
        topic = _make_topic(subj["id"])
        st = _make_subtopic(topic["id"])
        resp = client.patch(f"/subtopics/{st['id']}", json={"mastery_level": 75})
        assert resp.status_code == 200
        data = resp.json()
        assert data["mastery_level"] == 75
        assert data["last_reviewed_date"] == str(date.today())

    def test_update_mastery_with_explicit_date_uses_provided_date(self):
        subj = _make_subject()
        topic = _make_topic(subj["id"])
        st = _make_subtopic(topic["id"])
        resp = client.patch(f"/subtopics/{st['id']}",
                            json={"mastery_level": 50, "last_reviewed_date": "2026-01-15"})
        assert resp.status_code == 200
        assert resp.json()["last_reviewed_date"] == "2026-01-15"

    def test_update_notes_does_not_change_last_reviewed(self):
        subj = _make_subject()
        topic = _make_topic(subj["id"])
        st = _make_subtopic(topic["id"])
        resp = client.patch(f"/subtopics/{st['id']}", json={"notes": "Review binomial theorem"})
        assert resp.status_code == 200
        assert resp.json()["last_reviewed_date"] is None
        assert resp.json()["notes"] == "Review binomial theorem"

    def test_delete_subtopic(self):
        subj = _make_subject()
        topic = _make_topic(subj["id"])
        st = _make_subtopic(topic["id"])
        resp = client.delete(f"/subtopics/{st['id']}")
        assert resp.status_code == 204
        resp2 = client.get("/subjects")
        subj_data = next(s for s in resp2.json() if s["id"] == subj["id"])
        assert subj_data["topics"][0]["subtopics"] == []

    def test_create_subtopic_for_missing_topic_404(self):
        resp = client.post("/topics/9999/subtopics", json={"name": "X"})
        assert resp.status_code == 404

    def test_update_nonexistent_subtopic_404(self):
        resp = client.patch("/subtopics/9999", json={"mastery_level": 50})
        assert resp.status_code == 404


# ════════════════════════════════════════════════════════════
# Progress aggregation
# ════════════════════════════════════════════════════════════

class TestProgressAggregation:

    def _build_scenario(self):
        """
        Subject A:
          Topic 1 (weight=8): subtopics mastery [80, 60]  → avg=70
          Topic 2 (weight=4): subtopics mastery [40]      → avg=40
        weighted_pct = (8*70 + 4*40) / (8+4) = (560+160)/12 = 60.0
        mastery_pct  = (80+60+40) / 3 = 60.0
        """
        subj = _make_subject()
        t1 = _make_topic(subj["id"], "Topic 1", weight=8)
        t2 = _make_topic(subj["id"], "Topic 2", weight=4)
        _make_subtopic(t1["id"], "Sub A", mastery=80)
        _make_subtopic(t1["id"], "Sub B", mastery=60)
        _make_subtopic(t2["id"], "Sub C", mastery=40)
        return subj

    def test_mastery_pct_correct(self):
        self._build_scenario()
        resp = client.get("/subjects/progress")
        assert resp.status_code == 200
        row = resp.json()[0]
        assert row["mastery_pct"] == 60.0

    def test_weighted_pct_correct(self):
        self._build_scenario()
        row = client.get("/subjects/progress").json()[0]
        assert row["weighted_pct"] == 60.0

    def test_weighted_pct_differs_from_simple_when_weights_unequal(self):
        """
        Topic 1 (weight=10): [100]   → avg=100
        Topic 2 (weight=1):  [0]     → avg=0
        weighted = (10*100 + 1*0)/11 ≈ 90.9
        simple   = (100+0)/2 = 50
        """
        subj = _make_subject()
        t1 = _make_topic(subj["id"], "Heavy", weight=10)
        t2 = _make_topic(subj["id"], "Light", weight=1)
        _make_subtopic(t1["id"], "A", mastery=100)
        _make_subtopic(t2["id"], "B", mastery=0)
        row = client.get("/subjects/progress").json()[0]
        assert row["weighted_pct"] == round(1000 / 11, 1)
        assert row["mastery_pct"] == 50.0

    def test_null_when_zero_subtopics(self):
        """Subject with topics but no subtopics → both pcts are null."""
        subj = _make_subject()
        _make_topic(subj["id"], "Empty Topic", weight=8)
        row = client.get("/subjects/progress").json()[0]
        assert row["weighted_pct"] is None
        assert row["mastery_pct"] is None
        assert row["subtopic_count"] == 0

    def test_null_when_no_topics(self):
        """Subject with zero topics → both pcts are null."""
        _make_subject()
        row = client.get("/subjects/progress").json()[0]
        assert row["weighted_pct"] is None
        assert row["mastery_pct"] is None

    def test_topic_with_no_subtopics_excluded_from_weighting(self):
        """
        Topic 1 (weight=8): [60]  ← has subtopics
        Topic 2 (weight=5): []    ← no subtopics, must not add weight
        weighted = (8*60) / 8 = 60.0  (not (8*60+0)/(8+5))
        """
        subj = _make_subject()
        t1 = _make_topic(subj["id"], "Full", weight=8)
        _make_topic(subj["id"], "Empty", weight=5)
        _make_subtopic(t1["id"], "Sub", mastery=60)
        row = client.get("/subjects/progress").json()[0]
        assert row["weighted_pct"] == 60.0

    def test_subtopic_count_correct(self):
        subj = _make_subject()
        t1 = _make_topic(subj["id"], "T1", weight=5)
        t2 = _make_topic(subj["id"], "T2", weight=5)
        _make_subtopic(t1["id"], "A")
        _make_subtopic(t1["id"], "B")
        _make_subtopic(t2["id"], "C")
        row = client.get("/subjects/progress").json()[0]
        assert row["subtopic_count"] == 3

    def test_multiple_subjects_returned(self):
        _make_subject("Maths 9709", "9709")
        _make_subject("Business 9609", "9609")
        resp = client.get("/subjects/progress")
        assert len(resp.json()) == 2


# ════════════════════════════════════════════════════════════
# Weakest subtopics
# ════════════════════════════════════════════════════════════

class TestWeakestSubtopics:

    def test_returns_correct_limit(self):
        subj = _make_subject()
        topic = _make_topic(subj["id"], weight=5)
        for i in range(10):
            _make_subtopic(topic["id"], f"Sub {i}", mastery=i * 10)
        resp = client.get("/subjects/weakest?limit=3")
        assert resp.status_code == 200
        assert len(resp.json()) == 3

    def test_lowest_mastery_first(self):
        subj = _make_subject()
        topic = _make_topic(subj["id"], weight=5)
        _make_subtopic(topic["id"], "High",   mastery=90)
        _make_subtopic(topic["id"], "Medium", mastery=50)
        _make_subtopic(topic["id"], "Low",    mastery=10)
        rows = client.get("/subjects/weakest?limit=3").json()
        assert rows[0]["name"] == "Low"
        assert rows[1]["name"] == "Medium"
        assert rows[2]["name"] == "High"

    def test_higher_weight_topic_surfaces_first(self):
        """
        Two topics: heavy (w=10, mastery=30) and light (w=2, mastery=10).
        The heavy topic's subtopic should come first despite higher mastery,
        because weight DESC takes priority.
        """
        subj = _make_subject()
        t_heavy = _make_topic(subj["id"], "Heavy", weight=10)
        t_light = _make_topic(subj["id"], "Light", weight=2)
        _make_subtopic(t_heavy["id"], "HeavySub", mastery=30)
        _make_subtopic(t_light["id"], "LightSub", mastery=10)
        rows = client.get("/subjects/weakest?limit=2").json()
        assert rows[0]["name"] == "HeavySub"
        assert rows[1]["name"] == "LightSub"

    def test_response_contains_expected_fields(self):
        subj = _make_subject()
        topic = _make_topic(subj["id"], "P1", weight=8)
        _make_subtopic(topic["id"], "Quadratics", mastery=25)
        row = client.get("/subjects/weakest?limit=1").json()[0]
        assert "subtopic_id" in row
        assert "name" in row
        assert "mastery_level" in row
        assert "topic_name" in row
        assert "topic_weight" in row
        assert "subject_name" in row
        assert "subject_code" in row

    def test_empty_returns_empty_list(self):
        resp = client.get("/subjects/weakest")
        assert resp.status_code == 200
        assert resp.json() == []


# ════════════════════════════════════════════════════════════
# Cascade delete
# ════════════════════════════════════════════════════════════

class TestCascadeDelete:

    def test_delete_subject_removes_topics_and_subtopics(self):
        subj = _make_subject()
        topic = _make_topic(subj["id"])
        st = _make_subtopic(topic["id"])

        client.delete(f"/subjects/{subj['id']}")

        db = _db()
        assert db.query(models.Topic).filter(models.Topic.id == topic["id"]).first() is None
        assert db.query(models.Subtopic).filter(models.Subtopic.id == st["id"]).first() is None
        db.close()

    def test_delete_topic_removes_subtopics(self):
        subj = _make_subject()
        topic = _make_topic(subj["id"])
        st = _make_subtopic(topic["id"])

        client.delete(f"/topics/{topic['id']}")

        db = _db()
        assert db.query(models.Subtopic).filter(models.Subtopic.id == st["id"]).first() is None
        # Subject still exists
        assert db.query(models.Subject).filter(models.Subject.id == subj["id"]).first() is not None
        db.close()

    def test_delete_subtopic_leaves_topic_and_subject(self):
        subj = _make_subject()
        topic = _make_topic(subj["id"])
        st = _make_subtopic(topic["id"])

        client.delete(f"/subtopics/{st['id']}")

        db = _db()
        assert db.query(models.Topic).filter(models.Topic.id == topic["id"]).first() is not None
        assert db.query(models.Subject).filter(models.Subject.id == subj["id"]).first() is not None
        db.close()


# ════════════════════════════════════════════════════════════
# Pre-seed migration idempotency
# ════════════════════════════════════════════════════════════

class TestPreSeedMigration:

    def test_seed_inserts_four_subjects(self):
        from migrations.m002_academic_roadmap import seed_subjects
        db = _db()
        seed_subjects(db)
        db.close()

        resp = client.get("/subjects")
        codes = {s["code"] for s in resp.json()}
        assert codes == {"9709", "9231", "9609", "9708"}

    def test_seed_inserts_topics(self):
        from migrations.m002_academic_roadmap import seed_subjects
        db = _db()
        seed_subjects(db)
        db.close()

        resp = client.get("/subjects")
        subjects = {s["code"]: s for s in resp.json()}
        assert len(subjects["9709"]["topics"]) == 2
        assert len(subjects["9231"]["topics"]) == 2
        assert len(subjects["9609"]["topics"]) == 5
        assert len(subjects["9708"]["topics"]) == 6

    def test_seed_is_idempotent_run_twice(self):
        from migrations.m002_academic_roadmap import seed_subjects
        db = _db()
        seed_subjects(db)
        seed_subjects(db)   # second run must not duplicate
        db.close()

        resp = client.get("/subjects")
        codes = [s["code"] for s in resp.json()]
        assert len(codes) == len(set(codes)), "Duplicate subjects after double seed"

    def test_seed_skips_existing_subject(self):
        """If 9709 already exists, seed must not insert a second copy."""
        from migrations.m002_academic_roadmap import seed_subjects
        _make_subject("Maths 9709", "9709")   # pre-insert via API
        db = _db()
        seed_subjects(db)
        db.close()

        resp = client.get("/subjects")
        assert sum(1 for s in resp.json() if s["code"] == "9709") == 1

    def test_deleted_subject_reseeded_on_next_run(self):
        """
        Deleting a subject and re-running seed must recreate only that
        subject, not touch the others.
        """
        from migrations.m002_academic_roadmap import seed_subjects
        db = _db()
        seed_subjects(db)
        db.close()

        # Delete Maths
        resp = client.get("/subjects")
        maths = next(s for s in resp.json() if s["code"] == "9709")
        client.delete(f"/subjects/{maths['id']}")

        # Re-seed
        db = _db()
        seed_subjects(db)
        db.close()

        resp2 = client.get("/subjects")
        codes = {s["code"] for s in resp2.json()}
        assert "9709" in codes, "Deleted subject was not re-seeded"
        assert len(codes) == 4, "Wrong number of subjects after re-seed"
