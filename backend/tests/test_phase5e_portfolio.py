"""
Phase 5E — project portfolio acceptance tests.

  1. attention components and recommendations on a five-project fixture
  2. one pause suggestion per pause recommendation; accepting pauses; undo resumes
  3. the assistant cannot pause without `explicit`
  4. a blocker sets the reason and red health; unblocked clears it
  5. Focus demotes other priority-1 projects in one batch; batch undo restores
  6. briefing items (blocked 62, overdue ≥ 70, focus 45) and the projects_blocked alert
  7. waiting-on-client comes from the last communication's direction and age

The module makes no model calls. No network.
"""

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import llm
import models
import orchestrator_tools as ot
import portfolio
from tests.conftest import TestSessionLocal, client

TODAY = date.today()
NOW = datetime.utcnow()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ZMW_PER_USD", "27")
    llm.reset_client()
    yield
    llm.reset_client()


def _db():
    return TestSessionLocal()


def _project(db, title, **kw):
    kw.setdefault("status", "active")
    kw.setdefault("kind", "internal")
    kw.setdefault("priority", 3)
    kw.setdefault("last_activity_at", NOW)
    p = models.Project(title=title, **kw)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _five(db):
    clinic = models.Client(name="Hope Clinic", status="active")
    db.add(clinic)
    db.commit()
    return {
        "blocked": _project(db, "Clinic portal", kind="client", client_id=clinic.id, priority=2,
                            expected_value=27000, currency="ZMW", due_date=TODAY - timedelta(days=4),
                            blocked_reason="waiting on the logo", last_activity_at=NOW - timedelta(days=2)),
        "soon": _project(db, "Bakery ordering", expected_value=6000, currency="USD",
                         due_date=TODAY + timedelta(days=5)),
        "stale": _project(db, "Newsletter", priority=4, expected_value=2700, currency="ZMW",
                          last_activity_at=NOW - timedelta(days=20)),
        "paused": _project(db, "Side app", status="paused"),
        "delivered": _project(db, "Salon site", status="delivered", delivered_at=NOW - timedelta(days=40),
                              last_activity_at=NOW - timedelta(days=40)),
    }


def _expected(value_usd, urgency, staleness, blocked, priority, waiting=0):
    """The scoring formula, written out here rather than taken from the module."""
    value_w = round(min(1.0, value_usd / 5000) * 3, 2)
    pri = round((6 - priority) / 5 * 2, 2)
    return round(value_w + urgency + staleness + blocked + pri + waiting, 2), value_w, pri


# ── 1. scores and recommendations ─────────────────────────────

def test_attention_scores_and_recommendations_on_five_projects():
    db = _db()
    p = _five(db)
    db.close()
    review = client.get("/portfolio").json()
    by_title = {e["project"]["title"]: e for e in review["ranked"] + review["paused"]}

    score, value_w, pri = _expected(27000 / 27, urgency=4, staleness=0, blocked=2, priority=2)
    e = by_title["Clinic portal"]
    assert e["attention"]["score"] == score == 8.2
    assert e["attention"]["components"] == {"value": value_w, "urgency": 4, "staleness": 0, "blocked": 2,
                                            "priority": pri, "waiting": 0}
    assert e["bottleneck"] == "blocked: waiting on the logo"          # blocked outranks overdue
    assert (e["rank"], e["recommendation"], e["project"]["health"]) == (1, "focus", "red")

    score, value_w, pri = _expected(6000, urgency=3, staleness=0, blocked=0, priority=3)
    e = by_title["Bakery ordering"]
    assert e["attention"]["score"] == score == 7.2 and e["attention"]["components"]["value"] == 3.0
    assert (e["rank"], e["recommendation"], e["bottleneck"], e["project"]["health"]) == (2, "focus", None, "green")

    score, value_w, pri = _expected(2700 / 27, urgency=0, staleness=2, blocked=0, priority=4)
    e = by_title["Newsletter"]
    assert e["attention"]["score"] == score and value_w == 0.06
    assert (e["recommendation"], e["rank"], e["bottleneck"]) == ("pause", None, "no activity for 20 days")
    assert e["project"]["health"] == "amber"

    e = by_title["Side app"]
    assert e["attention"]["score"] == _expected(0, 0, 0, 0, 3)[0] == 1.2
    assert e["recommendation"] == "maintain" and e in review["paused"]

    e = by_title["Salon site"]
    assert e["attention"]["score"] == _expected(0, 0, 2, 0, 3)[0]
    assert e["recommendation"] == "close"

    recs = sorted(x["recommendation"] for x in by_title.values())
    assert recs == ["close", "focus", "focus", "maintain", "pause"]
    assert [x["project"]["title"] for x in review["ranked"]] == ["Clinic portal", "Bakery ordering",
                                                                 "Salon site", "Newsletter"]   # by score
    assert review["totals"] == {"active": 3, "paused": 1, "blocked": 1, "overdue": 1,
                                "value_usd_active": round(1000 + 6000 + 100, 2)}
    assert [b["title"] for b in review["bottlenecks"]] == ["Clinic portal", "Salon site", "Newsletter"]
    assert client.get("/portfolio", params={"include_paused": 0}).json()["paused"] == []

    db = _db()
    stored = {x.title: x.health for x in db.query(models.Project).all()}
    assert stored["Clinic portal"] == "red" and stored["Newsletter"] == "amber"   # health is stored
    db.close()


def test_urgency_and_staleness_bands():
    db = _db()
    cases = [(-1, 4), (0, 3), (7, 3), (8, 2), (14, 2), (15, 1), (30, 1), (31, 0)]
    for offset, urgency in cases:
        p = _project(db, f"due {offset}", due_date=TODAY + timedelta(days=offset))
        assert portfolio.attention(db, p)["components"]["urgency"] == urgency, offset
    for idle, staleness in [(7, 0), (8, 1), (14, 1), (15, 2)]:
        p = _project(db, f"idle {idle}", last_activity_at=NOW - timedelta(days=idle, minutes=5))
        assert portfolio.attention(db, p)["components"]["staleness"] == staleness, idle
    never = _project(db, "never touched", last_activity_at=None)
    att = portfolio.attention(db, never)
    assert att["components"]["staleness"] == 2 and att["idle_days"] == 30      # no activity counts as 30 days
    top = _project(db, "top priority", priority=1)
    low = _project(db, "low priority", priority=5)
    assert portfolio.attention(db, top)["components"]["priority"] == 2.0
    assert portfolio.attention(db, low)["components"]["priority"] == 0.4
    db.close()


# ── 2. pause suggestions ──────────────────────────────────────

def test_one_pause_suggestion_per_recommendation_accept_and_undo():
    db = _db()
    p = _five(db)
    stale_id = p["stale"].id
    db.close()

    first = client.get("/portfolio").json()
    second = client.get("/portfolio").json()
    assert len(first["pause_suggestions"]) == 1 and len(second["pause_suggestions"]) == 1
    pending = [s for s in client.get("/suggestions").json() if s["kind"] == "project_pause"]
    assert len(pending) == 1                                         # not one per call
    s = pending[0]
    assert s["payload"] == {"project_id": stale_id, "title": "Newsletter"}
    assert s["reason"] == "Stale for 20 days, low value, nothing due" and s["confidence"] == 0.8

    db = _db()
    assert db.query(models.Project).filter(models.Project.id == stale_id).one().status == "active"   # only suggested
    db.close()

    accepted = client.post(f"/suggestions/{s['id']}/accept").json()
    assert accepted["applied"] is True
    db = _db()
    project = db.query(models.Project).filter(models.Project.id == stale_id).one()
    assert project.status == "paused"
    update = db.query(models.ProjectUpdate).filter(models.ProjectUpdate.project_id == stale_id).one()
    assert (update.kind, update.text, update.source) == ("status", "active → paused", "suggestion")
    db.close()
    assert {c["table"] for c in accepted["changes"]} == {"projects", "project_updates"}

    assert client.get("/portfolio").json()["pause_suggestions"] == []       # paused: nothing left to suggest
    undone = client.post(f"/changes/batch/{accepted['changes'][0]['batch_id']}/undo")
    assert undone.status_code == 200
    db = _db()
    assert db.query(models.Project).filter(models.Project.id == stale_id).one().status == "active"
    assert db.query(models.ProjectUpdate).count() == 0
    db.close()


# ── 3. the assistant cannot pause on its own ──────────────────

def test_update_project_refuses_to_pause_without_explicit():
    db = _db()
    p = _project(db, "Newsletter")
    refused, summary = ot.run_tool("update_project", {"project": "Newsletter", "status": "paused"}, db)
    assert refused["error"] == "pausing needs a tap or an explicit 'pause X'" and summary == refused["error"]
    db.refresh(p)
    assert p.status == "active" and db.query(models.ChangeLog).count() == 0

    paused, summary = ot.run_tool("update_project", {"project": "Newsletter", "status": "paused", "explicit": True}, db)
    assert paused["status"] == "paused" and "paused" in summary
    db.refresh(p)
    assert p.status == "paused"
    db.close()


def test_update_project_creates_updates_and_never_guesses():
    db = _db()
    db.add(models.Client(name="Hope Clinic", status="active"))
    db.commit()
    made, summary = ot.run_tool("update_project", {"create": True, "title": "Clinic portal", "client": "Hope Clinic",
                                                   "priority": 1, "expected_value": 8000, "due_date": "in 2 weeks",
                                                   "next_action": "Send the wireframes"}, db)
    assert summary == "project 'Clinic portal' created — active"
    assert (made["kind"], made["client_name"], made["priority"]) == ("client", "Hope Clinic", 1)
    assert made["due_date"] == TODAY + timedelta(days=14) and made["currency"] == "ZMW"
    ot.run_tool("update_project", {"create": True, "title": "Clinic newsletter"}, db)

    moved, _ = ot.run_tool("update_project", {"project": "Clinic portal", "due_date": (TODAY + timedelta(days=30)).isoformat(),
                                              "blocked_reason": "waiting on the logo"}, db)
    assert moved["due_date"] == TODAY + timedelta(days=30) and moved["blocked_reason"] == "waiting on the logo"
    kinds = [u.kind for u in db.query(models.ProjectUpdate).all()]
    assert kinds == ["blocker"]
    cleared, _ = ot.run_tool("update_project", {"project": "Clinic portal", "blocked_reason": ""}, db)
    assert cleared["blocked_reason"] is None
    assert [u.kind for u in db.query(models.ProjectUpdate).order_by(models.ProjectUpdate.id).all()] == ["blocker", "unblocked"]

    vague, _ = ot.run_tool("update_project", {"project": "clinic", "priority": 2}, db)
    assert vague["ambiguous"] is True and len(vague["candidates"]) == 2
    missing, _ = ot.run_tool("update_project", {"project": "Quantum", "priority": 2}, db)
    assert "error" in missing
    untitled, _ = ot.run_tool("update_project", {"create": True}, db)
    assert untitled["error"] == "a project needs a title"
    db.close()


# ── 4. blockers ───────────────────────────────────────────────

def test_blocker_update_sets_reason_health_and_activity():
    db = _db()
    stale = NOW - timedelta(days=10)
    p = _project(db, "Clinic portal", last_activity_at=stale)

    blocked, summary = ot.run_tool("log_project_update", {"project": "Clinic portal", "kind": "blocker",
                                                          "text": "waiting on the clinic's logo"}, db)
    assert summary == "logged blocker on Clinic portal"
    assert blocked["blocked_reason"] == "waiting on the clinic's logo" and blocked["health"] == "red"
    db.refresh(p)
    assert p.blocked_reason == "waiting on the clinic's logo" and p.health == "red"
    assert p.last_activity_at > stale + timedelta(days=9)
    assert portfolio.attention(db, p)["bottleneck"] == "blocked: waiting on the clinic's logo"

    freed, _ = ot.run_tool("log_project_update", {"project": "Clinic portal", "kind": "unblocked", "text": "Logo arrived"}, db)
    assert freed["blocked_reason"] is None and freed["health"] == "green"
    note, _ = ot.run_tool("log_project_update", {"project": "Clinic portal", "text": "Shipped the booking page"}, db)
    assert note["kind"] == "note"
    assert [u.kind for u in db.query(models.ProjectUpdate).order_by(models.ProjectUpdate.id).all()] == ["blocker", "unblocked", "note"]
    empty, _ = ot.run_tool("log_project_update", {"project": "Clinic portal", "text": "  "}, db)
    assert "error" in empty
    pid = p.id
    db.close()

    detail = client.get(f"/projects/{pid}").json()
    assert [u["text"] for u in detail["updates"]][-1] == "waiting on the clinic's logo"
    assert detail["payments"] == [] and detail["communications"] == [] and "score" in detail["attention"]
    tapped = client.post(f"/projects/{pid}/updates", json={"text": "Demo booked", "kind": "milestone"})
    assert tapped.status_code == 201 and tapped.json()["kind"] == "milestone"
    assert client.get("/projects/9999").status_code == 404


# ── 5. focus ──────────────────────────────────────────────────

def test_focus_demotes_other_top_priorities_in_one_batch():
    db = _db()
    a = _project(db, "A", priority=1)
    b = _project(db, "B", priority=1)
    c = _project(db, "C", priority=3)
    paused = _project(db, "D", priority=1, status="paused")
    ids = (a.id, b.id, c.id, paused.id)
    db.close()

    out = client.post(f"/projects/{ids[2]}/focus").json()
    assert out["priority"] == 1
    assert len(out["changes"]) == 3 and len({ch["batch_id"] for ch in out["changes"]}) == 1

    def priorities():
        s = _db()
        try:
            return tuple(s.query(models.Project).filter(models.Project.id == i).one().priority for i in ids)
        finally:
            s.close()

    assert priorities() == (2, 2, 1, 1)                      # the paused project is left alone
    assert client.post(f"/changes/batch/{out['changes'][0]['batch_id']}/undo").status_code == 200
    assert priorities() == (1, 1, 3, 1)


def test_status_taps_and_project_endpoints():
    made = client.post("/projects", json={"title": "Founder OS Phase 5", "priority": 2, "expected_value": 500, "currency": "usd"})
    assert made.status_code == 201
    pid = made.json()["id"]
    assert (made.json()["kind"], made.json()["status"], made.json()["currency"]) == ("internal", "active", "USD")

    delivered = client.post(f"/projects/{pid}/status", json={"status": "delivered"}).json()
    assert delivered["status"] == "delivered" and delivered["delivered_at"]
    assert {c["table"] for c in delivered["changes"]} == {"projects", "project_updates"}
    assert client.post(f"/projects/{pid}/status", json={"status": "delivered"}).json()["changes"] == []   # no-op
    assert client.post(f"/projects/{pid}/status", json={"status": "cancelled"}).status_code == 400        # not a tap
    resumed = client.post(f"/projects/{pid}/status", json={"status": "active"}).json()
    assert resumed["status"] == "active"

    patched = client.patch(f"/projects/{pid}", json={"blocked_reason": "no hosting yet", "next_action": "Buy hosting"}).json()
    assert patched["blocked_reason"] == "no hosting yet" and patched["next_action"] == "Buy hosting"
    cleared = client.patch(f"/projects/{pid}", json={"blocked_reason": ""}).json()
    assert cleared["blocked_reason"] is None
    listed = client.get("/projects", params={"status": "active"}).json()
    assert [p["title"] for p in listed] == ["Founder OS Phase 5"] and listed[0]["attention"]["components"]["value"] == 0.3
    assert client.post("/projects", json={"title": " "}).status_code == 400
    assert client.post("/projects", json={"title": "X", "client_id": 9999}).status_code == 404


# ── 6. briefing and alert ─────────────────────────────────────

def test_briefing_items_for_blocked_overdue_waiting_and_focus():
    import briefing
    db = _db()
    clinic = models.Client(name="Hope Clinic", status="active")
    db.add(clinic)
    db.commit()
    blocked = _project(db, "Clinic portal", kind="client", client_id=clinic.id, blocked_reason="waiting on the logo").id
    overdue = _project(db, "Bakery ordering", due_date=TODAY - timedelta(days=3)).id
    very_late = _project(db, "Salon site", due_date=TODAY - timedelta(days=30)).id
    db.close()

    b = briefing.build_briefing(_db(), with_summary=False)
    items = {i["id"]: i for i in b["items"]}
    item = items[f"project:project:{blocked}"]
    assert item["score"] == 62 and item["reason"] == "Blocked: waiting on the logo"
    assert item["suggested_action"] == "Unblock or re-plan" and item["title"] == "Clinic portal — Hope Clinic"
    item = items[f"project:project:{overdue}"]
    assert item["score"] == 73 and item["due_label"] == "3d overdue" and item["suggested_action"] == "Deliver or re-date"
    assert items[f"project:project:{very_late}"]["score"] == 79             # 70 + min(days, 9)
    assert b["progress"]["projects"] == 0                                    # three active, none delivered


def test_briefing_focus_item_and_delivery_progress():
    import briefing
    db = _db()
    top = _project(db, "Founder OS Phase 5", priority=1, due_date=TODAY + timedelta(days=10),
                   next_action="Ship the importer").id
    _project(db, "Newsletter", priority=4)
    _project(db, "Old site", status="delivered", delivered_at=NOW - timedelta(days=5))
    db.close()

    b = briefing.build_briefing(_db(), with_summary=False)
    items = {i["id"]: i for i in b["items"]}
    focus = items[f"project:project:{top}"]
    assert (focus["score"], focus["due_label"]) == (45, "FOCUS")
    assert focus["reason"] == "Highest attention score" and focus["suggested_action"] == "Ship the importer"
    assert sum(1 for i in b["items"] if i["category"] == "project") == 1    # only the top project
    assert b["progress"]["projects"] == round(100 * 1 / 3)                   # one delivered, two active

    empty = briefing.build_briefing(_db(), with_summary=False)
    assert empty["progress"]["projects"] == 33


def test_projects_blocked_alert_fires_at_two():
    db = _db()

    def fired():
        alerts, _ = ot.detect_off_track(db)
        return any(a["rule_id"] == "projects_blocked" for a in alerts)

    a = _project(db, "A", blocked_reason="waiting on a client")
    b = _project(db, "B")
    _project(db, "C", blocked_reason="parked", status="paused")              # paused: not counted
    assert fired() is False
    b.blocked_reason = "no hosting"
    db.commit()
    assert fired() is True
    a.blocked_reason = None
    db.commit()
    assert fired() is False
    db.close()


# ── 7. waiting on the client ──────────────────────────────────

def test_waiting_on_client_uses_the_last_communication():
    db = _db()
    clinic = models.Client(name="Hope Clinic", status="active")
    db.add(clinic)
    db.commit()
    p = _project(db, "Clinic portal", kind="client", client_id=clinic.id)

    def comm(direction, days_ago):
        db.add(models.Communication(client_id=clinic.id, channel="whatsapp", direction=direction,
                                    summary="x", occurred_at=NOW - timedelta(days=days_ago)))
        db.commit()
        return portfolio.attention(db, p)

    assert portfolio.attention(db, p)["components"]["waiting"] == 0          # nothing logged
    att = comm("out", 9)
    assert att["components"]["waiting"] == 1 and att["bottleneck"] == "waiting on client for 9 days"
    assert portfolio.health_of(att) == "amber"
    assert comm("in", 4)["components"]["waiting"] == 0                        # they answered
    assert comm("out", 3)["components"]["waiting"] == 0                       # only three days ago
    internal = _project(db, "Internal tool")
    assert portfolio.attention(db, internal)["components"]["waiting"] == 0    # no client to wait on

    import briefing
    db.query(models.Communication).delete()
    db.add(models.Communication(client_id=clinic.id, channel="email", direction="out", summary="Sent the quote",
                                occurred_at=NOW - timedelta(days=12)))
    db.commit()
    b = briefing.build_briefing(_db(), with_summary=False)
    item = next(i for i in b["items"] if i["id"] == f"project:project:{p.id}")
    assert item["score"] == 36 and item["suggested_action"] == "Nudge Hope Clinic"
    assert item["reason"] == "Waiting on Hope Clinic for 12 days"
    db.close()


def test_overdue_payment_is_the_last_bottleneck():
    db = _db()
    clinic = models.Client(name="Hope Clinic", status="active")
    db.add(clinic)
    db.commit()
    p = _project(db, "Clinic portal", kind="client", client_id=clinic.id)
    db.add(models.Payment(client_id=clinic.id, amount=2000, currency="ZMW", status="overdue",
                          due_date=TODAY - timedelta(days=6)))
    db.commit()
    assert portfolio.attention(db, p)["bottleneck"] == "payment overdue (K2,000)"
    p.last_activity_at = NOW - timedelta(days=9)
    db.commit()
    assert portfolio.attention(db, p)["bottleneck"] == "no activity for 9 days"   # staleness comes first
    db.close()
