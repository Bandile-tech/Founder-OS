"""
When is an agent due?

All schedules are in the user's zone (``FOUNDER_OS_TZ``, default Africa/Lusaka). Stored
timestamps are naive UTC, as everywhere else; conversion happens only here and in
notification text.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agents.base import Schedule

_WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

from user_clock import DEFAULT_TZ, tz, to_local, to_utc   # noqa: F401 — one clock for the whole app


def parse_time(text: str | None):
    """'06:00' → (6, 0); None when unreadable."""
    try:
        hour, minute = str(text).strip().split(":")[:2]
        hour, minute = int(hour), int(minute)
    except (ValueError, AttributeError):
        return None
    return (hour, minute) if 0 <= hour <= 23 and 0 <= minute <= 59 else None


def effective(spec, row) -> Schedule:
    """The spec's schedule with the row's overrides applied. An override of either kind
    replaces the spec's kind (an interval override turns a daily agent into an interval one)."""
    base = spec.schedule
    interval, at_time, weekdays = base.interval_minutes, base.at_time, base.weekdays
    if row is not None:
        if row.at_time:
            at_time, interval = row.at_time, None
        if row.interval_minutes:
            interval = int(row.interval_minutes)
            if not row.at_time:
                at_time = None
        if row.weekdays_json:
            try:
                parsed = json.loads(row.weekdays_json)
                weekdays = [int(d) for d in parsed] if isinstance(parsed, list) and parsed else None
            except (ValueError, TypeError):
                pass
    return Schedule(interval_minutes=interval, at_time=at_time, weekdays=weekdays,
                    min_gap_minutes=base.min_gap_minutes)


def due(spec, row, now_local: datetime) -> bool:
    """Is the agent due by its schedule at ``now_local`` (an aware local datetime)?

    at_time: due once the local clock has passed it, if it has not run yet today and today
    is one of its weekdays. Otherwise interval: due when it has never run or the interval
    has elapsed. An agent with neither runs only on events."""
    s = effective(spec, row)
    last = row.last_run_at if row is not None else None
    at = parse_time(s.at_time)
    if at is not None:
        if s.weekdays is not None and now_local.weekday() not in s.weekdays:
            return False
        if (now_local.hour, now_local.minute) < at:
            return False
        return last is None or to_local(last).date() < now_local.date()
    if s.interval_minutes:
        if last is None:
            return True
        now_utc = to_utc(now_local)
        # Half a minute of slack: a timer that fires a few milliseconds early must not push
        # an every-tick agent to the tick after.
        return now_utc - last >= timedelta(minutes=int(s.interval_minutes)) - timedelta(seconds=30)
    return False


def inside_min_gap(spec, row, now_utc: datetime) -> bool:
    """True when the agent ran too recently to run again (a doubled tick)."""
    if row is None or row.last_run_at is None:
        return False
    return now_utc - row.last_run_at < timedelta(minutes=spec.schedule.min_gap_minutes)


def describe(s: Schedule) -> str:
    """'every 60 min', 'daily at 06:00', 'Sun at 07:00', or 'on events only'."""
    if parse_time(s.at_time) is not None:
        if s.weekdays:
            days = ", ".join(_WEEKDAY_NAMES[d] for d in sorted(set(s.weekdays)) if 0 <= d <= 6)
            return f"{days} at {s.at_time}"
        return f"daily at {s.at_time}"
    if s.interval_minutes:
        return f"every {int(s.interval_minutes)} min"
    return "on events only"


def next_due(spec, row, now_local: datetime) -> datetime | None:
    """A naive-UTC estimate of the next scheduled run (None for event-only agents)."""
    s = effective(spec, row)
    last = row.last_run_at if row is not None else None
    at = parse_time(s.at_time)
    if at is not None:
        for offset in range(0, 8):
            day = now_local + timedelta(days=offset)
            candidate = day.replace(hour=at[0], minute=at[1], second=0, microsecond=0)
            if s.weekdays is not None and candidate.weekday() not in s.weekdays:
                continue
            ran_that_day = last is not None and to_local(last).date() >= candidate.date()
            if ran_that_day:
                continue
            return to_utc(max(candidate, now_local))
        return None
    if s.interval_minutes:
        now_utc = to_utc(now_local)
        if last is None:
            return now_utc
        return max(now_utc, last + timedelta(minutes=int(s.interval_minutes)))
    return None
