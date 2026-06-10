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

Context: Year 12, Trident College, Zambia. A-levels: Maths 9709, Further Maths 9231, Business 9609, Economics 9708. Sprinter (100m/200m/400m). ISAZ Regionals ~17 May 2026. CIE exams Oct-Nov 2026. Building Aether AI services.

Known roadmap task IDs: {', '.join(roadmap_ids)}

Parse the brain dump and return ONLY valid JSON, no markdown, no explanation.

Schema:
{{
  "summary": "one-line summary",
  "kpi_updates": [{{"key": "sprint_100m|sprint_200m|sprint_400m|maths_syllabus|further_maths|business|economics", "value": number}}],
  "todos_add": [{{"text": "string", "priority": 1, "category": "athletics|academics|business|personal", "due": "YYYY-MM-DD|null", "roadmap_id": "id-or-null"}}],
  "todos_complete": ["text fragment"],
  "roadmap_complete": ["roadmap-task-id"],
  "habits_done": ["scripture_prayer|ironing|python_session|sprint_training|academics"],
  "annual_updates": [{{"name_fragment": "string", "current": number}}],
  "revenue_updates": [{{"amount": number, "source": "string", "client": "name-or-null"}}],
  "log_entry": "short log message",
  "advisory": "1-2 sentence strategic insight or null"
}}

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

    # PHYSICAL (sprint KPI progress)
    def kpi_pct(k):
        if not k:
            return 0
        if k.get("lower_is_better"):
            gap = k["value"] * 0.15
            d = k["value"] - k["target"]
            return max(0, min(100, ((gap - d) / gap) * 100))
        return min(100, (k["value"] / k["target"]) * 100)

    sprint_scores = [
        kpi_pct(kpis.get("sprint_100m")),
        kpi_pct(kpis.get("sprint_200m")),
        kpi_pct(kpis.get("sprint_400m")),
    ]
    physical = round(sum(sprint_scores) / max(len(sprint_scores), 1))

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
        "core": max(0, min(100, core)),
        "physical": max(0, min(100, physical)),
        "intellect": max(0, min(100, intellect)),
        "business": max(0, min(100, business)),
        "skills": max(0, min(100, skills)),
        "social": max(0, min(100, social)),
    }
