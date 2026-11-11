"""
Phase 6A — applications acceptance tests.

  1. import_requirements: dates need a phrase from the pasted text; a document deadline is
     marked as such; a user's deadline is never overwritten; re-import upserts; batch undo
  2. add_application resolves a spoken deadline; an unknown deadline is reported as unknown
  3. advance cycles; make_todo is dated and idempotent; completing the task leaves the
     requirement alone
  7. m019 is in the startup chain

The model is patched at ``llm.chat_text``. No network.
"""

import json
import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import applications
import llm
import models
import orchestrator_tools as ot
from tests.conftest import TestSessionLocal, client

TODAY = date.today()

PASTED = """Huntsman Program — application requirements (synthetic fixture)

Applications are due by 1 January 2027. Decisions are released on 1 April 2027.

1. Personal essay, 650 words. Submit by 1 January 2027.
2. Huntsman essay on a global issue, 500 words. Submit by 1 January 2027.
3. SAT or ACT scores. Send scores by 15 December 2026.
4. School transcript from your counsellor. Send by 15 December 2026.
5. Two teacher recommendations, due 20 December 2026.
6. Application fee of 75 dollars, paid by 1 January 2027.
7. Alumni interview, offered after you submit.
"""

SEVEN = {
    "requirements": [
        {"title": "Personal essay", "kind": "essay", "details": "650 words",
         "due_date": "2027-01-01", "due_text": "Submit by 1 January 2027"},
        {"title": "Huntsman essay", "kind": "essay", "details": "A global issue, 500 words",
         "due_date": "2027-01-01", "due_text": "Submit by 1 January 2027"},
        {"title": "SAT or ACT scores", "kind": "test", "details": None,
         "due_date": "2026-12-15", "due_text": "Send scores by 15 December 2026"},
        {"title": "School transcript", "kind": "transcript", "details": "From your counsellor",
         "due_date": "2026-12-15", "due_text": "Send by 15 December 2026"},
        {"title": "Teacher recommendations", "kind": "recommendation", "details": "Two teachers",
         "due_date": "2026-12-20", "due_text": "due 20 December 2026"},
        {"title": "Application fee", "kind": "fee", "details": "75 dollars",
         "due_date": "2027-01-01", "due_text": "paid by 1 January 2027"},
        # The model supplies a date with no phrase behind it: it must be dropped.
        {"title": "Alumni interview", "kind": "interview", "details": "Offered after you submit",
         "due_date": "2027-02-15", "due_text": None},
    ],
    "deadline": "2027-01-01", "deadline_text": "Applications are due by 1 January 2027",
    "decision_date": "2027-04-01", "decision_text": "Decisions are released on 1 April 2027",
}


@pytest.fixture(autouse=True)
def _no_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    llm.reset_client()
    yield
    llm.reset_client()


def _db():
    return TestSessionLocal()


def _app(name="Wharton", **body):
    made = client.post("/applications", json={"name": name, **body})
    assert made.status_code == 201, made.text
    return made.json()


def _import(app_id, answer=SEVEN, text=PASTED):
    with patch("llm.chat_text", return_value=json.dumps(answer)) as chat:
        resp = client.post(f"/applications/{app_id}/requirements/import", json={"text": text})
    return resp, chat


# ── 1. import ─────────────────────────────────────────────────

def test_import_keeps_only_dates_the_text_states_and_marks_the_deadline_source():
    app = _app()
    assert app["deadline"] is None and app["deadline_source"] == "unknown" and app["status"] == "researching"

    resp, chat = _import(app["id"])
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert (out["created"], out["updated"], out["deadline_set"], out["dates_dropped"]) == (7, 0, True, 1)
    assert out["deadline"] == "2027-01-01" and out["deadline_source"] == "document"
    kwargs = chat.call_args.kwargs
    assert kwargs["purpose"] == "application_parse" and kwargs["max_tokens"] == 1200
    assert kwargs["response_format"] == {"type": "json_object"}
    messages = chat.call_args.args[0]
    assert messages[0]["content"] == applications.IMPORT_PROMPT and "Never supply a date" in messages[0]["content"]

    detail = client.get(f"/applications/{app['id']}").json()
    reqs = {r["title"]: r for r in detail["requirements"]}
    assert len(reqs) == 7
    dated = {t: r["due_date"] for t, r in reqs.items() if r["due_date"]}
    assert len(dated) == 6 and reqs["Alumni interview"]["due_date"] is None        # no phrase, no date
    assert dated["SAT or ACT scores"] == "2026-12-15" and reqs["SAT or ACT scores"]["due_source"] == "document"
    assert reqs["Personal essay"]["details"] == "650 words" and reqs["Personal essay"]["status"] == "missing"
    assert detail["deadline"] == "2027-01-01" and detail["deadline_source"] == "document"
    assert detail["decision_date"] == "2027-04-01" and detail["status"] == "preparing"
    assert detail["timeline"]["requirements"] == {"missing": 7, "in_progress": 0, "ready": 0, "submitted": 0}

    # Undo-all removes every requirement and the deadline.
    assert len({c["batch_id"] for c in out["changes"]}) == 1 and len(out["changes"]) == 8
    assert client.post(f"/changes/batch/{out['batch_id']}/undo").status_code == 200
    after = client.get(f"/applications/{app['id']}").json()
    assert after["requirements"] == [] and after["deadline"] is None and after["deadline_source"] == "unknown"


def test_a_date_whose_phrase_is_not_in_the_text_is_dropped():
    app = _app()
    invented = {"requirements": [
        {"title": "Portfolio", "kind": "portfolio", "details": None,
         "due_date": "2026-11-30", "due_text": "Portfolios are due 30 November"},      # not in the pasted text
        {"title": "Form", "kind": "form", "details": None, "due_date": "not a date", "due_text": "Send by 15 December 2026"},
    ], "deadline": "2026-12-01", "deadline_text": "The usual early deadline is 1 December"}
    resp, _ = _import(app["id"], invented)
    out = resp.json()
    assert out["created"] == 2 and out["dates_dropped"] == 2 and out["deadline_set"] is False
    detail = client.get(f"/applications/{app['id']}").json()
    assert all(r["due_date"] is None for r in detail["requirements"])
    assert detail["deadline"] is None and detail["deadline_source"] == "unknown"


def test_a_user_deadline_survives_an_import_and_reimport_upserts():
    stated = TODAY + timedelta(days=200)
    app = _app(deadline=stated.isoformat())
    assert app["deadline_source"] == "user"

    first, _ = _import(app["id"])
    assert first.json()["deadline_set"] is False                    # the document does not overrule him
    detail = client.get(f"/applications/{app['id']}").json()
    assert detail["deadline"] == stated.isoformat() and detail["deadline_source"] == "user"

    changed = json.loads(json.dumps(SEVEN))
    changed["requirements"][0]["details"] = "650 words, one essay"
    changed["requirements"].append({"title": "Financial aid form", "kind": "form", "details": None,
                                    "due_date": None, "due_text": None})
    again, _ = _import(app["id"], changed)
    out = again.json()
    assert (out["created"], out["updated"]) == (1, 7)               # upsert by title: no duplicates
    detail = client.get(f"/applications/{app['id']}").json()
    titles = [r["title"] for r in detail["requirements"]]
    assert len(titles) == 8 and len(set(titles)) == 8
    assert next(r for r in detail["requirements"] if r["title"] == "Personal essay")["details"] == "650 words, one essay"

    # A date he set himself on a requirement also survives a later import.
    essay = next(r for r in detail["requirements"] if r["title"] == "Huntsman essay")
    mine = (TODAY + timedelta(days=30)).isoformat()
    client.patch(f"/requirements/{essay['id']}", json={"due_date": mine})
    _import(app["id"])
    kept = next(r for r in client.get(f"/applications/{app['id']}").json()["requirements"] if r["id"] == essay["id"])
    assert kept["due_date"] == mine and kept["due_source"] == "user"


def test_import_failures_write_nothing():
    app = _app()
    with patch("llm.chat_text", return_value="not json"):
        assert client.post(f"/applications/{app['id']}/requirements/import", json={"text": PASTED}).status_code == 400
    assert client.post(f"/applications/{app['id']}/requirements/import", json={"text": "short"}).status_code == 400
    assert client.post(f"/applications/{app['id']}/requirements/import", json={"text": PASTED}).status_code == 503   # no key
    assert client.post("/applications/9999/requirements/import", json={"text": PASTED}).status_code == 404
    assert client.get(f"/applications/{app['id']}").json()["requirements"] == []
    db = _db()
    result, _ = ot.run_tool("import_requirements", {"application": "Wharton", "text": PASTED}, db)
    assert "AI key" in result["error"]
    db.close()


# ── 2. deadlines come from the user or are unknown ────────────

def test_add_application_resolves_a_spoken_deadline_and_reports_unknown():
    db = _db()
    made, summary = ot.run_tool("add_application", {"name": "Wharton", "institution": "University of Pennsylvania",
                                                    "programme": "Huntsman Program", "deadline": "1 January"}, db)
    next_jan = date(TODAY.year + 1, 1, 1)
    assert made["deadline"] == next_jan and made["deadline_source"] == "user"
    assert summary == f"application 'Wharton' added, deadline {next_jan.isoformat()}"
    assert "Bandile stated it" in made["deadline_note"]

    again, summary = ot.run_tool("add_application", {"name": "wharton", "priority": 1}, db)
    assert again["application_id"] == made["application_id"] and "updated" in summary
    assert db.query(models.Application).count() == 1

    other, _ = ot.run_tool("add_application", {"name": "Mastercard Foundation Scholarship", "kind": "scholarship"}, db)
    status, summary = ot.run_tool("application_status", {"application": "Mastercard"}, db)
    assert status["deadline"] is None and status["deadline_source"] == "unknown"
    assert status["days_to_deadline"] is None and "Do not state a date" in status["deadline_note"]
    assert summary == "Mastercard Foundation Scholarship: 0 missing, deadline unknown"
    shown = json.loads(ot.for_model("application_status", status))            # what the model actually reads
    assert shown["deadline source"] == "unknown" and "ask him" in shown["deadline note"]

    everything, summary = ot.run_tool("application_status", {}, db)
    assert [a["name"] for a in everything["applications"]] == ["Wharton", "Mastercard Foundation Scholarship"]
    bad, _ = ot.run_tool("add_application", {"name": "X", "deadline": "sometime"}, db)
    assert "error" in bad
    nobody, _ = ot.run_tool("application_status", {"application": "Oxford"}, db)
    assert "error" in nobody
    db.close()

    patched = client.patch(f"/applications/{other['application_id']}", json={"deadline": "2027-03-01", "status": "preparing"})
    assert patched.json()["deadline_source"] == "user" and patched.json()["status"] == "preparing"
    assert client.patch(f"/applications/{other['application_id']}", json={"status": "daydreaming"}).status_code == 400
    listed = client.get("/applications").json()
    assert [a["name"] for a in listed] == ["Wharton", "Mastercard Foundation Scholarship"]     # by deadline
    assert client.get("/applications", params={"status": "preparing"}).json()[0]["name"].startswith("Mastercard")


# ── 3. advance, tasks ─────────────────────────────────────────

def test_advance_cycles_and_make_todo_is_dated_and_idempotent():
    deadline = TODAY + timedelta(days=40)
    app = _app(deadline=deadline.isoformat())
    dated = client.post(f"/applications/{app['id']}/requirements",
                        json={"title": "Personal essay", "kind": "essay", "due_date": (TODAY + timedelta(days=10)).isoformat()})
    undated = client.post(f"/applications/{app['id']}/requirements", json={"title": "Transcript", "kind": "transcript"})
    assert dated.status_code == 201 and dated.json()["due_source"] == "user"
    rid, uid = dated.json()["id"], undated.json()["id"]

    seen = [client.post(f"/requirements/{rid}/advance").json()["status"] for _ in range(5)]
    assert seen == ["in_progress", "ready", "submitted", "missing", "in_progress"]
    assert client.patch(f"/requirements/{rid}", json={"status": "not_required"}).json()["status"] == "not_required"
    assert client.post(f"/requirements/{rid}/advance").json()["status"] == "missing"
    assert client.patch(f"/requirements/{rid}", json={"status": "done"}).status_code == 400

    first = client.post(f"/requirements/{rid}/todo").json()
    assert first["existing"] is False and first["todo"]["text"] == "Wharton: Personal essay"
    assert first["todo"]["due_date"] == (TODAY + timedelta(days=10)).isoformat() and first["todo"]["category"] == "applications"
    second = client.post(f"/requirements/{rid}/todo").json()
    assert second["existing"] is True and second["todo"]["id"] == first["todo"]["id"]
    from_deadline = client.post(f"/requirements/{uid}/todo").json()
    assert from_deadline["todo"]["due_date"] == (deadline - timedelta(days=7)).isoformat()     # a week before the deadline

    db = _db()
    assert db.query(models.Todo).count() == 2
    done, _ = ot.run_tool("complete_task", {"todo_id": first["todo"]["id"]}, db)
    assert done["completed"] is True
    req = db.query(models.ApplicationRequirement).filter(models.ApplicationRequirement.id == rid).one()
    assert req.status == "missing"                                  # statuses are set by Bandile, not by the task
    db.close()
    third = client.post(f"/requirements/{rid}/todo").json()         # the old task is done: a new one is fine
    assert third["existing"] is False and third["todo"]["id"] != first["todo"]["id"]

    t = client.get(f"/applications/{app['id']}").json()["timeline"]
    assert t["days_to_deadline"] == 40 and t["at_risk"] is True     # the essay is due in ten days
    assert t["next_due"]["title"] == "Personal essay" and t["next_due"]["days"] == 10
    assert client.post("/requirements/9999/advance").status_code == 404


def test_update_requirement_tool_picks_by_title_and_never_guesses():
    db = _db()
    ot.run_tool("add_application", {"name": "Wharton"}, db)
    app = db.query(models.Application).one()
    for title in ("Personal essay", "Huntsman essay", "School transcript"):
        applications.add_requirement(db, app, title)
    db.commit()

    moved, summary = ot.run_tool("update_requirement", {"application": "Wharton", "requirement": "transcript",
                                                        "status": "ready", "due_date": "in 3 weeks"}, db)
    assert moved["status"] == "ready" and moved["due_date"] == TODAY + timedelta(days=21)
    assert summary == "Wharton: School transcript — ready"
    tasked, summary = ot.run_tool("update_requirement", {"application": "Wharton", "requirement": "personal essay",
                                                         "status": "in_progress", "make_todo": True}, db)
    assert tasked["todo"]["text"] == "Wharton: Personal essay" and "(task added)" in summary
    vague, _ = ot.run_tool("update_requirement", {"application": "Wharton", "requirement": "essay", "status": "ready"}, db)
    assert vague["ambiguous"] is True and len(vague["candidates"]) == 2
    missing, _ = ot.run_tool("update_requirement", {"application": "Wharton", "requirement": "visa"}, db)
    assert "error" in missing and "Personal essay" in missing["requirements"]
    status, _ = ot.run_tool("application_status", {"application": "Wharton"}, db)
    assert status["missing"] == ["Huntsman essay"] and status["in_progress"] == ["Personal essay"]
    assert status["requirement_counts"]["ready"] == 1
    db.close()


def test_gaps_endpoint_returns_stored_open_gaps():
    app = _app()
    db = _db()
    db.add_all([
        models.ApplicationGap(application_id=app["id"], gap="No leadership evidence", severity="medium", status="open"),
        models.ApplicationGap(application_id=app["id"], gap="Maths predicted grade", severity="high", status="open"),
        models.ApplicationGap(application_id=app["id"], gap="An old one", severity="high", status="superseded"),
    ])
    db.commit()
    db.close()
    gaps = client.get(f"/applications/{app['id']}/gaps").json()["gaps"]
    assert [g["gap"] for g in gaps] == ["Maths predicted grade", "No leadership evidence"]      # high first, open only
    done = client.patch(f"/gaps/{gaps[0]['id']}", json={"status": "addressed"}).json()
    assert done["status"] == "addressed" and done["change"]["table"] == "application_gaps"
    assert client.patch(f"/gaps/{gaps[1]['id']}", json={"status": "superseded"}).status_code == 400
    assert client.get(f"/applications/{app['id']}").json()["gaps_open"] == 1


def test_applications_are_deletable_through_confirmation():
    import actions
    app = _app()
    assert "applications" in actions.DELETABLE and "application_requirements" in actions.DELETABLE
    db = _db()
    pending, _ = ot.run_tool("propose_delete", {"table": "applications", "record_id": app["id"]}, db)
    assert pending["requires_confirmation"] is True and db.query(models.Application).count() == 1
    db.close()


# ── 7. migration ──────────────────────────────────────────────

def test_m019_is_registered_before_the_schema_check():
    source = (Path(__file__).parent.parent / "main.py").read_text(encoding="utf-8")
    assert "run_m019(db)" in source and source.index("run_m019(db)") < source.index("_validate_schema(engine)   #")
    from migrations import m019_applications
    assert set(m019_applications._TABLES) == {"applications", "application_requirements", "application_gaps"}
