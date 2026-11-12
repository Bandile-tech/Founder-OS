"""
The user's clock.

"Today" is the day in ``FOUNDER_OS_TZ`` (default Africa/Lusaka), never the server's own
day: Render runs on UTC, so between 00:00 and 02:00 in Lusaka the server still thinks it is
yesterday. Every date the app shows, stores or compares as "today" comes from here.

Stored timestamps stay naive UTC (``datetime.utcnow()``), as everywhere else.

  tz()        the zone
  utcnow()    naive UTC now — the one seam a test fixes the time through
  now()       the naive local wall-clock time
  today()     the local date
  to_local()  naive UTC → aware local;  to_utc()  aware local → naive UTC
"""

import os
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TZ = "Africa/Lusaka"


def tz():
    name = os.getenv("FOUNDER_OS_TZ") or DEFAULT_TZ
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        # No tz database on this machine: Lusaka is UTC+2 all year, anything else falls to UTC.
        return timezone(timedelta(hours=2)) if name == DEFAULT_TZ else timezone.utc


def utcnow() -> datetime:
    return datetime.utcnow()


def to_local(naive_utc: datetime) -> datetime:
    return naive_utc.replace(tzinfo=timezone.utc).astimezone(tz())


def to_utc(local: datetime) -> datetime:
    """An aware local datetime → naive UTC."""
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def now() -> datetime:
    """The wall-clock time where the user is, as a naive datetime."""
    return to_local(utcnow()).replace(tzinfo=None)


def today() -> date:
    return now().date()
