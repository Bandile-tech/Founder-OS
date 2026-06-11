import os
import re
import json
from openai import OpenAI
from sqlalchemy.orm import Session
from models import AIMemory
from dotenv import load_dotenv

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

SYSTEM_PROMPT = """
You are Bandile's CEO-level personal AI operating system.

You exist to steward his God-given talents, opportunities, and resources with excellence, discipline, and humility. Your role is to think, judge, plan, and advise at the level of an elite founder, strategist, and long-term empire builder, while remaining firmly anchored in Christian stewardship, service, and accountability before God.

FOUNDATIONAL PRINCIPLE (NON-NEGOTIABLE)
God is the ultimate owner. Bandile is a steward, not a consumer.

All intelligence, strategy, ambition, wealth-building, and execution must:
- Honour God
- Multiply entrusted talents
- Serve others, especially the middle class and the needy
- Avoid waste, sloth, ego, and misaligned ambition

CORE MISSION
Bandile's mission is to build Bandile Group Holdings — a technology-first multi-industry conglomerate targeting $1B+ valuation by 2030-2031, impacting 10M+ lives across 8 sectors. Current phase: Student → First Revenue. Immediate target: first AI services client (insurance CEO's PA). Proof of work: Wamu's Bakes & Cakes website.

NORTH STAR KPIs (2035 ceiling, 2030-31 realistic):
- $1B+ Group Valuation
- 10M+ Lives Impacted
- 8 Sectors (Aether to Helios)
- 35-40% Founder Stake retained

CURRENT CONTEXT
- Year 12, Trident College, Zambia
- A-levels: Maths 9709, Further Maths 9231, Business 9609, Economics 9708 (Oct-Nov 2026)
- Competitive sprinter: 100m, 200m, 400m — ISAZ Regionals ~17 May 2026
- Python/CS50P in execution blocks (20:30-21:30 Mon-Thu)
- Governing principle: "Character Before Status"
- Discipleship model: proximity over proclamation

OPERATING RULES
- Direct, strategic, unsentimental
- No fluff, no emojis, no motivational theatre
- Challenge weak thinking and procrastination
- Plain text only in analytical mode
- Think in leverage, compounding, and systems
- Surface what Bandile isn't seeing
"""


def clean_ai_output(text: str) -> str:
    if not text:
        return ""
    text = text.strip()
    text = re.sub(r'\n+', '\n', text)
    text = re.sub(r' +', ' ', text)
    text = re.sub(r'[\.\!\?]{2,}', '.', text)
    return text


def get_chat_response(messages: list) -> str:
    full_messages = [{"role": "system", "content": SYSTEM_PROMPT}] + messages
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=full_messages,
        max_tokens=800
    )
    return response.choices[0].message.content


def get_chat_response_with_memory(
    messages: list,
    db: Session,
    context_type: str = "general",
    project: str = "founder_os",
    instruction_block: str | None = None
) -> str:
    # Fetch recent memory
    recent_memories = db.query(AIMemory).filter(
        AIMemory.context_type == context_type,
        AIMemory.project == project
    ).order_by(AIMemory.created_at.desc()).limit(20).all()

    memory_text = ""
    for m in reversed(recent_memories):
        memory_text += f"\n[Memory @ {m.created_at}]: {m.context_data}\nResponse: {m.response}\n"

    system_prompt = ""
    if instruction_block:
        system_prompt += f"[Temporary Instruction]\n{instruction_block}\n\n"
    system_prompt += f"{memory_text}\n{SYSTEM_PROMPT}"

    full_messages = [{"role": "system", "content": system_prompt}] + messages

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=full_messages,
        max_tokens=800
    )
    raw = response.choices[0].message.content
    clean = clean_ai_output(raw)

    # Store in memory
    memory_entry = AIMemory(
        context_type=context_type,
        project=project,
        context_data=json.dumps([m["content"] for m in messages]),
        response=clean
    )
    db.add(memory_entry)
    db.commit()

    return clean


def get_parse_response(text: str, context: dict) -> dict:
    """
    Parse a brain dump into structured JSON.
    Returns a dict with kpi_updates, todos_add, etc.
    """
    roadmap_ids = context.get("roadmap_ids", [])
    today = context.get("today", "2026-05-03")

    system_prompt = f"""You are the Founder OS parsing engine for Bandile Masocha.

Context: Year 12, Trident College, Zambia. A-levels: Maths 9709, Further Maths 9231, Business 9609, Economics 9708. CIE exams Oct-Nov 2026. Building Aether AI services. Compound lifting: Bench Press, Pull-ups, Squat, Incline DB Press, Barbell Row.

Known roadmap task IDs: {', '.join(roadmap_ids)}

Parse the brain dump and return ONLY valid JSON, no markdown, no explanation.

CRITICAL EXTRACTION RULE: Process each category as a separate, independent extraction pass. A single sentence can contain multiple actionable categories — extract ALL of them. The presence of one category (e.g. a trade) does NOT exclude others (e.g. habits, revenue, KPIs) from the same input. Never skip a category because another was already found.

If the user provides only a P&L figure without an R-multiple, infer r_multiple from context or default to 1.0. Never output null for r_multiple — a null r_multiple causes the trade to be silently dropped by the system.

Example of correct mixed extraction:
Input: "Did scripture today and logged a live trade EURUSD long +$50, all rules followed"
Correct output includes BOTH:
  "habits_done": ["scripture_prayer"]
  "trade_logs": [{{"type": "live", "pair": "EURUSD", "direction": "long", "net_pl_usd": 50, "adherence": true, "outcome": "win", "r_multiple": 1.0}}]

Example:
Input: "Earned K500 from Wamu's and logged a backtest GBPUSD short +1.5R"
Correct output includes BOTH:
  "revenue_updates": [{{"amount": 500, "source": "Wamu's Bakes & Cakes", "client": null}}]
  "trade_logs": [{{"type": "backtest", "pair": "GBPUSD", "direction": "short", "r_multiple": 1.5, "outcome": "win", "adherence": true}}]

Schema:
{{
  "summary": "one-line summary",
  "kpi_updates": [{{"key": "maths_syllabus|further_maths|business|economics", "value": number}}],
  "todos_add": [{{"text": "string", "priority": 1, "category": "health|academics|business|personal", "due": "YYYY-MM-DD|null", "roadmap_id": "id-or-null"}}],
  "todos_complete": ["text fragment"],
  "roadmap_complete": ["roadmap-task-id"],
  "habits_done": ["scripture_prayer|ironing|python_session|sprint_training|academics"],
  "annual_updates": [{{"name_fragment": "string", "current": number}}],
  "revenue_updates": [{{"amount": number, "source": "string", "client": "name-or-null"}}],
  "log_entry": "short log message",
  "advisory": "1-2 sentence strategic insight or null",
  "health_updates": {{
    "sleep_hours": number_or_null,
    "mobility_done": true_or_null,
    "session_done": true_or_null
  }},
  "lift_logs": [
    {{"lift_name": "string", "weight_kg": number, "reps": integer}}
  ],
  "trade_logs": [
    {{
      "type": "backtest or live",
      "pair": "EURUSD or GBPUSD or other pair string",
      "direction": "long or short",
      "r_multiple": number (required — default 1.0 if not stated explicitly),
      "outcome": "win or loss or breakeven or null",
      "adherence": true_or_false_or_null,
      "entry_reason": "string_or_null",
      "account_name": "string_or_null — only for live trades",
      "risk_pct": number_or_null,
      "net_pl_usd": number_or_null,
      "rule_broken": true_or_false_or_null,
      "rule_broken_description": "string_or_null"
    }}
  ]
}}

Health extraction rules: extract sleep_hours from phrases like "slept 7 hours". Set mobility_done=true if mobility/stretching mentioned. Set session_done=true if a training session is mentioned. For lifts, populate lift_logs array — one entry per lift mentioned. lift_name is the exact lift name (e.g. "Bench Press", "Deadlift", "Squat"). If a new lift name is mentioned that isn't in the known list, include it anyway — the system will auto-create it. Omit health_updates key entirely if no health content found. Omit lift_logs key entirely if no lifts mentioned.

Trading extraction rules: populate trade_logs when a trade or backtest is mentioned. type must be "backtest" if replay/backtest/TradingView replay is mentioned, else "live". pair should be inferred from the text (default EURUSD if unclear). direction is "long" or "short". r_multiple is the R gained/lost (positive for wins, negative for stop-hits) — if only a P&L amount is given (e.g. "+$50"), default r_multiple to 1.0 rather than null. outcome: "win" if positive R, "loss" if negative R or stop hit, "breakeven" if 0R. adherence is true unless rule violations are mentioned. Set rule_broken=true and fill rule_broken_description if a rule break is described. Omit trade_logs key entirely if no trading content found. Remember: trade_logs is always extracted in addition to other categories, never instead of them.

Today is {today}. Return valid JSON only."""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text}
        ],
        max_tokens=1000
    )
    raw = response.choices[0].message.content
    try:
        return json.loads(raw.replace("```json", "").replace("```", "").strip())
    except Exception:
        return {
            "summary": "Parse error",
            "kpi_updates": [],
            "todos_add": [],
            "todos_complete": [],
            "roadmap_complete": [],
            "habits_done": [],
            "annual_updates": [],
            "revenue_updates": [],
            "log_entry": "Brain dump parse failed",
            "advisory": None
        }


def get_proactive_brief(context: dict) -> str:
    """Generate an unprompted system-state brief."""
    prompt = f"""Analyse this Founder OS system state and give a 3-4 sentence brief.
What is drifting. What is the single highest-leverage action right now. What is the uncomfortable truth.
Plain text only. No markdown. No flattery. Surgical.

State: {json.dumps(context)}"""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt}
        ],
        max_tokens=300
    )
    return clean_ai_output(response.choices[0].message.content)


def get_radar_scores(context: dict) -> dict:
    """
    Calculate radar domain scores from system data.
    Returns scores 0-100 for each domain.
    """
    kpis = context.get("kpis", {})
    habits = context.get("habits", {})
    annual = context.get("annual_targets", [])
    todos_done_today = context.get("todos_done_today", 0)
    bible_streak = context.get("bible_streak", 0)
    roadmap_pct = context.get("roadmap_pct", {})

    # CORE (identity, discipline, faith, integrity)
    habit_done = sum(1 for h in habits.values() if h)
    habit_total = max(len(habits), 1)
    habit_score = (habit_done / habit_total) * 100
    faith_score = min(bible_streak * 10, 100)
    core = round((habit_score * 0.6 + faith_score * 0.4))

    # HEALTH (mobility, session consistency, sleep — computed in main.py)
    health_ctx = context.get("health", {})
    if health_ctx.get("insufficient_data") or health_ctx.get("score") is None:
        health = 50  # neutral baseline when no data yet
    else:
        health = health_ctx["score"]

    def kpi_pct(k):
        if not k:
            return 0
        if k.get("lower_is_better"):
            gap = k["value"] * 0.15
            d = k["value"] - k["target"]
            return max(0, min(100, ((gap - d) / gap) * 100))
        return min(100, (k["value"] / k["target"]) * 100)

    # INTELLECT — prefer live subject mastery from Subject/Topic/Subtopic;
    # fall back to _kpi_state percentages when subjects have no subtopics yet.
    acad_mastery = context.get("acad_mastery", {})
    tracked = [v for v in acad_mastery.values() if v is not None]
    if tracked:
        intellect = round(sum(tracked) / len(tracked))
    else:
        acad_scores = [
            kpi_pct(kpis.get("maths_syllabus")),
            kpi_pct(kpis.get("further_maths")),
            kpi_pct(kpis.get("business")),
            kpi_pct(kpis.get("economics")),
        ]
        intellect = round(sum(acad_scores) / max(len(acad_scores), 1))

    # BUSINESS — real client/revenue data takes priority, fall back to annual targets
    clients = context.get("clients", [])
    revenue_total = context.get("revenue_total", 0)

    if clients or revenue_total > 0:
        active_count = sum(1 for c in clients if c.get("status") == "active")
        prospect_count = sum(1 for c in clients if c.get("status") == "prospect")
        rev_score = min(60, (revenue_total / 2000) * 60)   # 2000 USD target
        pipeline_score = min(25, prospect_count * 8)
        client_score = min(15, active_count * 15)
        business = round(rev_score + pipeline_score + client_score)
    else:
        biz_targets = [a for a in annual if a.get("category") == "business"]
        if biz_targets:
            biz_scores = []
            for a in biz_targets:
                if a.get("lower_is_better"):
                    pct = kpi_pct({"value": a["current"], "target": a["target"], "lower_is_better": True})
                else:
                    pct = min(100, (a["current"] / max(a["target"], 1)) * 100)
                biz_scores.append(pct)
            business = round(sum(biz_scores) / len(biz_scores))
        else:
            business = 10  # baseline — just started

    # SKILLS (CS50P + Python roadmap)
    python_target = next((a for a in annual if "cs50" in a.get("name", "").lower() or "python" in a.get("name", "").lower()), None)
    skills = round((python_target["current"] / max(python_target["target"], 1)) * 100) if python_target else 15

    # SOCIAL (manual for now — default moderate)
    # Will be driven by a dedicated social score input later
    social = context.get("social_score", 50)

    return {
        "core":      max(0, min(100, core)),
        "health":    max(0, min(100, health)),
        "intellect": max(0, min(100, intellect)),
        "business":  max(0, min(100, business)),
        "skills":    max(0, min(100, skills)),
        "social":    max(0, min(100, social)),
    }
