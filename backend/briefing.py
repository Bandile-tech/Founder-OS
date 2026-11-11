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
  "business_detail": {"revenue_pct": int|null, "mrr_usd": float, "outstanding_usd": float,
                      "overdue_count": int},
  "counts": {...}, "exams": [...], "non_negotiables": [...], "weakness_notes": [...]
}

Phase 5 adds the item categories ``payment``, ``deal``, ``client``, ``project`` (target
types of the same names), ``outreach`` (target types ``outreach_message`` and ``lead``) and
``opportunity`` (target types ``research_finding``, ``opportunity_pipeline`` and
``research_objective``);
none of them is completable from a checkbox. ``progress.projects`` is the share of projects
delivered or closed in the last 90 days out of those plus the active ones (null with no
projects).
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
    import assistant_state
    day = assistant_state.routine_day(db, today)
    rows, items = [], []
    for nn in day["items"]:
        rows.append({"key": nn["key"], "label": nn["label"], "done": nn["done"], "habit_id": nn["habit_id"]})
        if not nn["done"]:
            items.append(_item(title=nn["label"], category="non_negotiable", due=today, today=today, due_label="Today",
                               reason="Daily non-negotiable not yet done", action="Tick it off",
                               target_type="habit", target_id=nn["habit_id"], completable=False,
                               score=55, extra={"habit_key": nn["key"]}))
    return rows, items, day["pct"]


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


def _opportunity_items(db: Session, today: date) -> tuple[list, int]:
    """Research output that is waiting on a decision: fresh `pursue` findings, pipeline
    entries that have gone stale or missed their date, and standing objectives that are due.
    Returns the items and the number of new findings (not capped)."""
    from market_intel import objectives
    now = datetime.utcnow()
    since = now - timedelta(days=14)
    fresh = db.query(models.ResearchFinding).filter(
        models.ResearchFinding.status == "surfaced", models.ResearchFinding.recommendation == "pursue",
        models.ResearchFinding.reviewed_at.is_(None), models.ResearchFinding.created_at >= since,
    ).order_by(models.ResearchFinding.overall_score.desc(), models.ResearchFinding.id.desc()).all()
    items = []
    for f in fresh[:3]:
        items.append(_item(title=f.problem[:90], category="opportunity", today=today,
                           due_label=f"{f.overall_score}/10", reason="Verified opportunity, not yet reviewed",
                           action="Promote or dismiss", target_type="research_finding", target_id=f.id,
                           score=35))
    for e in db.query(models.OpportunityPipeline).filter(
            models.OpportunityPipeline.stage.in_(("discovered", "researching"))).all():
        title = (e.finding.problem if e.finding else f"Pipeline entry {e.id}")[:90]
        overdue = e.due_date is not None and e.due_date < today
        idle = (now - e.updated_at).days if e.updated_at else 0
        if overdue:
            items.append(_item(title=title, category="opportunity", due=e.due_date, today=today,
                               reason="Next action overdue", action=e.next_action or "Decide: validate or reject",
                               target_type="opportunity_pipeline", target_id=e.id, score=48))
        elif idle >= 14:
            items.append(_item(title=title, category="opportunity", today=today, due_label="",
                               reason=f"Pipeline entry stale for {idle} days",
                               action=e.next_action or "Decide: validate or reject",
                               target_type="opportunity_pipeline", target_id=e.id, score=30))
    # Once the market-intelligence agent can run them itself (enabled, with an AI key), due
    # objectives are its job and need no tap.
    agent = db.query(models.Agent).filter(models.Agent.key == "market_intel").first()
    agent_runs_them = llm.is_configured() and (agent is None or bool(agent.enabled))
    for o in ([] if agent_runs_them else objectives.due_objectives(db, now)):
        items.append(_item(title=f"Research due: {o.objective[:70]}", category="opportunity", today=today,
                           due_label="", reason=f"Runs every {o.cadence_days} days and is due",
                           action="Run it (one tap on Agents)", target_type="research_objective",
                           target_id=o.id, score=12))
    return items, len(fresh)


def _portfolio_items(db: Session, today: date) -> tuple[list, int | None]:
    """Blocked, overdue and waiting projects, plus the one project to focus on. Returns the
    items and the delivery progress (delivered or closed in 90 days ÷ that plus active)."""
    import portfolio
    try:
        result = portfolio.full_review(db)
    except Exception as exc:  # noqa: BLE001 — the briefing must still render
        db.rollback()
        print(f"[briefing] portfolio review failed: {exc}")
        return [], None
    items = []
    focus_done = False
    for e in result["ranked"]:
        p, att = e["project"], e["attention"]
        if p["status"] != "active":
            continue
        pid, who = p["id"], p["client_name"]
        title = p["title"] + (f" — {who}" if who else "")
        due = date.fromisoformat(str(p["due_date"])[:10]) if p["due_date"] else None
        if att["components"]["blocked"]:
            items.append(_item(title=title, category="project", due=due, today=today, due_label="BLOCKED",
                               reason=f"Blocked: {p['blocked_reason']}", action="Unblock or re-plan",
                               target_type="project", target_id=pid, score=62))
        elif att["overdue_days"]:
            days = att["overdue_days"]
            items.append(_item(title=title, category="project", due=due, today=today, due_label=f"{days}d overdue",
                               reason=f"Due {days} day{'s' if days != 1 else ''} ago", action="Deliver or re-date",
                               target_type="project", target_id=pid, score=70 + min(days, 9)))
        elif att["components"]["waiting"]:
            items.append(_item(title=title, category="project", due=due, today=today, due_label="",
                               reason=f"Waiting on {who or 'the client'} for {att['waiting_days']} days",
                               action=f"Nudge {who or 'the client'}", target_type="project", target_id=pid,
                               score=36))
        elif e["rank"] == 1 and e["recommendation"] == "focus" and not focus_done:
            items.append(_item(title=title, category="project", due=due, today=today, due_label="FOCUS",
                               reason=_cap_first(e["bottleneck"]) if e["bottleneck"] else "Highest attention score",
                               action=p["next_action"] or "Decide the next action", target_type="project",
                               target_id=pid, score=45))
        if e["rank"] == 1:
            focus_done = True
    since = datetime.utcnow() - timedelta(days=90)
    done = db.query(models.Project).filter(
        models.Project.status.in_(("delivered", "closed")), models.Project.delivered_at >= since).count()
    active = result["totals"]["active"]
    progress = round(100 * done / max(1, done + active)) if (done or active) else None
    return items, progress


def _cap_first(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def _outreach_items(db: Session, today: date) -> tuple[list, int]:
    """Drafts waiting for approval and contacted leads that have gone quiet."""
    names = {l.id: l for l in db.query(models.Lead).all()}
    now = datetime.utcnow()
    stale_pending = {p.id for p in db.query(models.PendingAction).filter(
        models.PendingAction.kind == "send_outreach",
        models.PendingAction.created_at < now - timedelta(hours=24)).all()}
    drafts = [m for m in db.query(models.OutreachMessage).filter(
        models.OutreachMessage.direction == "out",
        models.OutreachMessage.status.in_(("draft", "approved")),
    ).order_by(models.OutreachMessage.id.desc()).all() if m.pending_action_id not in stale_pending]
    items = []
    for m in drafts[:3]:
        lead = names.get(m.lead_id)
        who = lead.name if lead else "a lead"
        items.append(_item(title=f"{m.channel.capitalize()} draft to {who}", category="outreach", today=today,
                           due_label="", reason="Draft waiting for your approval",
                           action="Approve & send or dismiss", target_type="outreach_message",
                           target_id=m.id, score=38))
    quiet = []
    for lead in names.values():
        if lead.status != "contacted" or lead.last_contact_at is None:
            continue
        days = (now - lead.last_contact_at).days
        if days <= 7:
            continue
        has_follow_up = db.query(models.Todo).filter(
            models.Todo.lead_id == lead.id, models.Todo.done == False,   # noqa: E712
            models.Todo.category == "outreach").first() is not None
        if not has_follow_up:
            quiet.append((days, lead))
    quiet.sort(key=lambda pair: -pair[0])
    for days, lead in quiet[:3]:
        items.append(_item(title=f"{lead.name} has not replied", category="outreach", today=today,
                           due_label="", reason=f"No reply in {days} days", action="Follow up or mark lost",
                           target_type="lead", target_id=lead.id, score=28))
    return items, len(drafts)


def _crm_items(db: Session, today: date) -> tuple[list, dict]:
    """Payments, deals and quiet clients. Runs the CRM housekeeping first (recurring
    instalments that have fallen due, overdue flags) so the list is current."""
    import crm
    try:
        crm.housekeeping(db, "job:crm")
    except Exception as exc:  # noqa: BLE001 — the briefing must still render
        db.rollback()
        print(f"[briefing] crm housekeeping failed: {exc}")
    names = {c.id: c.name for c in db.query(models.Client).all()}
    items, overdue_count = [], 0
    open_payments = db.query(models.Payment).filter(models.Payment.status.in_(crm.OPEN_PAYMENT)).all()
    for p in open_payments:
        if p.due_date is None:
            continue
        who = names.get(p.client_id, "a client")
        label = crm.money_label(p.amount, p.currency)
        if p.due_date < today:
            days = (today - p.due_date).days
            overdue_count += 1
            items.append(_item(title=f"{label} from {who}", category="payment", due=p.due_date, today=today,
                               due_label=f"{days}d overdue", reason=f"{label} from {who} overdue",
                               action="Chase it or mark paid", target_type="payment", target_id=p.id,
                               score=72 + min(days, 14)))
        elif p.due_date <= today + timedelta(days=7):
            items.append(_item(title=f"{label} from {who}", category="payment", due=p.due_date, today=today,
                               reason=f"{label} from {who} is due", action="Expect payment",
                               target_type="payment", target_id=p.id, score=42))
    now = datetime.utcnow()
    for d in db.query(models.Deal).filter(models.Deal.stage.notin_(("won", "lost"))).all():
        stale = (d.stage == "negotiation" and d.updated_at is not None
                 and (now - d.updated_at).days > crm.AMBER_NEGOTIATION_DAYS)
        late = d.expected_close is not None and d.expected_close < today
        if stale or late:
            who = names.get(d.client_id, "a client")
            reason = ("Expected to close by now" if late
                      else f"In negotiation for {(now - d.updated_at).days} days")
            items.append(_item(title=f"{d.title} — {who}", category="deal", due=d.expected_close, today=today,
                               reason=reason, action="Close or mark lost", target_type="deal",
                               target_id=d.id, score=40))
    quiet = []
    for c in db.query(models.Client).filter(models.Client.status == "active").all():
        last = crm.last_contact_at(db, c) or c.created_at
        if last is not None and (now - last).days >= crm.AMBER_SILENCE_DAYS:
            quiet.append(((now - last).days, c))
    quiet.sort(key=lambda pair: -pair[0])
    for days, c in quiet[:3]:
        items.append(_item(title=f"Check in with {c.name}", category="client", today=today, due_label="",
                           reason=f"No contact in {days} days", action="Check in", target_type="client",
                           target_id=c.id, score=26))
    recurring = db.query(models.RecurringRevenue).filter(models.RecurringRevenue.active == True).all()   # noqa: E712
    detail = {
        "revenue_pct": _business_pct(db),
        "mrr_usd": crm._totals(recurring, amount_of=crm.monthly_amount)["total_usd"],
        "outstanding_usd": crm._totals(open_payments)["total_usd"],
        "overdue_count": overdue_count,
    }
    return items, detail


def _business_pct(db: Session) -> int | None:
    """Progress of the revenue annual target, or None when no revenue target is set.
    (The old $10K scoreboard is no longer read.)"""
    import assistant_state
    t = assistant_state.revenue_target(db)
    return t["progress_pct"] if t else None


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
    crm_items, business_detail = _crm_items(db, today)
    outreach_items, drafts_waiting = _outreach_items(db, today)
    portfolio_items, projects_pct = _portfolio_items(db, today)
    opportunity_items, opportunities_new = _opportunity_items(db, today)
    alerts, alert_items = _alert_items(db, today)

    items = (task_items + nn_items + exam_items + syl_items + target_items + alert_items
             + crm_items + outreach_items + portfolio_items + opportunity_items)
    items.sort(key=lambda i: i["score"], reverse=True)

    counts["payments_overdue"] = business_detail["overdue_count"]
    counts["drafts_waiting"] = drafts_waiting
    counts["opportunities_new"] = opportunities_new          # informational: not in attention_count
    attention = (counts["overdue"] + len(target_items)
                 + sum(1 for a in alerts if a.get("severity") == "critical")
                 + counts["payments_overdue"])

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
            "projects": projects_pct,
        },
        "business_detail": business_detail,
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
