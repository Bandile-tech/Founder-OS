"""
Phase 5A — market intelligence v2 acceptance tests.

  1. every model call of a run is logged with a market_intel_* purpose; the project
     carries the run's cost and call count
  2. weighted scoring and the recommendation bands
  3. a repeated finding is not inserted twice (keyword fallback, no key)
  4. with embeddings, paraphrases dedupe at cosine >= 0.9 and not below
  5. dismiss (primitive, endpoint and tool): status, change recorded, undo restores `surfaced`
  6. standing objectives: create by tool, due on creation, Run stamps the objective and
     the project, due becomes false
  7. briefing: a fresh `pursue` finding at 35 until promoted or dismissed; stale and
     overdue pipeline entries; due objectives
  8. the pre-existing suite still passes (tests/test_market_intel.py)

No network and no OpenAI: the SDK client and the embedder are fakes.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import llm
import models
from market_intel import agent, scoring
from market_intel.agent import run_market_research
from tests.conftest import TestSessionLocal, client
from tests.test_market_intel import FAKE_EVIDENCE, _fake_llm, _run


@pytest.fixture(autouse=True)
def _no_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("RESEARCH_MODEL", raising=False)
    llm.reset_client()
    yield
    llm.reset_client()


def _db():
    return TestSessionLocal()


SCORES = {"pain_severity": 8, "frequency": 8, "financial_value": 7, "market_size": 6,
          "competition_gap": 7, "founder_fit": 9, "speed_to_mvp": 8}


# ── 1. usage logging and run cost ─────────────────────────────

class _FakeCompletions:
    """Stands in for ``client.chat.completions``: canned JSON per role plus a usage object."""

    def __init__(self):
        self.calls = []

    def create(self, *, model, messages, **kwargs):
        self.calls.append({"model": model, **kwargs})
        system = messages[0]["content"]
        content = json.dumps(_fake_llm(system, messages[1]["content"]))
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(prompt_tokens=4000, completion_tokens=800,
                                  prompt_tokens_details=SimpleNamespace(cached_tokens=0)),
        )


def test_every_llm_call_is_logged_and_costed(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    completions = _FakeCompletions()
    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    db = _db()
    # Usage rows are written through database.SessionLocal (db=None in the agent): point it
    # at the test database. Embeddings are stubbed so only the six role calls are counted.
    with patch("llm.get_client", return_value=fake_client), \
         patch("database.SessionLocal", TestSessionLocal), \
         patch("llm.embed_texts", side_effect=lambda texts, **_: [[1.0, 0.0] for _t in texts]), \
         patch("market_intel.adapters.search_all", return_value=list(FAKE_EVIDENCE)):
        result = run_market_research(db, "find painful problems in Zambian SMEs where AI helps")

    assert "error" not in result
    rows = db.query(models.LLMUsage).filter(models.LLMUsage.purpose.like("market_intel_%")).all()
    assert sorted(r.purpose for r in rows) == sorted([
        "market_intel_planner", "market_intel_researcher", "market_intel_analyst",
        "market_intel_advisor", "market_intel_verifier", "market_intel_lessons",
    ])
    assert all(c["model"] == llm.research_model() for c in completions.calls)
    assert all(c["response_format"] == {"type": "json_object"} for c in completions.calls)

    project = db.query(models.ResearchProject).filter(
        models.ResearchProject.id == result["project_id"]).first()
    assert project.llm_calls == 6
    assert project.cost_usd > 0
    assert result["llm_calls"] == 6 and result["cost_usd"] == project.cost_usd
    db.close()


def test_research_model_follows_env(monkeypatch):
    assert llm.research_model() == llm.assistant_model()
    monkeypatch.setenv("RESEARCH_MODEL", "some-cheaper-model")
    assert llm.research_model() == "some-cheaper-model"


def test_no_model_literal_left_in_the_agent():
    source = (Path(__file__).parent.parent / "market_intel" / "agent.py").read_text(encoding="utf-8")
    assert "gpt-4o-mini" not in source and "MODEL =" not in source


# ── 2. scoring ────────────────────────────────────────────────

def test_weighted_overall_and_recommendation_bands():
    w = scoring.DEFAULT_WEIGHTS
    expected = round(sum(SCORES[k] * w[k] for k in w) / sum(w.values()), 1)
    assert scoring.overall(SCORES) == expected
    assert expected != round(sum(SCORES.values()) / 7, 1)   # weighted, not the plain mean

    assert scoring.recommendation(7.5) == "pursue"
    assert scoring.recommendation(7.4) == "watch"
    assert scoring.recommendation(6.0) == "watch"
    assert scoring.recommendation(5.9) == "park"


def test_invalid_weights_file_falls_back_to_defaults(tmp_path):
    bad = tmp_path / "weights.json"
    bad.write_text("{not json", encoding="utf-8")
    with patch.object(scoring, "WEIGHTS_PATH", bad):
        assert scoring.weights() == scoring.DEFAULT_WEIGHTS
    with patch.object(scoring, "WEIGHTS_PATH", tmp_path / "missing.json"):
        assert scoring.weights() == scoring.DEFAULT_WEIGHTS
    partial = tmp_path / "partial.json"
    partial.write_text(json.dumps({"pain_severity": 3}), encoding="utf-8")
    with patch.object(scoring, "WEIGHTS_PATH", partial):
        assert scoring.weights() == scoring.DEFAULT_WEIGHTS


def test_finding_stores_its_recommendation():
    db = _db()
    result = _run(db)
    opp = result["opportunities"][0]
    assert opp["recommendation"] == scoring.recommendation(scoring.overall(SCORES))
    assert opp["times_seen"] == 1
    db.close()


# ── 3. dedup by keyword (no key) ──────────────────────────────

def test_second_run_repeats_instead_of_duplicating():
    db = _db()
    first = _run(db)
    fid = first["opportunities"][0]["id"]
    assert first["repeated"] == []

    more_evidence = [{**FAKE_EVIDENCE[0], "ref": "https://example.com/second-source",
                      "title": "A second source"}]
    second = _run(db, evidence=more_evidence)

    assert second["opportunities"] == []
    assert second["repeated"] == [{"finding_id": fid, "problem": first["opportunities"][0]["problem"]}]
    assert db.query(models.ResearchFinding).count() == 1
    finding = db.query(models.ResearchFinding).filter(models.ResearchFinding.id == fid).first()
    assert finding.times_seen == 2
    refs = [e["ref"] for e in json.loads(finding.evidence)]
    assert refs == [FAKE_EVIDENCE[0]["ref"], "https://example.com/second-source"]

    # the same evidence again adds nothing new
    _run(db, evidence=more_evidence)
    db.refresh(finding)
    assert finding.times_seen == 3
    assert len(json.loads(finding.evidence)) == 2
    db.close()


def test_a_different_segment_is_not_a_repeat():
    db = _db()
    _run(db)

    def other_segment(system, user, temperature=0.4, **_):
        out = _fake_llm(system, user)
        if "Researcher in a market intelligence" in system:
            out["candidates"][0]["customer_segment"] = "bakeries, Ndola"
        return out

    second = _run(db, llm=other_segment)
    assert len(second["opportunities"]) == 1 and second["repeated"] == []
    assert db.query(models.ResearchFinding).count() == 2
    db.close()


# ── 4. dedup by embeddings ────────────────────────────────────

def _paraphrase(problem):
    def fake(system, user, temperature=0.4, **_):
        out = _fake_llm(system, user)
        if "Researcher in a market intelligence" in system:
            out["candidates"][0]["problem"] = problem
            out["candidates"][0]["customer_segment"] = "chemists in the capital"
        return out
    return fake


def _embedder(vectors):
    def fake(texts, **_):
        out = []
        for t in texts:
            for needle, vec in vectors.items():
                if needle in t:
                    out.append(vec)
                    break
            else:
                out.append([0.0, 0.0, 1.0])
        return out
    return fake


@pytest.mark.parametrize("vector,is_repeat", [
    ([0.95, 0.3122, 0.0], True),     # cosine 0.95 with [1, 0, 0]
    ([0.85, 0.5268, 0.0], False),    # cosine 0.85
])
def test_embedding_dedup_threshold(monkeypatch, vector, is_repeat):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    db = _db()
    vectors = {"manual stock tracking": [1.0, 0.0, 0.0], "Chemists run out": vector}
    with patch("llm.embed_texts", side_effect=_embedder(vectors)):
        first = _run(db)
        second = _run(db, llm=_paraphrase("Chemists run out of fast sellers because counts are done by hand"))

    assert len(first["opportunities"]) == 1
    if is_repeat:
        assert second["opportunities"] == []
        assert second["repeated"][0]["finding_id"] == first["opportunities"][0]["id"]
        assert db.query(models.ResearchFinding).count() == 1
    else:
        assert len(second["opportunities"]) == 1 and second["repeated"] == []
        assert db.query(models.ResearchFinding).count() == 2
    db.close()


# ── 5. dismiss, change log, undo ──────────────────────────────

def test_run_and_promote_are_in_the_change_log():
    db = _db()
    result = _run(db)
    fid = result["opportunities"][0]["id"]
    db.close()

    changes = client.get("/changes").json()
    created = [c for c in changes if c["table"] == "research_findings" and c["action"] == "create"]
    assert len(created) == 1 and created[0]["summary"].startswith("Surfaced: ")

    promoted = client.post("/research/promote", json={"finding_id": fid}).json()
    assert len(promoted["changes"]) == 2
    changes = client.get("/changes").json()
    assert any(c["table"] == "opportunity_pipeline" and c["action"] == "create" for c in changes)
    assert any(c["table"] == "research_findings" and c["action"] == "update" for c in changes)

    patched = client.patch(f"/research/pipeline/{promoted['pipeline_id']}", json={"stage": "researching"})
    assert patched.status_code == 200 and patched.json()["change"]["table"] == "opportunity_pipeline"

    db = _db()
    finding = db.query(models.ResearchFinding).filter(models.ResearchFinding.id == fid).first()
    assert finding.reviewed_at is not None
    db.close()


def test_dismiss_is_logged_idempotent_and_undoable():
    import change_log

    db = _db()
    fid = _run(db)["opportunities"][0]["id"]

    with change_log.batch_scope(db, "ui"):
        out = agent.dismiss_finding(db, fid, reason="too crowded")
        db.commit()
    assert out["status"] == "dismissed" and out["dismissed_reason"] == "too crowded"
    assert out["reviewed_at"] is not None
    assert out["change"]["table"] == "research_findings" and out["change"]["action"] == "update"

    again = agent.dismiss_finding(db, fid)
    assert again["already_dismissed"] is True and again["change"] is None
    assert agent.dismiss_finding(db, 9999)["error"]
    db.close()

    undo = client.post(f"/changes/{out['change']['id']}/undo")
    assert undo.status_code == 200
    db = _db()
    finding = db.query(models.ResearchFinding).filter(models.ResearchFinding.id == fid).first()
    assert finding.status == "surfaced"
    assert finding.dismissed_reason is None and finding.reviewed_at is None
    db.close()


# ── migration backfill ────────────────────────────────────────

def test_m014_backfills_recommendation_without_touching_overall():
    from sqlalchemy import text
    from tests.conftest import engine
    from migrations import m014_market_intel_v2 as m014
    from migrations._helpers import dialect

    db = _db()
    project = models.ResearchProject(title="old run", objective="an older research objective")
    db.add(project)
    db.flush()
    old = models.ResearchFinding(project_id=project.id, problem="an older finding",
                                 scores=json.dumps({**SCORES, "overall": 7.6}), overall_score=7.6)
    unscored = models.ResearchFinding(project_id=project.id, problem="no scores", scores=None)
    db.add_all([old, unscored])
    db.commit()
    old_id, unscored_id = old.id, unscored.id
    db.close()

    with engine.connect() as conn:
        with conn.begin():
            assert m014._backfill_recommendations(conn, dialect(conn, "test")) == 1
            assert m014._backfill_recommendations(conn, dialect(conn, "test")) == 0   # re-runnable
        rows = dict(conn.execute(text("SELECT id, recommendation FROM research_findings")).fetchall())
        overall = conn.execute(text("SELECT overall_score FROM research_findings WHERE id = :i"),
                               {"i": old_id}).scalar()
    assert rows[old_id] == scoring.recommendation(scoring.overall(SCORES))
    assert rows[unscored_id] is None
    assert overall == 7.6


# ════════════════════════════════════════════════════════════════
# 5A.2 — dismiss endpoint and tool, standing objectives, briefing
# ════════════════════════════════════════════════════════════════

from datetime import date, datetime, timedelta

import orchestrator_tools as ot


def test_dismiss_endpoint_and_tool_log_and_undo():
    db = _db()
    first = _run(db)["opportunities"][0]["id"]
    other = models.ResearchFinding(project_id=1, problem="A second finding", status="surfaced")
    db.add(other)
    db.commit()
    second = other.id
    db.close()

    tapped = client.post(f"/research/findings/{first}/dismiss", json={"reason": "too crowded"})
    assert tapped.status_code == 200
    body = tapped.json()
    assert body["status"] == "dismissed" and body["dismissed_reason"] == "too crowded"
    assert body["change"]["table"] == "research_findings"
    assert client.post(f"/research/findings/{first}/dismiss").json()["already_dismissed"] is True
    assert client.post("/research/findings/9999/dismiss").status_code == 404

    db = _db()
    result, summary = ot.run_tool("dismiss_research_finding", {"finding_id": second, "reason": "not for me"}, db)
    assert (result["status"], result["reason"], summary) == ("dismissed", "not for me", f"finding {second} dismissed")
    again, summary = ot.run_tool("dismiss_research_finding", {"finding_id": second}, db)
    assert "already dismissed" in summary
    missing, _ = ot.run_tool("dismiss_research_finding", {"finding_id": 9999}, db)
    assert "error" in missing
    db.close()

    assert client.post(f"/changes/{result['change']['id']}/undo").status_code == 200
    assert client.post(f"/changes/{body['change']['id']}/undo").status_code == 200
    db = _db()
    assert {f.status for f in db.query(models.ResearchFinding).all()} == {"surfaced"}
    db.close()


# ── 6. standing objectives ────────────────────────────────────

def test_objectives_schedule_run_and_due(monkeypatch):
    db = _db()
    made, summary = ot.run_tool("schedule_research", {"objective": "painful admin problems in Lusaka pharmacies",
                                                      "cadence_days": 14}, db)
    assert summary == "research scheduled every 14 days"
    assert made["created"] is True and made["due"] is True and made["max_opportunities"] == 3
    oid = made["id"]

    again, summary = ot.run_tool("schedule_research", {"objective": "Painful admin problems in Lusaka pharmacies",
                                                       "cadence_days": 7, "max_opportunities": 2}, db)
    assert again["created"] is False and again["id"] == oid and summary == "research scheduled every 7 days"
    assert db.query(models.ResearchObjective).count() == 1
    short, _ = ot.run_tool("schedule_research", {"objective": "SMEs"}, db)
    assert "error" in short
    db.close()

    assert client.post(f"/research/objectives/{oid}/run").status_code == 503      # no AI key

    monkeypatch.setenv("OPENAI_API_KEY", "test")
    with patch("market_intel.agent._llm_json", side_effect=_fake_llm), \
         patch("llm.embed_texts", side_effect=lambda texts, **_: [[1.0, 0.0] for _t in texts]), \
         patch("market_intel.adapters.search_all", return_value=list(FAKE_EVIDENCE)):
        ran = client.post(f"/research/objectives/{oid}/run")
    assert ran.status_code == 200, ran.text
    out = ran.json()
    assert len(out["opportunities"]) == 1 and out["objective_id"] == oid
    assert out["objective"]["due"] is False and out["objective"]["last_project_id"] == out["project_id"]
    assert any(c["table"] == "research_findings" for c in out["changes"])

    db = _db()
    project = db.query(models.ResearchProject).filter(models.ResearchProject.id == out["project_id"]).one()
    objective = db.query(models.ResearchObjective).one()
    assert project.objective_id == oid and objective.last_run_at is not None
    objective.last_run_at = datetime.utcnow() - timedelta(days=8)                  # a week and a day ago
    db.commit()
    db.close()

    listed = client.get("/research/objectives").json()
    assert listed[0]["due"] is True and listed[0]["cadence_days"] == 7
    off = client.patch(f"/research/objectives/{oid}", json={"enabled": False}).json()
    assert off["enabled"] is False and off["due"] is False and off["change"]["table"] == "research_objectives"
    assert client.patch(f"/research/objectives/{oid}", json={"objective": "x"}).status_code == 400
    assert client.patch("/research/objectives/9999", json={"enabled": True}).status_code == 404
    created = client.post("/research/objectives", json={"objective": "what bakeries in Kitwe struggle with every week"})
    assert created.status_code == 201 and created.json()["cadence_days"] == 14

    detail = client.get(f"/research/projects/{out['project_id']}").json()
    assert detail["llm_calls"] == 0 and detail["objective_id"] == oid and detail["repeated"] == []


def test_project_detail_lists_findings_seen_again():
    db = _db()
    first = _run(db)
    second = _run(db)
    db.close()
    detail = client.get(f"/research/projects/{second['project_id']}").json()
    assert detail["repeated"] == [{"finding_id": first["opportunities"][0]["id"],
                                   "problem": first["opportunities"][0]["problem"]}]
    assert client.get(f"/research/projects/{first['project_id']}").json()["repeated"] == []


def test_run_tool_summary_mentions_repeats():
    db = _db()
    _run(db)
    with patch("market_intel.agent._llm_json", side_effect=_fake_llm), \
         patch("market_intel.adapters.search_all", return_value=list(FAKE_EVIDENCE)):
        result, summary = ot.run_tool("run_market_research_agent",
                                      {"objective": "find painful problems in Zambian SMEs where AI helps"}, db)
    assert len(result["repeated"]) == 1 and "1 seen before" in summary and "0 opportunities surfaced" in summary
    db.close()


# ── 7. briefing ───────────────────────────────────────────────

def test_briefing_shows_new_opportunities_stale_pipeline_and_due_objectives():
    import briefing
    db = _db()
    result = _run(db)
    fid = result["opportunities"][0]["id"]
    finding = db.query(models.ResearchFinding).filter(models.ResearchFinding.id == fid).one()
    finding.recommendation, finding.overall_score = "pursue", 8.1
    watch = models.ResearchFinding(project_id=result["project_id"], problem="A watch-grade finding",
                                   status="surfaced", recommendation="watch", overall_score=6.5)
    old = models.ResearchFinding(project_id=result["project_id"], problem="An old pursue finding",
                                 status="surfaced", recommendation="pursue", overall_score=9.0,
                                 created_at=datetime.utcnow() - timedelta(days=20))
    db.add_all([watch, old])
    db.commit()
    db.close()

    def items():
        b = briefing.build_briefing(_db(), with_summary=False)
        return b, {i["id"]: i for i in b["items"]}

    b, by_id = items()
    item = by_id[f"opportunity:research_finding:{fid}"]
    assert item["score"] == 35 and item["due_label"] == "8.1/10"
    assert item["reason"] == "Verified opportunity, not yet reviewed" and item["suggested_action"] == "Promote or dismiss"
    assert sum(1 for i in b["items"] if i["target_type"] == "research_finding") == 1   # not watch, not 20 days old
    assert b["counts"]["opportunities_new"] == 1
    base_attention = b["attention_count"]

    promoted = client.post("/research/promote", json={"finding_id": fid}).json()
    b, by_id = items()
    assert f"opportunity:research_finding:{fid}" not in by_id            # reviewed: gone
    assert b["counts"]["opportunities_new"] == 0 and b["attention_count"] == base_attention
    assert f"opportunity:opportunity_pipeline:{promoted['pipeline_id']}" not in by_id   # fresh pipeline entry

    db = _db()
    entry = db.query(models.OpportunityPipeline).filter(models.OpportunityPipeline.id == promoted["pipeline_id"]).one()
    entry.updated_at = datetime.utcnow() - timedelta(days=16)
    db.commit()
    db.close()
    b, by_id = items()
    stale = by_id[f"opportunity:opportunity_pipeline:{promoted['pipeline_id']}"]
    assert stale["score"] == 30 and stale["reason"] == "Pipeline entry stale for 16 days"
    assert stale["suggested_action"] == "Decide: validate or reject"

    yesterday = (date.today() - timedelta(days=1)).isoformat()
    patched = client.patch(f"/research/pipeline/{promoted['pipeline_id']}",
                           json={"next_action": "Call three pharmacies", "due_date": yesterday}).json()
    assert patched["next_action"] == "Call three pharmacies" and patched["due_date"] == yesterday
    b, by_id = items()
    late = by_id[f"opportunity:opportunity_pipeline:{promoted['pipeline_id']}"]
    assert late["score"] == 48 and late["reason"] == "Next action overdue"
    assert late["suggested_action"] == "Call three pharmacies"
    listed = client.get("/research/pipeline").json()[0]
    assert listed["next_action"] == "Call three pharmacies" and listed["finding"]["recommendation"] == "pursue"

    client.patch(f"/research/pipeline/{promoted['pipeline_id']}", json={"stage": "validating"})
    b, by_id = items()
    assert f"opportunity:opportunity_pipeline:{promoted['pipeline_id']}" not in by_id   # past the early stages

    made = client.post("/research/objectives", json={"objective": "painful admin problems in Lusaka pharmacies"}).json()
    b, by_id = items()
    due = by_id[f"opportunity:research_objective:{made['id']}"]
    assert due["score"] == 12 and due["suggested_action"] == "Run it (one tap on Agents)"


def test_briefing_drops_a_dismissed_finding():
    import briefing
    db = _db()
    fid = _run(db)["opportunities"][0]["id"]
    finding = db.query(models.ResearchFinding).filter(models.ResearchFinding.id == fid).one()
    finding.recommendation = "pursue"
    db.commit()
    db.close()
    key = f"opportunity:research_finding:{fid}"
    assert key in {i["id"] for i in briefing.build_briefing(_db(), with_summary=False)["items"]}
    client.post(f"/research/findings/{fid}/dismiss")
    assert key not in {i["id"] for i in briefing.build_briefing(_db(), with_summary=False)["items"]}
