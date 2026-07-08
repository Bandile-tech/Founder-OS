"""
Market Intelligence Agent — role pipeline.

Given a research objective, runs five internal roles and returns structured,
evidence-backed opportunities:

  1. Research Planner  — decides what to investigate (2-4 independent threads),
                         informed by the lesson memory digest.
  2. Researcher        — one per thread, run in PARALLEL; pulls evidence from
                         every source adapter and extracts candidate problems.
  3. Analyst           — merges candidates, evaluates pain + market, scores
                         the first five axes.
  4. Founder Advisor   — filters against the founder profile, scores
                         founder_fit + speed_to_mvp, adds first-customer path.
  5. Verifier          — adversarial pass against the evidence list; enforces
                         the quality bar and rejects unevidenced/generic ideas.

The agent SURFACES findings (status='surfaced'). It never writes to the
opportunity pipeline — promotion is an explicit, separate action.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import models
from market_intel import adapters, memory

MODEL = "gpt-4o-mini"
MAX_THREADS = 4
MAX_QUERIES_PER_THREAD = 3
EVIDENCE_PER_SOURCE = 4
MAX_EVIDENCE_PER_THREAD = 12

_PROFILE_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "context" / "core" / "founder_profile.md"
)

_DEFAULT_PROFILE = """\
Solo founder. Student in Zambia (limited hours; zero-to-low budget).
Technical strengths: Python, FastAPI, AI agents / LLM integration, web apps.
Focus: SME automation in emerging markets (Zambia / Southern Africa first).
Constraints: must be executable solo, MVP shippable in weeks not months,
low infrastructure cost, first customers reachable without a sales team.
"""

_QUALITY_BAR = """\
QUALITY BAR — a candidate FAILS if any of these hold:
- No specific customer (a named segment + persona), only "businesses" or "SMEs" in general.
- No specific pain: vague, aspirational, or "AI will change everything" filler.
- No supporting evidence items, or the evidence does not actually support the pain claim.
- Large market invoked with no specific customer pain behind it.
- Not executable by a solo Python/FastAPI founder (needs hardware, licenses, a team, or capital).
- No plausible path to a first customer.
"""


def _load_founder_profile() -> str:
    try:
        text = _PROFILE_PATH.read_text(encoding="utf-8").strip()
        if text:
            return text
    except OSError:
        pass
    return _DEFAULT_PROFILE


def _llm_json(system: str, user: str, temperature: float = 0.4) -> dict:
    """One JSON-mode completion. Returns {} on any failure so a single bad
    call degrades the run instead of crashing it."""
    from openai_client import client

    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
            temperature=temperature,
        )
        return json.loads(resp.choices[0].message.content or "{}")
    except Exception as e:                  # noqa: BLE001
        print(f"[market_intel] LLM call failed: {e}")
        return {}


# ── Role 1: Research Planner ─────────────────────────────────

def _plan(objective: str, profile: str, digest: str) -> dict:
    system = (
        "You are the Research Planner of a market intelligence department. "
        "Break the objective into 2-4 INDEPENDENT research threads. Each thread has a "
        "focus question and 2-3 search queries. Write queries for different channels: "
        "plain web, forums/discussions (e.g. add site:reddit.com or 'forum'), "
        "reviews/complaints (add 'review' or 'complaints'), and industry reports "
        "(add 'report' or 'survey'). Skip angles the lesson memory marks as dead ends; "
        "lean into industries it marks as repeatedly painful. "
        'Respond as JSON: {"title": "<short project title>", '
        '"threads": [{"focus": "...", "queries": ["...", "..."]}]}'
    )
    user = (
        f"OBJECTIVE:\n{objective}\n\n"
        f"FOUNDER PROFILE:\n{profile}\n\n"
        f"LESSON MEMORY:\n{digest}"
    )
    plan = _llm_json(system, user)
    threads = plan.get("threads") or []
    threads = [
        {
            "focus": t.get("focus", ""),
            "queries": [q for q in (t.get("queries") or []) if q][:MAX_QUERIES_PER_THREAD],
        }
        for t in threads
        if t.get("focus") and t.get("queries")
    ][:MAX_THREADS]
    if not threads:
        # Planner failed — fall back to a single thread on the raw objective.
        threads = [{"focus": objective, "queries": [objective]}]
    title = (plan.get("title") or objective)[:120]
    return {"title": title, "threads": threads}


# ── Role 2: Researcher (parallel, one per thread) ────────────

def _research_thread(objective: str, thread: dict) -> dict:
    """Collect evidence for one thread across all adapters, then extract
    candidate problems. Runs inside a worker thread — adapters open their own
    DB sessions; no session is shared."""
    evidence, seen_refs = [], set()
    for query in thread["queries"]:
        for item in adapters.search_all(query, limit_per_source=EVIDENCE_PER_SOURCE):
            if item["ref"] in seen_refs:
                continue
            seen_refs.add(item["ref"])
            evidence.append(item)
            if len(evidence) >= MAX_EVIDENCE_PER_THREAD:
                break
        if len(evidence) >= MAX_EVIDENCE_PER_THREAD:
            break

    if not evidence:
        return {"focus": thread["focus"], "candidates": [], "evidence": []}

    numbered = "\n".join(
        f"[{i}] ({e['source_type']}) {e['title']} — {e['excerpt']} <{e['ref']}>"
        for i, e in enumerate(evidence)
    )
    system = (
        "You are a Researcher in a market intelligence department. From the numbered "
        "evidence items, extract SPECIFIC business problems relevant to the objective. "
        "Only extract problems the evidence actually supports — do not invent. "
        'Respond as JSON: {"candidates": [{"problem": "one specific pain, one paragraph", '
        '"industry": "...", "customer_segment": "...", "persona": "...", '
        '"pain_description": "...", "frequency": "how often it bites", '
        '"financial_impact": "cost of the problem", "current_workaround": "...", '
        '"evidence_indexes": [0, 2]}]} — evidence_indexes MUST reference the numbered '
        "items that support the claim. A candidate with no supporting index is invalid."
    )
    user = (
        f"OBJECTIVE:\n{objective}\n\nTHREAD FOCUS:\n{thread['focus']}\n\n"
        f"EVIDENCE:\n{numbered}"
    )
    out = _llm_json(system, user)
    candidates = []
    for c in out.get("candidates") or []:
        if not c.get("problem"):
            continue
        idxs = [i for i in (c.get("evidence_indexes") or [])
                if isinstance(i, int) and 0 <= i < len(evidence)]
        c["evidence"] = [evidence[i] for i in idxs]
        candidates.append(c)
    return {"focus": thread["focus"], "candidates": candidates, "evidence": evidence}


# ── Role 3: Analyst ──────────────────────────────────────────

_ANALYST_AXES = ("pain_severity", "frequency", "financial_value",
                 "market_size", "competition_gap")


def _candidates_block(candidates: list) -> str:
    lines = []
    for c in candidates:
        ev = "; ".join(
            f"({e['source_type']}) {e['title']}: {e['excerpt'][:150]}"
            for e in c.get("evidence", [])
        ) or "NO EVIDENCE"
        lines.append(
            f"id={c['id']} | problem={c.get('problem')} | industry={c.get('industry')} | "
            f"customer={c.get('customer_segment')} | persona={c.get('persona')} | "
            f"evidence: {ev}"
        )
    return "\n".join(lines)


def _analyze(objective: str, candidates: list) -> list:
    system = (
        "You are the Analyst of a market intelligence department. Merge duplicate "
        "candidates (same underlying problem) and evaluate each distinct one. "
        'Respond as JSON: {"analyses": [{"id": "<candidate id>", '
        '"merged_ids": ["ids folded into this one"], '
        '"market_analysis": {"existing_solutions": "...", "why_they_fail": "...", '
        '"competitive_landscape": "...", "market_attractiveness": "...", '
        '"demand_trends": "..."}, '
        '"scores": {"pain_severity": 0-10, "frequency": 0-10, "financial_value": 0-10, '
        '"market_size": 0-10, "competition_gap": 0-10}}]} '
        "Score honestly against the evidence — unsupported claims score low."
    )
    user = f"OBJECTIVE:\n{objective}\n\nCANDIDATES:\n{_candidates_block(candidates)}"
    out = _llm_json(system, user, temperature=0.2)

    by_id = {c["id"]: c for c in candidates}
    analyzed = []
    for a in out.get("analyses") or []:
        c = by_id.get(a.get("id"))
        if c is None:
            continue
        for mid in a.get("merged_ids") or []:
            dup = by_id.get(mid)
            if dup is not None and dup is not c:
                c["evidence"] = c.get("evidence", []) + dup.get("evidence", [])
                dup["_merged_away"] = True
        c["market_analysis"] = a.get("market_analysis") or {}
        c["scores"] = {
            k: _clamp_score((a.get("scores") or {}).get(k)) for k in _ANALYST_AXES
        }
        analyzed.append(c)
    return [c for c in analyzed if not c.get("_merged_away")]


# ── Role 4: Founder Advisor ──────────────────────────────────

def _advise(profile: str, candidates: list) -> list:
    system = (
        "You are the Founder Advisor. Evaluate each candidate strictly against the "
        "founder profile. Be honest — a great market the founder cannot execute solo "
        "scores low on fit. "
        'Respond as JSON: {"assessments": [{"id": "<candidate id>", '
        '"founder_fit": {"why_i_can_solve_this": "...", "difficulty": "low|medium|high", '
        '"mvp_feasibility": "what the MVP is and why it is shippable solo", '
        '"business_model": "..."}, '
        '"first_customer_path": "specific, plausible path to customer #1", '
        '"scores": {"founder_fit": 0-10, "speed_to_mvp": 0-10}}]}'
    )
    user = f"FOUNDER PROFILE:\n{profile}\n\nCANDIDATES:\n{_candidates_block(candidates)}"
    out = _llm_json(system, user, temperature=0.2)

    by_id = {c["id"]: c for c in candidates}
    for a in out.get("assessments") or []:
        c = by_id.get(a.get("id"))
        if c is None:
            continue
        c["founder_fit"] = a.get("founder_fit") or {}
        c["first_customer_path"] = a.get("first_customer_path") or ""
        c.setdefault("scores", {})
        c["scores"]["founder_fit"] = _clamp_score((a.get("scores") or {}).get("founder_fit"))
        c["scores"]["speed_to_mvp"] = _clamp_score((a.get("scores") or {}).get("speed_to_mvp"))
    return candidates


# ── Role 5: Verifier ─────────────────────────────────────────

def _verify(objective: str, candidates: list) -> tuple:
    """Adversarial check of every candidate against its evidence and the
    quality bar. Returns (accepted, rejected_with_reasons)."""
    system = (
        "You are the Verifier — an adversarial reviewer. For each candidate, check the "
        "claims against the evidence actually cited and apply the quality bar below. "
        "Default to REJECT when uncertain.\n\n" + _QUALITY_BAR +
        '\nRespond as JSON: {"verdicts": [{"id": "<candidate id>", '
        '"verdict": "accept" or "reject", "reason": "one sentence"}]}'
    )
    user = f"OBJECTIVE:\n{objective}\n\nCANDIDATES:\n{_candidates_block(candidates)}"
    out = _llm_json(system, user, temperature=0.0)

    verdicts = {v.get("id"): v for v in out.get("verdicts") or []}
    accepted, rejected = [], []
    for c in candidates:
        v = verdicts.get(c["id"], {})
        # Hard floor regardless of the model's verdict: no evidence → reject.
        if not c.get("evidence"):
            rejected.append({"id": c["id"], "reason": "no supporting evidence"})
        elif v.get("verdict") == "accept":
            accepted.append(c)
        else:
            rejected.append({
                "id": c["id"],
                "reason": v.get("reason", "failed verification"),
            })
    return accepted, rejected


# ── Lesson extraction ────────────────────────────────────────

def _extract_lessons(objective: str, accepted: list, rejected: list) -> list:
    system = (
        "You maintain the lesson memory of a market intelligence department. From this "
        "run, produce 0-3 durable lessons worth remembering for FUTURE research runs: "
        "industries repeatedly showing strong pain, problems already investigated, "
        "failed assumptions, patterns discovered. No generic advice. "
        'Respond as JSON: {"lessons": [{"slug": "kebab-case-id", '
        '"summary": "one line", "content": "2-4 sentence markdown body"}]}'
    )
    user = (
        f"OBJECTIVE:\n{objective}\n\n"
        f"ACCEPTED ({len(accepted)}): "
        + "; ".join(c.get("problem", "")[:120] for c in accepted)
        + f"\n\nREJECTED ({len(rejected)}): "
        + "; ".join(f"{r['reason']}" for r in rejected[:10])
    )
    out = _llm_json(system, user)
    return (out.get("lessons") or [])[:3]


# ── Helpers ──────────────────────────────────────────────────

def _clamp_score(value) -> float:
    try:
        return round(min(10.0, max(0.0, float(value))), 1)
    except (TypeError, ValueError):
        return 0.0


_ALL_AXES = _ANALYST_AXES + ("founder_fit", "speed_to_mvp")


def _finalize_scores(candidate: dict) -> dict:
    scores = {k: _clamp_score((candidate.get("scores") or {}).get(k)) for k in _ALL_AXES}
    scores["overall"] = round(sum(scores.values()) / len(_ALL_AXES), 1)
    return scores


def _serialize_finding(f: models.ResearchFinding) -> dict:
    def _j(raw):
        try:
            return json.loads(raw) if raw else None
        except (ValueError, TypeError):
            return raw
    return {
        "id": f.id,
        "project_id": f.project_id,
        "problem": f.problem,
        "industry": f.industry,
        "customer_segment": f.customer_segment,
        "persona": f.persona,
        "discovery": _j(f.discovery),
        "market_analysis": _j(f.market_analysis),
        "founder_fit": _j(f.founder_fit),
        "evidence": _j(f.evidence),
        "scores": _j(f.scores),
        "overall_score": f.overall_score,
        "first_customer_path": f.first_customer_path,
        "status": f.status,
        "notes": f.notes,
        "created_at": str(f.created_at) if f.created_at else None,
    }


# ── Entry point ──────────────────────────────────────────────

def run_market_research(db, objective: str, max_opportunities: int = 3) -> dict:
    """Full pipeline run. Creates a ResearchProject, surfaces findings, updates
    lesson memory. Never touches the opportunity pipeline. Returns a dict; on
    failure the project is marked 'failed' and an error payload is returned."""
    objective = (objective or "").strip()
    if len(objective) < 10:
        return {"error": "Objective too short — give the agent a real research objective."}
    max_opportunities = max(1, min(int(max_opportunities or 3), 5))

    profile = _load_founder_profile()
    digest = memory.memory_digest(db)

    project = models.ResearchProject(title=objective[:120], objective=objective)
    db.add(project)
    db.commit()
    db.refresh(project)

    try:
        plan = _plan(objective, profile, digest)
        project.title = plan["title"]
        db.commit()

        with ThreadPoolExecutor(max_workers=MAX_THREADS) as pool:
            thread_results = list(pool.map(
                lambda t: _research_thread(objective, t), plan["threads"]
            ))

        candidates = []
        for tr in thread_results:
            for c in tr["candidates"]:
                c["id"] = f"c{len(candidates) + 1}"
                candidates.append(c)

        if not candidates:
            project.status = "completed"
            project.completed_at = datetime.utcnow()
            db.commit()
            return {
                "project_id": project.id, "title": project.title,
                "opportunities": [], "rejected_count": 0, "lessons_updated": 0,
                "note": "No evidence-backed candidates found for this objective.",
            }

        analyzed = _analyze(objective, candidates)
        advised = _advise(profile, analyzed)
        accepted, rejected = _verify(objective, advised)

        accepted.sort(key=lambda c: _finalize_scores(c)["overall"], reverse=True)
        accepted = accepted[:max_opportunities]

        findings = []
        for c in accepted:
            scores = _finalize_scores(c)
            finding = models.ResearchFinding(
                project_id=project.id,
                problem=c["problem"],
                industry=c.get("industry"),
                customer_segment=c.get("customer_segment"),
                persona=c.get("persona"),
                discovery=json.dumps({
                    "pain_description": c.get("pain_description"),
                    "frequency": c.get("frequency"),
                    "financial_impact": c.get("financial_impact"),
                    "current_workaround": c.get("current_workaround"),
                }),
                market_analysis=json.dumps(c.get("market_analysis") or {}),
                founder_fit=json.dumps(c.get("founder_fit") or {}),
                evidence=json.dumps(c.get("evidence") or []),
                scores=json.dumps(scores),
                overall_score=scores["overall"],
                first_customer_path=c.get("first_customer_path"),
                status="surfaced",
            )
            db.add(finding)
            findings.append(finding)
        db.commit()

        lessons = _extract_lessons(objective, accepted, rejected)
        lessons_written = memory.upsert_lessons(db, lessons)

        project.status = "completed"
        project.completed_at = datetime.utcnow()
        db.commit()

        return {
            "project_id": project.id,
            "title": project.title,
            "opportunities": [_serialize_finding(f) for f in findings],
            "rejected_count": len(rejected),
            "lessons_updated": lessons_written,
        }

    except Exception as e:                  # noqa: BLE001 — a failed run must not kill the SSE stream
        db.rollback()
        project.status = "failed"
        project.error = str(e)[:2000]
        project.completed_at = datetime.utcnow()
        db.commit()
        print(f"[market_intel] research run failed: {e}")
        return {"error": f"Research run failed: {e}", "project_id": project.id}
