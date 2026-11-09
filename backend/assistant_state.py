"""
What the assistant knows, read from the current sources only.

Every number the assistant quotes about Bandile's state comes through this module:

* academics       — ``syllabus_points`` (four-state tracker), per subject and paper
* daily routine   — ``non_negotiables`` (the active list and its labels). A day's ticks are
                    stored in the ``habits`` table, keyed by the non-negotiable's key; this
                    module is the only assistant-side reader of that table, and it always
                    goes through the active non-negotiables, never the raw rows
* targets         — ``annual_targets``
* revenue         — ``revenue``, reported in Kwacha (USD rows converted at ``ZMW_PER_USD``)

The legacy tables (``subjects``/``topics``/``subtopics`` mastery, ``roadmap_tasks``,
``kpi_snapshots``, the $10K scoreboard, trading) are never read here. Everything returned
is plain language: labels instead of keys, words instead of booleans and status codes.
"""

import os
import re
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

import change_log
import models
import syllabus

DEFAULT_ZMW_PER_USD = 27.0

STATUS_WORDS = {
    "not_started": "not started",
    "learned": "learned but weak",
    "revised": "revised but not 100%",
    "bulletproof": "bulletproof",
}
TARGET_STATUS_WORDS = {
    "ahead": "ahead of pace",
    "on_track": "on track",
    "behind": "behind pace",
    "critical": "critically behind",
    "done": "done",
    "pending": "not done yet",
}


# ── Formatting ───────────────────────────────────────────────

def zmw_per_usd() -> float:
    try:
        v = float(os.getenv("ZMW_PER_USD", DEFAULT_ZMW_PER_USD))
        return v if v > 0 else DEFAULT_ZMW_PER_USD
    except ValueError:
        return DEFAULT_ZMW_PER_USD


def to_zmw(amount: float, currency: str | None) -> float:
    cur = (currency or "USD").strip().upper()
    if cur in ("ZMW", "K", "KWACHA", "ZK"):
        return float(amount)
    return float(amount) * zmw_per_usd()


def fmt_zmw(value: float) -> str:
    value = round(float(value or 0), 2)
    return f"K{value:,.0f}" if value == int(value) else f"K{value:,.2f}"


def fmt_amount(amount: float, currency: str | None) -> str:
    cur = (currency or "USD").strip().upper()
    if cur in ("ZMW", "K", "KWACHA", "ZK"):
        return fmt_zmw(amount)
    amount = float(amount)
    return f"${amount:,.0f}" if amount == int(amount) else f"${amount:,.2f}"


def fmt_number(v) -> str:
    if v is None:
        return "?"
    v = float(v)
    return f"{v:,.0f}" if v == int(v) else f"{v:,.2f}".rstrip("0").rstrip(".")


def plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def day_label(d: date, today: date | None = None) -> str:
    today = today or date.today()
    if d == today:
        return "today"
    if d == today - timedelta(days=1):
        return "yesterday"
    if d == today + timedelta(days=1):
        return "tomorrow"
    return f"{d.day} {d.strftime('%b')}"


# ── Daily routine (non-negotiables) ──────────────────────────

def active_non_negotiables(db: Session) -> list[models.NonNegotiable]:
    return (db.query(models.NonNegotiable)
            .filter(models.NonNegotiable.is_active == True)  # noqa: E712
            .order_by(models.NonNegotiable.sort_order, models.NonNegotiable.id)
            .all())


def _ticks(db: Session, keys: list[str], start: date, end: date) -> list[models.Habit]:
    if not keys:
        return []
    return (db.query(models.Habit)
            .filter(models.Habit.key.in_(keys), models.Habit.date >= start, models.Habit.date <= end)
            .all())


def routine_day(db: Session, day: date | None = None) -> dict:
    """The active non-negotiables for one day: label, done, and the tick row id if any."""
    day = day or date.today()
    active = active_non_negotiables(db)
    ticks = {h.key: h for h in _ticks(db, [n.key for n in active], day, day)}
    items = []
    for n in active:
        h = ticks.get(n.key)
        items.append({"key": n.key, "label": n.label, "done": bool(h and h.done), "habit_id": h.id if h else None})
    done = sum(1 for i in items if i["done"])
    return {"date": day, "items": items, "done": done, "total": len(items),
            "pct": round(100 * done / len(items)) if items else 0}


def routine_completion(db: Session, start: date, end: date, *, only_days_with_data: bool = False) -> dict:
    """Completion of the active non-negotiables over [start, end]. A day with no tick counts
    as not done, unless ``only_days_with_data`` (the off-track rule's no-signal guard)."""
    active = active_non_negotiables(db)
    keys = [n.key for n in active]
    rows = _ticks(db, keys, start, end)
    done = sum(1 for h in rows if h.done)
    if only_days_with_data:
        days = len({h.date for h in rows})
    else:
        days = (end - start).days + 1
    possible = len(keys) * days
    return {"done": done, "possible": possible, "days": days, "has_data": bool(rows),
            "pct": round(100 * done / possible) if possible else None}


def routine_text(db: Session, day: date | None = None) -> str:
    r = routine_day(db, day)
    if not r["total"]:
        return "No daily non-negotiables are set."
    done = [i["label"] for i in r["items"] if i["done"]]
    open_ = [i["label"] for i in r["items"] if not i["done"]]
    line = f"{r['done']} of {r['total']} done {day_label(r['date'])}."
    if done:
        line += " Done: " + "; ".join(done) + "."
    if open_:
        line += " Still open: " + "; ".join(open_) + "."
    return line


def _match_non_negotiable(active: list[models.NonNegotiable], text: str):
    """Exact key → label substring (either way) → shared word. Returns (nn, strategy)."""
    t = (text or "").strip().lower()
    if not t:
        return None, None
    for n in active:
        if n.key == t or n.key == t.replace(" ", "_"):
            return n, "exact_key"
    for n in active:
        label = n.label.lower()
        if t in label or label in t:
            return n, "label_substring"
    words = {w for w in t.replace("_", " ").split() if len(w) > 2}
    for n in active:
        if words & {w for w in n.label.lower().split() if len(w) > 2}:
            return n, "keyword_overlap"
    for n in active:
        if words & {w for w in n.key.split("_") if len(w) > 2}:
            return n, "keyword_overlap"
    return None, None


def mark_non_negotiable_done(db: Session, text: str, day: date | None = None) -> dict | None:
    """Tick the active non-negotiable that ``text`` names, for ``day``. Creates the day's
    tick row when it does not exist yet. Logged in the change log. Caller commits.
    Returns {key, label, strategy} or None when nothing matches."""
    day = day or date.today()
    active = active_non_negotiables(db)
    if not active and db.query(models.NonNegotiable).count() == 0:
        # Never configured: match against the defaults the app seeds (nothing is written
        # to non_negotiables here, so the parse stays one atomic commit).
        import main
        active = [models.NonNegotiable(key=k, label=lbl) for k, lbl in main.HABIT_DEFAULTS]
    nn, strategy = _match_non_negotiable(active, text)
    if nn is None:
        return None
    row = (db.query(models.Habit)
           .filter(models.Habit.key == nn.key, models.Habit.date == day)
           .first())
    if row is None:
        row = models.Habit(key=nn.key, label=nn.label, done=True, date=day)
        db.add(row)
        db.flush()
        change_log.record_create(db, row, f"Non-negotiable done: {nn.label}")
    elif not row.done:
        before = change_log.snapshot(row)
        row.done = True
        change_log.record_update(db, row, before, f"Non-negotiable done: {nn.label}")
    return {"key": nn.key, "label": nn.label, "strategy": strategy}


# ── Academics (syllabus_points) ──────────────────────────────

def academics(db: Session) -> list[dict]:
    """Per subject: name, overall %, counts per state, and per-paper %."""
    data = syllabus.grouped(db)
    out = []
    for s in data["subjects"]:
        pr = s["progress"]
        out.append({
            "subject": s["subject_name"], "code": s["subject_code"], "pct": pr["pct"],
            "total": pr["total"], "counts": pr["counts"], "exam_date": s["exam_date"],
            "papers": [{"paper": p["paper"], "pct": p["progress"]["pct"], "total": p["progress"]["total"]}
                       for p in s["papers"]],
        })
    return out


def academics_lines(db: Session, today: date | None = None) -> list[str]:
    today = today or date.today()
    lines = []
    for s in academics(db):
        c = s["counts"]
        parts = [f"{c.get('bulletproof', 0)} bulletproof", f"{c.get('revised', 0)} revised",
                 f"{c.get('learned', 0)} learned but weak", f"{c.get('not_started', 0)} not started"]
        line = f"{s['subject']}: syllabus {s['pct']}% ({', '.join(parts)} of {plural(s['total'], 'topic')})"
        papers = [p for p in s["papers"] if p["paper"]]
        if papers:
            line += "; papers " + ", ".join(f"{p['paper']} {p['pct']}%" for p in papers)
        if s["exam_date"]:
            d = date.fromisoformat(s["exam_date"])
            days = (d - today).days
            if days >= 0:
                line += f"; exam {day_label(d, today)} ({plural(days, 'day')} away)"
        lines.append(line + ".")
    return lines


def topic_line(row: dict) -> str:
    """One weakest-topic row (from syllabus.weakest_topics) as a sentence."""
    label = f"{row['subject_name']}{' ' + row['paper'] if row.get('paper') else ''} {row['topic_code']} {row['title']}"
    line = f"{label} — {row['reason']}"
    notes = (row.get("weakness_notes") or "").strip()
    if notes:
        last = notes.splitlines()[-1].lstrip("- ").strip()
        m = re.match(r"^(\d{4}-\d{2}-\d{2}):\s*(.*)$", last)
        if m:
            last = f"{m.group(2)} ({day_label(date.fromisoformat(m.group(1)))})"
        line += f". Latest weakness note: {last}"
    if row.get("past_paper_score") is not None:
        line += f". Past-paper score {fmt_number(row['past_paper_score'])}%"
    return line + "."


# ── Annual targets ───────────────────────────────────────────

def targets(db: Session) -> list[dict]:
    import main
    year_pct = main._year_pct()
    rows = (db.query(models.AnnualTarget)
            .filter(models.AnnualTarget.is_active == True)  # noqa: E712
            .order_by(models.AnnualTarget.sort_order, models.AnnualTarget.id)
            .all())
    return [main._serialize_annual_target(t, year_pct) for t in rows]


def target_line(t: dict) -> str:
    status = TARGET_STATUS_WORDS.get(t["status"], t["status"])
    if t["is_numeric"]:
        unit = f" {t['unit']}" if t.get("unit") else ""
        return (f"{t['name']}: {fmt_number(t['current_value'] or 0)} of {fmt_number(t['target_value'])}{unit} "
                f"({t['progress_pct']}%, {t['expected_pct']}% expected by now) — {status}.")
    shown = f" ({t['display_value']})" if t.get("display_value") else ""
    return f"{t['name']}{shown} — {status}."


def revenue_target(db: Session) -> dict | None:
    """The active numeric annual target that tracks revenue, if one exists."""
    for t in targets(db):
        name = (t["name"] or "").lower()
        unit = (t.get("unit") or "").strip().upper()
        if t["is_numeric"] and ("revenue" in name or unit in ("ZMW", "K", "KWACHA", "USD", "$")):
            return t
    return None


# ── Revenue (ZMW with conversion) ────────────────────────────

def revenue(db: Session, today: date | None = None) -> dict:
    today = today or date.today()
    rows = db.query(models.Revenue).order_by(models.Revenue.date.desc(), models.Revenue.id.desc()).all()
    year_start = date(today.year, 1, 1)
    month_start = today.replace(day=1)
    last_30 = today - timedelta(days=29)

    def total(since):
        return round(sum(to_zmw(r.amount, r.currency) for r in rows if r.date >= since), 2)

    return {
        "year_zmw": total(year_start),
        "month_zmw": total(month_start),
        "last_30_zmw": total(last_30),
        "all_time_zmw": round(sum(to_zmw(r.amount, r.currency) for r in rows), 2),
        "entries": len(rows),
        "latest": rows[0] if rows else None,
        "rate": zmw_per_usd(),
        "has_usd": any((r.currency or "USD").upper() == "USD" for r in rows),
    }


def revenue_text(db: Session, today: date | None = None) -> str:
    today = today or date.today()
    r = revenue(db, today)
    if not r["entries"]:
        return "No revenue logged yet."
    text = (f"Revenue this year {fmt_zmw(r['year_zmw'])}, this month {fmt_zmw(r['month_zmw'])}, "
            f"last 30 days {fmt_zmw(r['last_30_zmw'])}.")
    last = r["latest"]
    text += f" Latest: {fmt_amount(last.amount, last.currency)}"
    if last.source:
        text += f" from {last.source}"
    text += f" on {day_label(last.date, today)}."
    if r["has_usd"]:
        text += f" Dollar amounts converted at K{fmt_number(r['rate'])} per $1."
    return text
