"""
Market Intelligence Agent test suite.

All LLM calls are mocked at the ``market_intel.agent._llm_json`` seam (a
dispatcher keyed on role phrases in the system prompt) and all source
searches at ``market_intel.adapters.search_all`` — no network, no OpenAI.

Layers:
  1. Pipeline    — full run surfaces findings, never touches the pipeline
  2. Quality bar — unevidenced candidates are rejected
  3. Promotion   — explicit promote is the only pipeline write path; idempotent
  4. Dispatch    — both tools resolve through run_tool
  5. Memory      — upsert-by-slug, no duplicates
  6. Adapters    — degrade to [] on failure
  7. REST        — endpoints round-trip
"""

import sys
import json
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import models
import orchestrator_tools as ot
from market_intel import memory as mi_memory
from market_intel.adapters import WebSearchAdapter
from market_intel.agent import run_market_research
from tests.conftest import TestSessionLocal, client


def _db():
    return TestSessionLocal()


FAKE_EVIDENCE = [{
    "source_type": "web",
    "ref": "https://example.com/pharmacy-stockouts",
    "title": "Pharmacy stockouts in Lusaka",
    "excerpt": "Independent pharmacies report weekly stockouts from paper-based tracking.",
}]


def _fake_llm(system, user, temperature=0.4):
    """Canned outputs per role, keyed on distinctive system-prompt phrases."""
    if "Research Planner" in system:
        return {
            "title": "Zambian SME pain scan",
            "threads": [{"focus": "pharmacy stock management",
                         "queries": ["pharmacy stock tracking zambia"]}],
        }
    if "Researcher in a market intelligence" in system:
        return {"candidates": [{
            "problem": "Independent pharmacies in Lusaka lose sales to manual stock tracking",
            "industry": "retail pharmacy",
            "customer_segment": "independent pharmacies, Lusaka",
            "persona": "owner-operator, 1-3 staff",
            "pain_description": "weekly stockouts of fast-moving items",
            "frequency": "weekly",
            "financial_impact": "10-15% of monthly sales lost",
            "current_workaround": "paper ledgers and memory",
            "evidence_indexes": [0],
        }]}
    if "You are the Analyst" in system:
        return {"analyses": [{
            "id": "c1", "merged_ids": [],
            "market_analysis": {
                "existing_solutions": "imported POS systems",
                "why_they_fail": "too expensive, need training",
                "competitive_landscape": "sparse local offerings",
                "market_attractiveness": "high",
                "demand_trends": "growing",
            },
            "scores": {"pain_severity": 8, "frequency": 8, "financial_value": 7,
                       "market_size": 6, "competition_gap": 7},
        }]}
    if "You are the Founder Advisor" in system:
        return {"assessments": [{
            "id": "c1",
            "founder_fit": {
                "why_i_can_solve_this": "FastAPI backend + WhatsApp bot frontend",
                "difficulty": "medium",
                "mvp_feasibility": "stock alert bot in 2-3 weeks",
                "business_model": "monthly subscription",
            },
            "first_customer_path": "walk into five pharmacies in Lusaka with a demo",
            "scores": {"founder_fit": 9, "speed_to_mvp": 8},
        }]}
    if "You are the Verifier" in system:
        return {"verdicts": [{"id": "c1", "verdict": "accept",
                              "reason": "evidence supports the pain claim"}]}
    if "lesson memory" in system:
        return {"lessons": [{
            "slug": "lusaka-pharmacy-stock-pain",
            "summary": "Lusaka pharmacies repeatedly show strong stock-tracking pain",
            "content": "Multiple runs found stockout pain in independent pharmacies.",
        }]}
    return {}


def _run(db, objective="find painful problems in Zambian SMEs where AI helps",
         llm=_fake_llm, evidence=FAKE_EVIDENCE):
    with patch("market_intel.agent._llm_json", side_effect=llm), \
         patch("market_intel.adapters.search_all", return_value=list(evidence)):
        return run_market_research(db, objective)


# ════════════════════════════════════════════════════════════════
# 1. Pipeline run
# ════════════════════════════════════════════════════════════════

class TestResearchRun:

    def test_run_surfaces_findings_and_completes_project(self):
        db = _db()
        result = _run(db)

        assert "error" not in result
        assert result["title"] == "Zambian SME pain scan"
        assert len(result["opportunities"]) == 1
        opp = result["opportunities"][0]
        assert opp["status"] == "surfaced"
        assert opp["customer_segment"] == "independent pharmacies, Lusaka"
        assert opp["evidence"][0]["ref"] == FAKE_EVIDENCE[0]["ref"]
        assert opp["first_customer_path"].startswith("walk into")
        # overall computed in Python: mean of (8,8,7,6,7,9,8) = 7.6
        assert opp["scores"]["overall"] == 7.6
        assert opp["overall_score"] == 7.6

        project = db.query(models.ResearchProject).get(result["project_id"])
        assert project.status == "completed"
        assert project.completed_at is not None
        db.close()

    def test_run_never_writes_to_pipeline(self):
        db = _db()
        _run(db)
        assert db.query(models.OpportunityPipeline).count() == 0
        db.close()

    def test_run_updates_lesson_memory(self):
        db = _db()
        result = _run(db)
        assert result["lessons_updated"] == 1
        note = db.query(models.ResearchMemoryNote).filter_by(
            slug="lusaka-pharmacy-stock-pain").first()
        assert note is not None
        assert note.times_reinforced == 1
        db.close()

    def test_short_objective_rejected_without_project(self):
        db = _db()
        result = run_market_research(db, "SMEs")
        assert "error" in result
        assert db.query(models.ResearchProject).count() == 0
        db.close()

    def test_no_evidence_completes_with_zero_opportunities(self):
        db = _db()
        result = _run(db, evidence=[])
        assert result["opportunities"] == []
        project = db.query(models.ResearchProject).get(result["project_id"])
        assert project.status == "completed"
        db.close()


# ════════════════════════════════════════════════════════════════
# 2. Quality bar
# ════════════════════════════════════════════════════════════════

class TestQualityBar:

    def test_verifier_rejection_drops_candidate(self):
        def llm(system, user, temperature=0.4):
            if "You are the Verifier" in system:
                return {"verdicts": [{"id": "c1", "verdict": "reject",
                                      "reason": "generic, no specific customer"}]}
            return _fake_llm(system, user, temperature)

        db = _db()
        result = _run(db, llm=llm)
        assert result["opportunities"] == []
        assert result["rejected_count"] == 1
        assert db.query(models.ResearchFinding).count() == 0
        db.close()

    def test_unevidenced_candidate_rejected_despite_llm_accept(self):
        """Hard floor: a candidate whose evidence_indexes are invalid carries no
        evidence and must be rejected even if the Verifier model says accept."""
        def llm(system, user, temperature=0.4):
            if "Researcher in a market intelligence" in system:
                out = _fake_llm(system, user, temperature)
                out["candidates"][0]["evidence_indexes"] = [99]   # out of range
                return out
            return _fake_llm(system, user, temperature)

        db = _db()
        result = _run(db, llm=llm)
        assert result["opportunities"] == []
        assert result["rejected_count"] == 1
        db.close()


# ════════════════════════════════════════════════════════════════
# 3. Promotion — explicit, idempotent, only write path
# ════════════════════════════════════════════════════════════════

def _seed_finding(db) -> int:
    project = models.ResearchProject(title="t", objective="o", status="completed")
    db.add(project)
    db.flush()
    finding = models.ResearchFinding(project_id=project.id, problem="pharmacy stockouts")
    db.add(finding)
    db.commit()
    return finding.id


class TestPromotion:

    def test_promote_creates_pipeline_entry_and_flips_status(self):
        db = _db()
        fid = _seed_finding(db)
        result, summary = ot.promote_research_opportunity(db, finding_id=fid, notes="go")
        assert result["stage"] == "discovered"
        assert result["already_promoted"] is False
        assert "promoted" in summary

        finding = db.query(models.ResearchFinding).get(fid)
        assert finding.status == "promoted"
        entry = db.query(models.OpportunityPipeline).filter_by(finding_id=fid).first()
        assert entry is not None and entry.notes == "go"
        db.close()

    def test_promote_is_idempotent(self):
        db = _db()
        fid = _seed_finding(db)
        ot.promote_research_opportunity(db, finding_id=fid)
        result, summary = ot.promote_research_opportunity(db, finding_id=fid)
        assert result["already_promoted"] is True
        assert db.query(models.OpportunityPipeline).count() == 1
        db.close()

    def test_promote_missing_finding_errors_without_raising(self):
        db = _db()
        result, summary = ot.promote_research_opportunity(db, finding_id=9999)
        assert "error" in result
        db.close()


# ════════════════════════════════════════════════════════════════
# 4. Tool dispatch
# ════════════════════════════════════════════════════════════════

class TestDispatch:

    def test_both_tools_registered_with_schemas(self):
        names = {s["function"]["name"] for s in ot.TOOL_SCHEMAS}
        assert "run_market_research_agent" in names
        assert "promote_research_opportunity" in names
        assert "run_market_research_agent" in ot._TOOL_DISPATCH
        assert "promote_research_opportunity" in ot._TOOL_DISPATCH

    def test_run_tool_routes_research_agent(self):
        db = _db()
        with patch("market_intel.agent._llm_json", side_effect=_fake_llm), \
             patch("market_intel.adapters.search_all", return_value=list(FAKE_EVIDENCE)):
            result, summary = ot.run_tool(
                "run_market_research_agent",
                {"objective": "find painful problems in Zambian SMEs"},
                db,
            )
        assert len(result["opportunities"]) == 1
        assert "1 opportunity surfaced" in summary
        db.close()

    def test_run_tool_routes_promote(self):
        db = _db()
        fid = _seed_finding(db)
        result, summary = ot.run_tool(
            "promote_research_opportunity", {"finding_id": fid}, db
        )
        assert result["stage"] == "discovered"
        db.close()


# ════════════════════════════════════════════════════════════════
# 5. Memory
# ════════════════════════════════════════════════════════════════

class TestMemory:

    def test_upsert_updates_existing_note_not_duplicate(self):
        db = _db()
        mi_memory.upsert_lessons(db, [
            {"slug": "fintech-dead-end", "summary": "v1", "content": "first"},
        ])
        mi_memory.upsert_lessons(db, [
            {"slug": "fintech-dead-end", "summary": "v2", "content": "second"},
        ])
        notes = db.query(models.ResearchMemoryNote).all()
        assert len(notes) == 1
        assert notes[0].summary == "v2"
        assert notes[0].times_reinforced == 2
        db.close()

    def test_digest_lists_slug_and_summary(self):
        db = _db()
        mi_memory.upsert_lessons(db, [
            {"slug": "pharmacy-pain", "summary": "pharmacies hurt", "content": "x"},
        ])
        digest = mi_memory.memory_digest(db)
        assert "pharmacy-pain: pharmacies hurt" in digest
        db.close()

    def test_invalid_lessons_skipped(self):
        db = _db()
        written = mi_memory.upsert_lessons(db, [
            {"slug": "no-content", "summary": "s", "content": ""},
            {"summary": "", "content": "body"},
        ])
        assert written == 0
        assert db.query(models.ResearchMemoryNote).count() == 0
        db.close()


# ════════════════════════════════════════════════════════════════
# 6. Adapter contract
# ════════════════════════════════════════════════════════════════

class TestAdapters:

    def test_web_adapter_degrades_to_empty_on_network_failure(self):
        adapter = WebSearchAdapter()
        with patch("market_intel.adapters.httpx.post",
                   side_effect=ConnectionError("network down")):
            assert adapter.search("zambian sme pain") == []

    def test_short_query_returns_empty_without_calling_source(self):
        adapter = WebSearchAdapter()
        assert adapter.search("a") == []


# ════════════════════════════════════════════════════════════════
# 7. REST endpoints
# ════════════════════════════════════════════════════════════════

class TestRest:

    def test_projects_and_detail_roundtrip(self):
        db = _db()
        _run(db)
        db.close()

        projects = client.get("/research/projects").json()
        assert len(projects) == 1
        assert projects[0]["finding_count"] == 1

        detail = client.get(f"/research/projects/{projects[0]['id']}").json()
        assert detail["status"] == "completed"
        assert detail["findings"][0]["scores"]["overall"] == 7.6

    def test_promote_endpoint_and_pipeline_listing(self):
        db = _db()
        fid = _seed_finding(db)
        db.close()

        resp = client.post("/research/promote", json={"finding_id": fid, "notes": "n"})
        assert resp.status_code == 200
        assert resp.json()["stage"] == "discovered"

        pipeline = client.get("/research/pipeline").json()
        assert len(pipeline) == 1
        assert pipeline[0]["finding"]["id"] == fid

        entry_id = pipeline[0]["id"]
        moved = client.patch(f"/research/pipeline/{entry_id}",
                             json={"stage": "validating"})
        assert moved.json()["stage"] == "validating"

        bad = client.patch(f"/research/pipeline/{entry_id}", json={"stage": "bogus"})
        assert bad.status_code == 400

    def test_promote_endpoint_404_for_missing_finding(self):
        resp = client.post("/research/promote", json={"finding_id": 424242})
        assert resp.status_code == 404

    def test_memory_endpoint(self):
        db = _db()
        mi_memory.upsert_lessons(db, [
            {"slug": "s1", "summary": "sum", "content": "body"},
        ])
        db.close()
        notes = client.get("/research/memory").json()
        assert notes[0]["slug"] == "s1"
