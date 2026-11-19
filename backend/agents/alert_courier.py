"""
Alert courier — turns what needs attention into Telegram messages, once each.

On every tick: the off-track rules at or above the severity set in Settings (one message
per rule per day) and exams at the configured countdown marks. On events: an overdue
payment, a lead that replied, an application deadline coming up, a failed brand sync.
Every message has a dedupe key, so running twice sends nothing twice. Does nothing until
Telegram is paired.
"""

import json
from datetime import date

import agents
from agents.base import AgentResult, AgentSpec, Permissions, Schedule

_RANK = {"info": 0, "warn": 1, "critical": 2}
_TITLES = {"info": "Note", "warn": "Warning", "critical": "CRITICAL"}
EVENTS = ["payment.overdue", "lead.replied", "application.deadline_near", "brand.sync_failed"]


def _payload(event) -> dict:
    try:
        data = json.loads(event.payload_json or "{}")
    except (ValueError, TypeError):
        data = {}
    return data if isinstance(data, dict) else {}


def _days(n) -> str:
    return "today" if n == 0 else "tomorrow" if n == 1 else f"in {n} days"


def _short_date(iso) -> str:
    try:
        d = date.fromisoformat(str(iso))
    except (ValueError, TypeError):
        return ""
    return f"{d.day} {d.strftime('%b')}"


def _from_event(ctx, event) -> dict | None:
    """The notification an event asks for, or None when it cannot be described."""
    import models
    p, day = _payload(event), ctx.today.isoformat()
    if event.kind == "payment.overdue":
        payment_id = p.get("payment_id")
        if payment_id is None:
            return None
        import crm
        client = ctx.db.query(models.Client).filter(models.Client.id == p.get("client_id")).first() \
            if p.get("client_id") is not None else None
        who = f" from {client.name}" if client is not None else ""
        late = p.get("days_overdue")
        how_late = f"{late} day{'s' if late != 1 else ''} overdue" if isinstance(late, int) and late > 0 else "overdue"
        return {"kind": "alert", "title": "Payment overdue", "target": "business:clients",
                "body": f"Payment{who} of {crm.money_label(p.get('amount'), p.get('currency'))} is {how_late}.",
                "dedupe_key": f"payment:{payment_id}:{day}"}
    if event.kind == "lead.replied":
        lead_id = p.get("lead_id")
        if lead_id is None:
            return None
        lead = ctx.db.query(models.Lead).filter(models.Lead.id == lead_id).first()
        name = (lead.name if lead is not None else None) or p.get("name") or "A lead"
        return {"kind": "alert", "title": "A lead replied", "body": f"{name} replied. Open the pipeline to answer.",
                "dedupe_key": f"lead_replied:{lead_id}", "target": "business:pipeline"}
    if event.kind == "application.deadline_near":
        app_id = p.get("application_id")
        if app_id is None:
            return None
        days, missing = p.get("days"), p.get("missing")
        body = f"{p.get('name') or 'An application'} is due {_days(days)}"
        if _short_date(p.get("deadline")):
            body += f" ({_short_date(p.get('deadline'))})"
        body += "."
        if isinstance(missing, int) and missing > 0:
            body += f" {missing} requirement{'s' if missing != 1 else ''} still open."
        return {"kind": "deadline", "title": "Application deadline", "body": body, "target": "applications",
                "dedupe_key": f"deadline:application:{app_id}:{p.get('mark', days)}"}
    if event.kind == "brand.sync_failed":
        account_id = p.get("account_id")
        if account_id is None:
            return None
        import integrations
        reason = integrations.redact(p.get("error") or "no reason given", ctx.db)[:200]
        return {"kind": "alert", "title": "Brand sync failed", "target": "business:brand",
                "body": f"{str(p.get('platform') or 'An account').capitalize()} could not be synced: {reason}",
                "dedupe_key": f"brand_sync_failed:{account_id}:{day}"}
    return None


def run(ctx) -> AgentResult:
    try:
        import briefing
        import crm
        import models
        import notify
        import orchestrator_tools
        import syllabus
    except ImportError:
        return AgentResult(status="skipped", summary="notifications not built")
    settings = notify.settings(ctx.db)
    if not settings.enabled:
        return AgentResult(status="skipped", summary="notifications are off")
    if notify.active_channel(ctx.db, settings) is None:
        return AgentResult(status="skipped", summary="no device has notifications on, and Telegram is not paired")

    # Everything below is a query and a sentence: no model call.
    day, month, wanted = ctx.today.isoformat(), ctx.today.strftime("%Y-%m"), []
    floor = _RANK.get(settings.alert_min_severity or "warn", 1)
    alerts, _ = orchestrator_tools.detect_off_track(ctx.db)
    for alert in alerts:
        severity = alert.get("severity", "info")
        if _RANK.get(severity, 0) >= floor:
            wanted.append({"kind": "alert", "title": _TITLES.get(severity, "Note"), "body": alert["message"],
                           "dedupe_key": f"alert:{alert['rule_id']}:{day}", "target": "home"})

    # Founder OS's own alerts: the cost cap once a month, a schedule that stopped once a day.
    for alert in briefing.system_alerts(ctx.db):
        if _RANK.get(alert.get("severity", "info"), 0) < floor:
            continue
        cap = alert["rule_id"] == "llm_cost_cap"
        wanted.append({"kind": "alert", "title": "AI spend" if cap else "Schedule", "body": alert["message"],
                       "dedupe_key": f"alert:{alert['rule_id']}:{month if cap else day}",
                       "target": "settings" if cap else "agents"})

    marks = set(notify.deadline_days(settings))
    for exam in syllabus.upcoming_exams(ctx.db, ctx.today):
        days = exam["days_away"]
        if days in marks:
            name = f"{exam['subject']}{' ' + exam['paper'] if exam['paper'] else ''}"
            wanted.append({"kind": "deadline", "title": "Exam countdown", "target": "syllabus",
                           "body": f"{name} exam {_days(days)} ({_short_date(exam['exam_date'])}).",
                           "dedupe_key": f"deadline:exam:{exam['subject_code']}:{exam['paper'] or ''}:{days}"})

    # Tasks with a date have reminder days of their own (3 and 1 days out by default):
    # a task is nearer and smaller than an exam, and a month's notice of it is noise.
    task_marks = set(notify.task_deadline_days(settings))
    for todo in ctx.db.query(models.Todo).filter(models.Todo.done.is_(False), models.Todo.due_date.isnot(None)).all():
        days = (todo.due_date - ctx.today).days
        if days in task_marks and days > 0:
            wanted.append({"kind": "deadline", "title": "Deadline", "target": "home",
                           "body": f"{todo.text} is due {_days(days)} ({_short_date(todo.due_date.isoformat())}).",
                           "dedupe_key": f"deadline:todo:{todo.id}:{days}"})

    # Client payments that fall due today.
    for payment in ctx.db.query(models.Payment).filter(models.Payment.status == "due",
                                                        models.Payment.due_date == ctx.today).all():
        client = ctx.db.query(models.Client).filter(models.Client.id == payment.client_id).first()
        who = f" from {client.name}" if client is not None else ""
        wanted.append({"kind": "alert", "title": "Payment due today", "target": "business:clients",
                       "body": f"Payment{who} of {crm.money_label(payment.amount, payment.currency)} is due today.",
                       "dedupe_key": f"payment_due:{payment.id}"})

    for event in ctx.events:
        try:
            item = _from_event(ctx, event)
        except Exception as exc:                 # noqa: BLE001 — one odd event must not stop the rest
            ctx.log(f"{event.kind}: {type(exc).__name__}: {exc}")
            item = None
        if item is not None:
            wanted.append(item)

    queued = 0
    for item in wanted:
        result = ctx.notify(**item)
        if not result.get("error") and not result.get("deduped"):
            queued += 1
    ctx.db.commit()
    outcome = notify.dispatch(ctx.db, ctx.now)
    if not queued and not any(outcome.values()):
        return AgentResult(status="skipped", summary="nothing new to send", result=outcome)
    parts = [f"{queued} queued"] if queued else []
    parts += [f"{count} {label}" for label, count in outcome.items() if count]
    return AgentResult(summary=", ".join(parts), result={"queued": queued, **outcome})


SPEC = agents.register(AgentSpec(
    key="alert_courier", name="Alert courier",
    description="Sends an alert when something crosses a threshold: an off-track rule, a payment due or "
                "overdue, an exam or an application deadline coming up, a task due in a few days, the AI "
                "spend cap. Once each. The reminder days are in the notification settings.",
    schedule=Schedule(interval_minutes=15), events=EVENTS,
    permissions=Permissions(tools=[], tables_write=[], may_notify=True, max_llm_calls=0),
    run=run, phase="6",
))
