"""
Daily briefing — GET /briefing/today

Deterministic core (no LLM needed): overdue and carried-over tasks, today's tasks,
non-negotiables, deadlines in the next 14 days, exam countdowns, syllabus priorities,
recent weakness notes, critical annual targets and alerts — every item with a title,
category, due label, reason, suggested action and target id, ranked.

Optional summary: at most one LLM call per day, cached in ``briefing_summaries``;
regenerated only with ``?refresh=1``. Works fully with no OpenAI key.

Response shape (the target home screen is built against this):
{
  "date": "2026-11-04", "weekday": "Wednesday", "badge": "EXECUTION DAY",
  "greeting": "Good afternoon", "greeting_line": "...", "summary": "..." | null,
  "summary_source": "llm" | "deterministic", "summary_cached": bool,
  "items": [ {id, title, category, due_label, due_date, reason, suggested_action,
              target_type, target_id, completable, score}, ... ],
  "attention_count": int, "alert_count": int, "alerts": [...],
  "progress": {"academics": int|null, "business": int, "targets": int|null, "habits": int},
  "counts": {...}, "exams": [...], "non_negotiables": [...], "weakness_notes": [...]
}
"""

import re
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

import models
import llm
import syllabus
from database import get_db

router = APIRouter()

DEADLINE_WINDOW_DAYS = 14
TEN_K_TARGET_USD = 10000


def _greeting(now: datetime) -> str:
    h = now.hour
    if h < 12:
        return "Good morning"
    if h < 17:
        return "Good afternoon"
    return "Good evening"


def _badge(d: date) -> str:
    return {5: "BUILD DAY", 6: "REVIEW DAY"}.get(d.weekday(), "EXECUTION DAY")


def _due_label(d: date | None, today: date, time_label: str | None = None) -> str:
    if time_label:
        return time_label
    if d is None:
        return ""
    if d == today:
        return "Today"
    if d == today + timedelta(days=1):
        return "Tomorrow"
    if d < today:
        days = (today - d).days
        return f"{days}d overdue" if days > 1 else "Yesterday"
    return f"Due {d.day} {d.strftime('%b')}"


def _item(*, title, category, reason, action, target_type, target_id, score, due=None, today=None,
          due_label=None, completable=False, extra=None):
    out = {
        "id": f"{category}:{target_type}:{target_id}",
        "title": title, "category": category,
        "due_label": due_label if due_label is not None else _due_label(due, today),
        "due_date": due.isoformat() if isinstance(due, date) else None,
        "reason": reason, "suggested_action": action,
        "target_type": target_type, "target_id": target_id,
        "completable": completable, "score": round(score, 2),
    }
    if extra:
        out.update(extra)
    return out


# ── Sections ─────────────────────────────────────────────────

def _task_items(db: Session, today: date) -> tuple[list, dict]:
    open_todos = db.query(models.Todo).filter(models.Todo.done == False).all()
    items, counts = [], {"overdue": 0, "today": 0, "upcoming": 0, "undated": 0}
    horizon = today + timedelta(days=DEADLINE_WINDOW_DAYS)
    for t in open_todos:
        pr = max(1, min(9, t.priority or 5))
        prio_bonus = (9 - pr) / 8          # 1.0 for priority 1 … 0 for priority 9
        if t.due_date and t.due_date < today:
            days = (today - t.due_date).days
            counts["overdue"] += 1
            items.append(_item(title=t.text, category="task", due=t.due_date, today=today,
                               reason=f"Overdue by {days} day{'s' if days != 1 else ''} — carried over",
                               action="Do it first or re-date it", target_type="todo", target_id=t.id,
                               completable=True, score=90 + min(days, 9) + prio_bonus * 5))
        elif t.due_date == today:
            counts["today"] += 1
            items.append(_item(title=t.text, category="task", due=t.due_date, today=today,
                               reason="Due today", action="Complete it today",
                               target_type="todo", target_id=t.id, completable=True, score=70 + prio_bonus * 10))
        elif t.due_date and t.due_date <= horizon:
            counts["upcoming"] += 1
            days = (t.due_date - today).days
            items.append(_item(title=t.text, category="deadline", due=t.due_date, today=today,
                               reason=f"Due in {days} day{'s' if days != 1 else ''}", action="Schedule a block for it",
                               target_type="todo", target_id=t.id, completable=True,
                               score=40 + (DEADLINE_WINDOW_DAYS - days) * 1.5 + prio_bonus * 5))
        else:
            counts["undated"] += 1 if not t.due_date else 0
            if pr <= 2:
                items.append(_item(title=t.text, category="task", due=t.due_date, today=today,
                                   reason="High priority, no near deadline", action="Decide when it happens",
                                   target_type="todo", target_id=t.id, completable=True, score=30 + prio_bonus * 10))
    return items, counts


def _non_negotiables(db: Session, today: date) -> tuple[list, list, int]:
    active = db.query(models.NonNegotiable).filter(models.NonNegotiable.is_active == True).order_by(
        models.NonNegotiable.sort_order, models.NonNegotiable.id).all()
    existing = {h.key: h for h in db.query(models.Habit).filter(models.Habit.date == today).all()}
    rows, items = [], []
    done = 0
    for nn in active:
        h = existing.get(nn.key)
        is_done = bool(h and h.done)
        done += 1 if is_done else 0
        rows.append({"key": nn.key, "label": nn.label, "done": is_done, "habit_id": h.id if h else None})
        if not is_done:
            items.append(_item(title=nn.label, category="non_negotiable", due=today, today=today, due_label="Today",
                               reason="Daily non-negotiable not yet done", action="Tick it off",
                               target_type="habit", target_id=h.id if h else None, completable=False,
                               score=55, extra={"habit_key": nn.key}))
    pct = round(100 * done / len(active)) if active else 0
    return rows, items, pct


def _exam_items(db: Session, today: date) -> tuple[list, list]:
    exams = [e for e in syllabus.upcoming_exams(db, today) if e["days_away"] >= 0]
    items = []
    for e in exams[:6]:
        d = e["days_away"]
        label = f"{e['subject']}{' ' + e['paper'] if e['paper'] else ''} exam"
        items.append(_item(title=label, category="exam", due=date.fromisoformat(e["exam_date"]), today=today,
                           due_label=f"{d} days" if d > 1 else ("Tomorrow" if d == 1 else "Today"),
                           reason=f"Exam in {d} day{'s' if d != 1 else ''}", action="Check the weakest topics below",
                           target_type="exam", target_id=f"{e['subject_code']}:{e['paper'] or ''}",
                           score=max(5.0, 80 - d * 0.8)))
    return exams, items


def _syllabus_items(db: Session, today: date) -> tuple[list, list]:
    weakest = syllabus.weakest_topics(db, limit=5, today=today)
    items = []
    for w in weakest:
        if w["status"] not in ("not_started", "learned"):
            continue
        label = f"{w['subject_name']}{' ' + w['paper'] if w['paper'] else ''}: {w['topic_code']} {w['title']}"
        items.append(_item(title=label, category="syllabus", due=None, today=today,
                           due_label=f"{w['exam_days_away']}d to exam" if w["exam_days_away"] is not None else "",
                           reason=w["reason"].capitalize(), action="Revise it and log the outcome",
                           target_type="syllabus_point", target_id=w["id"], score=20 + w["score"] * 4,
                           extra={"status": w["status"], "colour": w["colour"]}))
    notes = []
    for p in db.query(models.SyllabusPoint).filter(models.SyllabusPoint.weakness_notes.isnot(None)).order_by(
            models.SyllabusPoint.updated_at.desc()).limit(5).all():
        last = (p.weakness_notes or "").strip().splitlines()[-1] if (p.weakness_notes or "").strip() else ""
        m = re.match(r"^-?\s*(\d{4}-\d{2}-\d{2}):\s*(.*)$", last.strip())
        note_text, note_date = (m.group(2), m.group(1)) if m else (last.lstrip("- ").strip(), None)
        notes.append({"syllabus_point_id": p.id, "subject": p.subject_name, "paper": p.paper,
                      "topic": f"{p.topic_code} {p.title}", "note": note_text, "date": note_date, "status": p.status})
    return items, notes


def _target_items(db: Session, today: date) -> tuple[list, int | None]:
    import main
    year_pct = main._year_pct()
    targets = db.query(models.AnnualTarget).filter(models.AnnualTarget.is_active == True).all()
    items, pcts = [], []
    for t in targets:
        ser = main._serialize_annual_target(t, year_pct)
        if ser["is_numeric"]:
            pcts.append(ser["progress_pct"])
        if ser["status"] == "critical":
            items.append(_item(title=ser["name"], category="target", due=None, today=today,
                               due_label=f"{ser['progress_pct']}% vs {ser['expected_pct']}% expected",
                               reason="Annual target is critical", action="Re-plan or re-sequence this week",
                               target_type="annual_target", target_id=t.id, score=60))
    return items, (round(sum(pcts) / len(pcts)) if pcts else None)


def _alert_items(db: Session, today: date) -> tuple[list, list]:
    import orchestrator_tools
    alerts, _ = orchestrator_tools.detect_off_track(db)
    items = []
    for a in alerts:
        sev = a.get("severity", "info")
        items.append(_item(title=a["message"].split(".")[0], category="alert", due=None, today=today, due_label=sev.upper(),
                           reason=a["message"], action="Review and act", target_type="alert", target_id=a["rule_id"],
                           score={"critical": 65, "warn": 45, "info": 15}.get(sev, 15),
                           extra={"alert_check": a.get("check")}))
    return alerts, items


def _business_pct(db: Session) -> int:
    import main
    usd = main._revenue_usd_total(db, since=main.TEN_K_START_DATE)
    return max(0, min(100, round(100 * usd / TEN_K_TARGET_USD)))


# ── Summary (optional, cached, one call per day) ─────────────

def _deterministic_greeting_line(core: dict) -> str:
    month = core["today_date"].strftime("%B")
    targets = core["progress"]["targets"]
    n = core["attention_count"]
    first = f"You're {targets}% through your {month} targets." if targets is not None else "Your targets are not set yet."
    if n == 0:
        second = "Nothing needs your attention right now."
    elif n == 1:
        second = "One item needs your attention."
    else:
        words = {2: "Two", 3: "Three", 4: "Four", 5: "Five"}
        second = f"{words.get(n, n)} items need your attention."
    return f"{first} {second}"


def _summary_prompt(core: dict) -> list[dict]:
    lines = [f"- [{i['category']}] {i['title']} ({i['due_label']}) — {i['reason']}" for i in core["items"][:12]]
    facts = (
        f"Date: {core['date']} ({core['weekday']}).\n"
        f"Progress: {core['progress']}.\n"
        f"Counts: {core['counts']}.\n"
        f"Exams: {[(e['subject'], e['paper'], e['days_away']) for e in core['exams'][:4]]}.\n"
        f"Alerts: {[a['message'] for a in core['alerts']]}.\n"
        f"Ranked items:\n" + "\n".join(lines)
    )
    system = (
        "You write the opening of Bandile's daily briefing. 3 to 5 sentences, plain text, no "
        "lists, no emojis, no motivational theatre. Direct and specific: what matters most today, "
        "what is slipping, the single highest-leverage move. First sentence must stand alone as a "
        "one-line greeting subtitle. Use only the facts given."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": facts}]


def _summary(db: Session, core: dict, refresh: bool) -> tuple[str | None, str | None, bool]:
    """(summary, greeting_line, cached). One LLM call per day unless refreshed."""
    today = core["today_date"]
    row = db.query(models.BriefingSummary).filter(models.BriefingSummary.day == today).first()
    if row is not None and not refresh:
        return row.summary, row.greeting_line, True
    if not llm.is_configured():
        return None, None, False
    try:
        text = llm.chat_text(_summary_prompt(core), db=db, purpose="briefing_summary", max_tokens=300).strip()
    except Exception as exc:  # noqa: BLE001
        print(f"[briefing] summary failed: {exc}")
        return (row.summary, row.greeting_line, True) if row else (None, None, False)
    if not text:
        return None, None, False
    first = text.split(". ")[0].strip()
    greeting_line = first if first.endswith((".", "!", "?")) else first + "."
    if row is None:
        row = models.BriefingSummary(day=today)
        db.add(row)
    row.summary, row.greeting_line, row.model, row.created_at = text, greeting_line, llm.assistant_model(), datetime.utcnow()
    db.commit()
    return text, greeting_line, False


# ── Assemble ─────────────────────────────────────────────────

def build_briefing(db: Session, *, refresh: bool = False, now: datetime | None = None, with_summary: bool = True) -> dict:
    now = now or datetime.now()
    today = now.date()

    task_items, counts = _task_items(db, today)
    nn_rows, nn_items, habits_pct = _non_negotiables(db, today)
    exams, exam_items = _exam_items(db, today)
    syl_items, weakness_notes = _syllabus_items(db, today)
    target_items, targets_pct = _target_items(db, today)
    alerts, alert_items = _alert_items(db, today)

    items = task_items + nn_items + exam_items + syl_items + target_items + alert_items
    items.sort(key=lambda i: i["score"], reverse=True)

    attention = counts["overdue"] + len(target_items) + sum(1 for a in alerts if a.get("severity") == "critical")

    core = {
        "date": today.isoformat(), "today_date": today, "weekday": today.strftime("%A"), "badge": _badge(today),
        "greeting": _greeting(now),
        "items": items,
        "attention_count": attention,
        "alert_count": len(alerts) + counts["overdue"],
        "alerts": alerts,
        "progress": {
            "academics": syllabus.academics_pct(db),
            "business": _business_pct(db),
            "targets": targets_pct,
            "habits": habits_pct,
        },
        "counts": {**counts, "items": len(items), "non_negotiables_done": sum(1 for r in nn_rows if r["done"]),
                   "non_negotiables_total": len(nn_rows), "exams_upcoming": len(exams)},
        "exams": exams[:6],
        "non_negotiables": nn_rows,
        "weakness_notes": weakness_notes,
    }

    summary, greeting_line, cached = (None, None, False)
    if with_summary:
        summary, greeting_line, cached = _summary(db, core, refresh)
    core["summary"] = summary
    core["summary_source"] = "llm" if summary else "deterministic"
    core["summary_cached"] = cached
    core["greeting_line"] = greeting_line or _deterministic_greeting_line(core)
    core.pop("today_date")
    return core


@router.get("/briefing/today")
def briefing_today(refresh: int = Query(0, ge=0, le=1), db: Session = Depends(get_db)):
    return build_briefing(db, refresh=bool(refresh))


@router.get("/deadlines")
def deadlines_endpoint(days: int = Query(14, ge=1, le=90), db: Session = Depends(get_db)):
    """Overdue, due today, upcoming tasks and exams — the data behind the deadlines view."""
    import orchestrator_tools
    result, _summary = orchestrator_tools.get_deadlines(db, days)
    return result
