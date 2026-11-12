"""
Shared helpers for the business modules (leads, CRM, portfolio, brand).

  fuzzy_pick(rows, key_fn, fragment)  — one clear match or a short list of candidates
  money(amount, currency)             — {"amount", "currency", "usd"}
  sum_usd(rows)                       — total in dollars across currencies
  days_since(dt)                      — whole days since a date or datetime
"""

import difflib
import re
from datetime import date, datetime
import user_clock


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 1}


def _score(fragment: str, label: str) -> float:
    fw, lw = _words(fragment), _words(label)
    if not fw or not lw:
        return 0.0
    overlap = len(fw & lw) / min(len(fw), len(lw))
    ratio = difflib.SequenceMatcher(None, " ".join(sorted(fw)), " ".join(sorted(lw))).ratio()
    fl, ll = fragment.lower().strip(), label.lower().strip()
    contain = 1.0 if fl and (fl in ll or ll in fl) else 0.0
    return round(max(overlap * 0.9, ratio, contain), 3)


def fuzzy_pick(rows, key_fn, fragment, *, min_score=0.5, accept=0.75, gap=0.2):
    """Match ``fragment`` against ``key_fn(row)`` for every row.

    Returns ``(picked, candidates)``. ``picked`` is the row when the best score is at least
    ``accept`` and at least ``gap`` ahead of the runner-up, otherwise None. ``candidates``
    is every row scoring ``min_score`` or more as ``(score, row)``, best first. ``key_fn``
    may return one label or a list of labels (the best one counts)."""
    scored = []
    for row in rows:
        labels = key_fn(row)
        if isinstance(labels, str) or labels is None:
            labels = [labels]
        score = max((_score(fragment or "", label or "") for label in labels), default=0.0)
        if score >= min_score:
            scored.append((score, row))
    scored.sort(key=lambda pair: (-pair[0], getattr(pair[1], "id", 0) or 0))
    if not scored:
        return None, []
    top = scored[0][0]
    second = scored[1][0] if len(scored) > 1 else 0.0
    if top >= accept and (top - second) >= gap:
        return scored[0][1], scored
    return None, scored


def money(amount, currency) -> dict:
    import main
    cur = main._normalise_currency(currency)
    value = float(amount or 0)
    return {"amount": round(value, 2), "currency": cur, "usd": round(main._to_usd(value, cur), 2)}


def sum_usd(rows, amount_attr="amount", currency_attr="currency") -> float:
    import main
    total = 0.0
    for row in rows:
        amount = getattr(row, amount_attr, None)
        if amount is None:
            continue
        total += main._to_usd(float(amount), getattr(row, currency_attr, None))
    return round(total, 2)


def days_since(dt) -> int | None:
    """Whole days from ``dt`` (a date or a naive UTC datetime) to now. None for no date."""
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return (datetime.utcnow() - dt).days
    if isinstance(dt, date):
        return (user_clock.today() - dt).days
    return None


_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def parse_day(text, today: date | None = None, *, past: bool = False) -> date | None:
    """A spoken day → a date: today, tomorrow, yesterday, a weekday name, "in 3 days",
    "end of month", "end of week", or anything ``syllabus.parse_date_text`` reads
    ("12 May", "2026-05-12").

    ``past=True`` is for things that already happened (a call, a payment): a weekday means
    its most recent occurrence and a day-month with no year is never in the future."""
    import calendar
    from datetime import timedelta

    import syllabus

    today = today or user_clock.today()
    t = " ".join(str(text or "").lower().replace(",", " ").split())
    if not t:
        return None
    if t in ("today", "now", "tonight", "this morning", "this afternoon", "this evening"):
        return today
    if t == "tomorrow":
        return today + timedelta(days=1)
    if t == "yesterday":
        return today - timedelta(days=1)
    m = re.fullmatch(r"in (\d+) (day|days|week|weeks)", t)
    if m:
        n = int(m.group(1)) * (7 if m.group(2).startswith("week") else 1)
        return today + timedelta(days=n)
    m = re.fullmatch(r"(\d+) (day|days|week|weeks) ago", t)
    if m:
        n = int(m.group(1)) * (7 if m.group(2).startswith("week") else 1)
        return today - timedelta(days=n)
    if t in ("end of month", "end of the month", "month end"):
        return today.replace(day=calendar.monthrange(today.year, today.month)[1])
    if t in ("end of week", "end of the week"):
        return today + timedelta(days=(4 - today.weekday()) % 7)
    words = t.split()
    if words[-1] in _WEEKDAYS and words[:-1] in ([], ["next"], ["this"], ["on"], ["last"]):
        target = _WEEKDAYS.index(words[-1])
        if words[:-1] == ["last"]:
            return today - timedelta(days=((today.weekday() - target) % 7) or 7)
        if past:
            return today - timedelta(days=(today.weekday() - target) % 7)
        return today + timedelta(days=((target - today.weekday()) % 7) or 7)
    parsed = syllabus.parse_date_text(str(text).strip(), today)
    if parsed is not None and past and parsed > today and not re.search(r"\d{4}", t):
        parsed = parsed.replace(year=parsed.year - 1)
    return parsed


_UNITS = {"minute": 1, "minutes": 1, "min": 1, "mins": 1, "hour": 60, "hours": 60, "hr": 60, "hrs": 60}
_CLOCK = re.compile(r"(?:^|\s)(at\s+)?(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)?$")
DEFAULT_REMINDER_HOUR = 9


def parse_when(text, now_local, *, default_hour: int | None = DEFAULT_REMINDER_HOUR):
    """A spoken moment → a naive UTC datetime, or None when it cannot be read or has passed.
    With ``default_hour=None`` a day with no clock time is refused (None) instead of
    meaning 09:00.

    Reads "17:00", "5pm", "at 5", "in 2 hours", "in 30 minutes", "tomorrow 17:00",
    "friday at 9", "12 May 14:30", and anything ``parse_day`` reads on its own (a day with
    no time means 09:00). A bare hour from 1 to 12 with no am/pm is the next time the clock
    shows it today; on another day 1–6 means the afternoon. ``now_local`` is an aware
    datetime in the user's zone."""
    from datetime import datetime, time, timedelta

    from agents import schedule

    t = " ".join(str(text or "").lower().replace(",", " ").split())
    if not t:
        return None
    m = re.fullmatch(r"in (\d+|an|a|half an) (minute|minutes|min|mins|hour|hours|hr|hrs)", t)
    if m:
        amount = {"an": 1.0, "a": 1.0, "half an": 0.5}.get(m.group(1)) or float(m.group(1))
        return schedule.to_utc(now_local + timedelta(minutes=amount * _UNITS[m.group(2)]))

    today = now_local.date()
    day_text, clock = t, None
    m = _CLOCK.search(t)
    if m:
        hour, minute, meridiem = int(m.group(2)), int(m.group(3) or 0), m.group(4)
        explicit = bool(m.group(1) or m.group(3) or meridiem)
        rest = t[:m.start()].strip()
        if rest.endswith(" at") or rest == "at":
            rest, explicit = rest[:-2].strip(), True
        # "May 12" ends in a number too: read a bare number as a time only when what is
        # left of it is a day on its own (or nothing).
        if hour <= 23 and minute <= 59 and (explicit or not rest or parse_day(rest, today) is not None):
            if not (meridiem and not 1 <= hour <= 12):
                day_text, clock = rest, (hour, minute, meridiem, m.group(2).startswith("0"))

    day = parse_day(day_text, today) if day_text else today
    if day is None:
        return None

    def at(d, hour, minute):
        return datetime.combine(d, time(hour, minute), tzinfo=now_local.tzinfo)

    if clock is None:
        if default_hour is None:
            return None
        moment = at(day, default_hour, 0)
    else:
        hour, minute, meridiem, zero_padded = clock
        if meridiem:
            hour = hour % 12 + (12 if meridiem == "pm" else 0)
            moment = at(day, hour, minute)
        elif hour == 0 or hour >= 13 or zero_padded:
            moment = at(day, hour, minute)
        elif day == today and not day_text:
            # "at 5" said today: the next time the clock reads 5.
            options = [at(day, hour % 12, minute), at(day, hour % 12 + 12, minute)]
            later = [o for o in options if o > now_local]
            moment = min(later) if later else options[0] + timedelta(days=1)
        else:
            moment = at(day, hour + 12 if hour <= 6 else hour, minute)
        if not day_text and moment <= now_local:
            moment += timedelta(days=1)             # a time alone that has passed means tomorrow
    return schedule.to_utc(moment) if moment > now_local else None
