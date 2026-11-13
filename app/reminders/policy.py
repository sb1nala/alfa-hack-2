"""Calendar delivery windows. All inputs and results are aware instants."""

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from app.reminders.schedule import aware_utc


def day_bounds(now: datetime, zone: str) -> tuple[datetime, datetime]:
    local = aware_utc(now).astimezone(ZoneInfo(zone))
    start = datetime.combine(local.date(), time(), local.tzinfo)
    end = datetime.combine(local.date() + timedelta(days=1), time(), local.tzinfo)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def allowed_at(now: datetime, zone: str, *, tomorrow: bool = False) -> datetime:
    instant = aware_utc(now)
    local = instant.astimezone(ZoneInfo(zone))
    if not tomorrow and 9 <= local.hour < 21:
        return instant
    date = local.date() + timedelta(days=int(tomorrow or local.hour >= 21))
    return datetime.combine(date, time(9), local.tzinfo).astimezone(timezone.utc)
