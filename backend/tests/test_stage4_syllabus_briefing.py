"""
Rebuild Stage 4 — syllabus model, CSV import, exam dates, daily briefing, alerts.

OpenAI is mocked. The briefing must work fully with no key.
"""

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import models
import llm
import syllabus
import briefing
import orchestrator_tools as ot
from tests.conftest import TestSessionLocal, client

TODAY = date.today()
FIXTURE = Path(__file__).parent / "fixtures" / "syllabus_sample.csv"


def _db():
    return TestSessionLocal()


@pytest.fixture(autouse=True)
def _no_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    llm.reset_client()
    yield
    llm.reset_client()


def _import(papers="P1,S1", code="9709", name="Maths 9709", level="AS"):
    with FIXTURE.open("rb") as fh:
        data = {"subject_code": code, "subject_name": name, "level": level}
        if papers:
            data["papers"] = papers
        r = client.post("/syllabus/import", files={"file": ("tracker.csv", fh, "text/csv")}, data=data)
    assert r.status_code == 200, r.text
    return r.json()


def _seed_subject(code="9709", name="Maths 9709", exam=None):
    db = _db()
    if not db.query(models.Subject).filter(models.Subject.code == code).first():
        db.add(models.Subject(name=name, code=code, exam_date=exam))
        db.commit()
    db.close()


# ════════════════════════════════════════════════════════════════
# 1. CSV import (acceptance test 7)
# ════════════════════════════════════════════════════════════════

class TestImport:

    def test_fixture_imports_blocks_blanks_and_legend(self):
        result = _import()
        assert result["created"] == 10 and result["updated"] == 0 and result["skipped"] == 0
        assert result["papers"] == ["P1", "S1"]
        db = _db()
        points = db.query(models.SyllabusPoint).order_by(models.SyllabusPoint.topic_code).all()
        db.close()
        by_code = {p.topic_code: p for p in points}
        assert "9.9" not in by_code                                   # everything after Legend dropped
        assert by_code["1.3"].title == "Coordinate geometry"
        assert by_code["1.3"].paper == "P1" and by_code["5.3"].paper == "S1"   # blocks → papers in order
        assert by_code["1.1"].status == "bulletproof"
        assert by_code["1.2"].status == "revised" and by_code["1.2"].past_paper_score == 71.0
        assert by_code["1.3"].status == "learned"
        assert by_code["1.4"].status == "not_started"
        assert by_code["1.5"].last_revised == date(2026, 10, 12)      # dd/mm/yyyy
        assert by_code["5.3"].weakness_notes == "Conditional probability"
        assert all(p.level == "AS" and p.subject_name == "Maths 9709" for p in points)

    def test_reimport_never_duplicates(self):
        _import()
        second = _import()
        assert second["created"] == 0 and second["updated"] == 0 and second["skipped"] == 10
        db = _db()
        assert db.query(models.SyllabusPoint).count() == 10
        db.close()

    def test_reimport_after_edit_updates_not_duplicates(self):
        _import()
        db = _db()
        p = db.query(models.SyllabusPoint).filter(models.SyllabusPoint.topic_code == "1.4").first()
        p.status = "learned"
        db.commit()
        db.close()
        second = _import()
        assert second["updated"] == 1 and second["skipped"] == 9
        db = _db()
        assert db.query(models.SyllabusPoint).filter(models.SyllabusPoint.topic_code == "1.4").first().status == "not_started"
        assert db.query(models.SyllabusPoint).count() == 10
        db.close()

    def test_paper_column_wins_over_papers_list(self):
        text = ("Paper,Topic,Learned,Revised,Bulletproof,Past Paper Score %,Last Revised,Weakness Notes\n"
                "P3,7.1 Algebra,TRUE,FALSE,FALSE,,,\n"
                "S2,8.2 Hypothesis tests,FALSE,FALSE,FALSE,,,\n")
        rows = syllabus.parse_tracker_csv(text, papers=["P1"])
        assert [r["paper"] for r in rows] == ["P3", "S2"]

    def test_topic_without_code_gets_slug(self):
        assert syllabus.split_topic("Coordinate geometry") == ("coordinate-geometry", "Coordinate geometry")
        assert syllabus.split_topic("1.3 Coordinate geometry") == ("1.3", "Coordinate geometry")
        assert syllabus.split_topic("S1.2: Probability") == ("S1.2", "Probability")

    def test_status_is_highest_true_flag(self):
        assert syllabus.status_from_flags("TRUE", "FALSE", "TRUE") == "bulletproof"
        assert syllabus.status_from_flags("x", "yes", "") == "revised"
        assert syllabus.status_from_flags("1", "", "") == "learned"
        assert syllabus.status_from_flags("", "", "") == "not_started"


# ════════════════════════════════════════════════════════════════
# 2. Endpoints
# ════════════════════════════════════════════════════════════════

class TestEndpoints:

    def test_get_syllabus_grouped_with_progress(self):
        _seed_subject(exam=date(2026, 11, 1))
        _import()
        data = client.get("/syllabus").json()
        assert data["progress"]["total"] == 10
        subj = data["subjects"][0]
        assert subj["subject_code"] == "9709" and subj["exam_date"] == "2026-11-01"
        papers = {p["paper"]: p for p in subj["papers"]}
        assert set(papers) == {"P1", "S1"}
        assert papers["P1"]["progress"]["counts"] == {"not_started": 1, "learned": 2, "revised": 1, "bulletproof": 1}
        assert papers["P1"]["progress"]["pct"] == 47      # (1 + 2/3 + 1/3 + 0 + 1/3) / 5 → 46.7
        assert papers["P1"]["points"][0]["colour"] in ("red", "yellow", "blue", "green")

    def test_advance_wraps_round(self):
        _import()
        db = _db()
        pid = db.query(models.SyllabusPoint).filter(models.SyllabusPoint.topic_code == "1.4").first().id
        db.close()
        seen = [client.post(f"/syllabus/{pid}/advance").json()["status"] for _ in range(4)]
        assert seen == ["learned", "revised", "bulletproof", "not_started"]
        db = _db()
        p = db.query(models.SyllabusPoint).filter(models.SyllabusPoint.id == pid).first()
        db.close()
        assert p.last_revised == TODAY                      # set when it passed through revised
        assert client.get("/changes").json()[0]["table"] == "syllabus_points"

    def test_patch(self):
        _import()
        db = _db()
        pid = db.query(models.SyllabusPoint).filter(models.SyllabusPoint.topic_code == "1.4").first().id
        db.close()
        r = client.patch(f"/syllabus/{pid}", json={"status": "revised", "past_paper_score": 66})
        assert r.json()["status"] == "revised" and r.json()["past_paper_score"] == 66
        assert client.patch(f"/syllabus/{pid}", json={"status": "purple"}).status_code == 400
        assert client.patch("/syllabus/99999", json={"status": "revised"}).status_code == 404


# ════════════════════════════════════════════════════════════════
# 3. update_syllabus tool (acceptance test 2)
# ════════════════════════════════════════════════════════════════

class TestUpdateSyllabusTool:

    def test_revised_s1_probability_sets_status_date_and_note(self):
        _import()
        db = _db()
        result, summary = ot.run_tool("update_syllabus", {
            "topic": "probability", "paper": "S1", "status": "revised",
            "weakness_note": "still shaky on conditional probability",
        }, db)
        db.close()
        assert result["matched"] is True and result["topic_code"] == "5.3"
        assert result["status"] == "revised"
        assert result["last_revised"] == TODAY.isoformat()
        assert "still shaky on conditional probability" in result["weakness_notes"]
        assert "Conditional probability" in result["weakness_notes"]   # previous note kept
        assert "→ revised" in summary and "note added" in summary
        db = _db()
        p = db.query(models.SyllabusPoint).filter(models.SyllabusPoint.topic_code == "5.3").first()
        assert p.status == "revised" and p.last_revised == TODAY
        change = db.query(models.ChangeLog).order_by(models.ChangeLog.id.desc()).first()
        db.close()
        assert change.table_name == "syllabus_points" and change.action == "update"

    def test_match_by_code(self):
        _import()
        db = _db()
        result, _ = ot.run_tool("update_syllabus", {"topic": "1.3", "status": "bulletproof"}, db)
        db.close()
        assert result["matched"] and result["title"] == "Coordinate geometry" and result["status"] == "bulletproof"

    def test_ambiguous_returns_candidates_and_writes_nothing(self):
        db = _db()
        for code, title in (("2.1", "Probability basics"), ("2.2", "Probability trees")):
            db.add(models.SyllabusPoint(subject_code="9709", subject_name="Maths 9709", paper="S1",
                                        topic_code=code, title=title, status="learned"))
        db.commit()
        result, summary = ot.run_tool("update_syllabus", {"topic": "probability", "status": "revised"}, db)
        assert result["matched"] is False and result["ambiguous"] is True and len(result["candidates"]) == 2
        assert db.query(models.SyllabusPoint).filter(models.SyllabusPoint.status == "revised").count() == 0
        db.close()

    def test_no_match(self):
        _import()
        db = _db()
        result, summary = ot.run_tool("update_syllabus", {"topic": "quantum chromodynamics"}, db)
        db.close()
        assert result["matched"] is False and result["candidates"] == []


# ════════════════════════════════════════════════════════════════
# 4. Weakest topics + alerts
# ════════════════════════════════════════════════════════════════

class TestWeakestAndAlerts:

    def test_ranking_uses_status_staleness_notes_and_exam(self):
        _seed_subject(exam=TODAY + timedelta(days=20))
        _import()
        db = _db()
        rows = syllabus.weakest_topics(db, limit=10)
        db.close()
        statuses = [r["status"] for r in rows]
        assert "bulletproof" not in statuses
        assert statuses[0] in ("not_started", "learned")
        # a never-revised red topic outranks a recently revised blue one
        codes = [r["topic_code"] for r in rows]
        assert codes.index("1.4") < codes.index("1.2")
        assert all("exam in" in r["reason"] for r in rows)

    def test_exam_proximity_lifts_a_subject(self):
        db = _db()
        for code, subj, exam in (("9709", "Maths 9709", TODAY + timedelta(days=200)), ("9708", "Economics 9708", TODAY + timedelta(days=10))):
            db.add(models.Subject(name=subj, code=code, exam_date=exam))
            db.add(models.SyllabusPoint(subject_code=code, subject_name=subj, paper=None, topic_code="1.1",
                                        title="Topic", status="not_started"))
        db.commit()
        rows = syllabus.weakest_topics(db, limit=2)
        db.close()
        assert rows[0]["subject_code"] == "9708"

    def test_syllabus_alert_rule(self):
        rules = {r["id"]: r for r in ot._load_rules()}
        assert "backtest_stalled" not in rules and "zero_mastery_high_weight" not in rules
        assert rules["habit_drift_7d"]["check"] == "non_negotiable_completion_below"
        assert rules["syllabus_weak_before_exam"]["check"] == "syllabus_weak_before_exam"
        db = _db()
        assert ot._check_syllabus_weak_before_exam(db, {"days": 60}) is False
        db.add(models.Subject(name="Maths 9709", code="9709", exam_date=TODAY + timedelta(days=30)))
        db.add(models.SyllabusPoint(subject_code="9709", subject_name="Maths 9709", topic_code="1.4",
                                    title="Circular measure", status="not_started"))
        db.commit()
        assert ot._check_syllabus_weak_before_exam(db, {"days": 60}) is True
        db.close()

    def test_non_negotiable_rule_uses_active_keys(self):
        db = _db()
        db.add(models.NonNegotiable(key="a", label="A", is_active=True))
        db.add(models.NonNegotiable(key="b", label="B", is_active=False))
        for i in range(3):
            d = TODAY - timedelta(days=i)
            db.add(models.Habit(key="a", label="A", date=d, done=False))
            db.add(models.Habit(key="b", label="B", date=d, done=True))   # inactive — ignored
        db.commit()
        assert ot._check_non_negotiable_completion_below(db, {"threshold_pct": 60, "window_days": 7}) is True
        for h in db.query(models.Habit).filter(models.Habit.key == "a").all():
            h.done = True
        db.commit()
        assert ot._check_non_negotiable_completion_below(db, {"threshold_pct": 60, "window_days": 7}) is False
        db.close()


# ════════════════════════════════════════════════════════════════
# 5. Daily briefing (acceptance test 8)
# ════════════════════════════════════════════════════════════════

def _seed_world():
    db = _db()
    db.add(models.Subject(name="Maths 9709", code="9709", exam_date=TODAY + timedelta(days=25)))
    db.add(models.Todo(text="Finalise Bel Baby delivery", priority=1, category="business",
                       due=str(TODAY - timedelta(days=2)), due_date=TODAY - timedelta(days=2), source="manual"))
    db.add(models.Todo(text="Economics P1 prep", priority=3, category="academics",
                       due=str(TODAY), due_date=TODAY, source="manual"))
    db.add(models.Todo(text="10 client outreaches", priority=2, category="business",
                       due=str(TODAY + timedelta(days=3)), due_date=TODAY + timedelta(days=3), source="manual"))
    db.add(models.Todo(text="Someday thing", priority=5, category="personal",
                       due=str(TODAY + timedelta(days=60)), due_date=TODAY + timedelta(days=60), source="manual"))
    db.add(models.Todo(text="Already done", priority=1, category="personal", due=str(TODAY), due_date=TODAY, done=True))
    db.add(models.NonNegotiable(key="scripture_prayer", label="Scripture & Prayer", is_active=True, sort_order=0))
    db.add(models.NonNegotiable(key="ironing", label="Ironing", is_active=True, sort_order=1))
    db.add(models.Habit(key="scripture_prayer", label="Scripture & Prayer", date=TODAY, done=True))
    db.add(models.SyllabusPoint(subject_code="9709", subject_name="Maths 9709", paper="P1", topic_code="1.4",
                                title="Circular measure", status="not_started"))
    db.add(models.SyllabusPoint(subject_code="9709", subject_name="Maths 9709", paper="P1", topic_code="1.1",
                                title="Quadratics", status="bulletproof", last_revised=TODAY))
    db.add(models.SyllabusPoint(subject_code="9709", subject_name="Maths 9709", paper="S1", topic_code="5.3",
                                title="Probability", status="learned", weakness_notes="- 2026-10-01: Conditional probability"))
    db.add(models.AnnualTarget(name="Revenue 10K", current_value=2600, target_value=10000, is_active=True))
    db.add(models.AnnualTarget(name="Dead target", current_value=0, target_value=100, is_active=True))
    db.add(models.Revenue(amount=2600, currency="USD", source="x", date=TODAY))
    db.commit()
    db.close()


class TestBriefing:

    def test_briefing_ranked_with_no_key(self):
        _seed_world()
        r = client.get("/briefing/today")
        assert r.status_code == 200
        b = r.json()
        assert b["summary"] is None and b["summary_source"] == "deterministic"
        assert b["greeting"] in ("Good morning", "Good afternoon", "Good evening")
        assert b["badge"] in ("EXECUTION DAY", "BUILD DAY", "REVIEW DAY")
        assert b["date"] == TODAY.isoformat()
        items = b["items"]
        assert items and items == sorted(items, key=lambda i: i["score"], reverse=True)
        for it in items:
            for key in ("id", "title", "category", "due_label", "reason", "suggested_action", "target_type", "target_id", "completable"):
                assert key in it
        assert items[0]["title"] == "Finalise Bel Baby delivery" and items[0]["category"] == "task"
        assert items[0]["due_label"] == "2d overdue" and items[0]["completable"] is True
        titles = [i["title"] for i in items]
        assert "Economics P1 prep" in titles and "10 client outreaches" in titles
        assert "Someday thing" not in titles and "Already done" not in titles
        assert "Ironing" in titles and "Scripture & Prayer" not in titles
        assert any(i["category"] == "exam" for i in items)
        syl = [i for i in items if i["category"] == "syllabus"]
        assert syl and all(i["status"] in ("not_started", "learned") for i in syl)
        assert any(i["category"] == "target" and i["title"] == "Dead target" for i in items)
        assert any(i["category"] == "alert" for i in items)

    def test_progress_counts_and_greeting_line(self):
        _seed_world()
        b = client.get("/briefing/today").json()
        assert b["progress"]["academics"] == 44          # (0 + 1 + 1/3) / 3
        assert b["progress"]["business"] == 26
        assert b["progress"]["targets"] == 13            # (26 + 0) / 2
        assert b["progress"]["habits"] == 50
        assert b["counts"]["overdue"] == 1 and b["counts"]["today"] == 1 and b["counts"]["upcoming"] == 1
        assert b["alert_count"] >= 1
        assert b["attention_count"] >= 2
        month = TODAY.strftime("%B")
        assert b["greeting_line"].startswith(f"You're 13% through your {month} targets.")
        assert "need your attention" in b["greeting_line"] or "needs your attention" in b["greeting_line"]
        assert b["weakness_notes"][0]["note"] == "Conditional probability"
        assert b["exams"][0]["subject_code"] == "9709"

    def test_due_labels(self):
        assert briefing._due_label(TODAY, TODAY) == "Today"
        assert briefing._due_label(TODAY + timedelta(days=1), TODAY) == "Tomorrow"
        assert briefing._due_label(TODAY - timedelta(days=1), TODAY) == "Yesterday"
        assert briefing._due_label(date(2026, 11, 7), date(2026, 11, 4)) == "Due 7 Nov"
        assert briefing._due_label(None, TODAY, "19:00") == "19:00"

    def test_summary_is_one_call_per_day_and_cached(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        _seed_world()
        with patch("llm.chat_text", return_value="Two things matter today. Ship the delivery. Then revise probability.") as chat:
            first = client.get("/briefing/today").json()
            second = client.get("/briefing/today").json()
            assert chat.call_count == 1
            assert first["summary_cached"] is False and second["summary_cached"] is True
            assert first["summary_source"] == "llm" and second["summary"] == first["summary"]
            assert first["greeting_line"] == "Two things matter today."
            third = client.get("/briefing/today", params={"refresh": 1}).json()
            assert chat.call_count == 2 and third["summary_cached"] is False
        db = _db()
        assert db.query(models.BriefingSummary).count() == 1
        db.close()

    def test_summary_failure_falls_back_cleanly(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        _seed_world()
        with patch("llm.chat_text", side_effect=RuntimeError("api down")):
            b = client.get("/briefing/today").json()
        assert b["summary"] is None and b["greeting_line"].startswith("You're")

    def test_empty_system_briefing(self):
        b = client.get("/briefing/today").json()
        assert b["items"] == [] or all("id" in i for i in b["items"])
        assert b["progress"]["academics"] is None
        assert "attention" in b["greeting_line"]
