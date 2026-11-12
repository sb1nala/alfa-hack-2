"""Pure, second-precision recurrence calculations with stable DTSTART/TZID.

Only one DTSTART and RRULE are supported. Search is limited to 10,000 events
and 400 calendar years from DTSTART; exceeding either budget raises ValueError,
never silently reports an exhausted recurrence. DST gaps and folds are rejected
when encountered, rather than shifted or delivered twice.
"""

import re
from calendar import monthrange
from collections.abc import Iterator
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil import rrule, tz

UTC = timezone.utc
MAX_EVENTS = 10_000


def aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("time must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("time must be timezone-aware")
    return value.astimezone(UTC)


def _local_safe(value: datetime) -> None:
    if not tz.datetime_exists(value) or tz.datetime_ambiguous(value):
        raise ValueError("DST gap or ambiguous local time is unsupported")


def _rule_text(value: str) -> str:
    parts = value.removeprefix("RRULE:").split(";")
    fields = {}
    allowed = {"FREQ", "INTERVAL", "BYDAY", "BYMONTHDAY", "BYMONTH", "COUNT", "UNTIL"}
    for part in parts:
        key, sep, val = part.partition("=")
        if not sep or key not in allowed or key in fields or not val:
            raise ValueError("Invalid or unsupported RRULE field")
        fields[key] = val
    if fields.get("FREQ") not in {"DAILY", "WEEKLY", "MONTHLY", "YEARLY"}:
        raise ValueError("Supported FREQ: DAILY, WEEKLY, MONTHLY, YEARLY")
    for key, limit in (("INTERVAL", 100), ("COUNT", MAX_EVENTS)):
        if key in fields and (not re.fullmatch(r"[0-9]{1,5}", fields[key])
                              or not 1 <= int(fields[key]) <= limit):
            raise ValueError(f"{key} must be positive and at most {limit}")
    if "COUNT" in fields and "UNTIL" in fields:
        raise ValueError("COUNT and UNTIL cannot be combined")
    if "UNTIL" in fields and not re.fullmatch(r"\d{8}T\d{6}Z", fields["UNTIL"]):
        raise ValueError("UNTIL must be a UTC timestamp YYYYMMDDTHHMMSSZ")
    for key, low, high in (("BYMONTH", 1, 12), ("BYMONTHDAY", -31, 31)):
        if key in fields:
            values = fields[key].split(",")
            if len(values) > 62 or any(not re.fullmatch(r"-?\d{1,2}", v)
                                      or not low <= int(v) <= high or int(v) == 0
                                      for v in values):
                raise ValueError(f"Invalid {key}")
    if "BYDAY" in fields:
        values = fields["BYDAY"].split(",")
        if len(values) > 7 or any(v not in {"MO", "TU", "WE", "TH", "FR", "SA", "SU"}
                                 for v in values):
            raise ValueError("BYDAY supports plain weekdays only")
    return "RRULE:" + ";".join(f"{k}={fields[k]}" for k in sorted(fields))


def _parse(value: str) -> tuple[str, datetime, rrule.rrule]:
    if not isinstance(value, str):
        raise TypeError("rrule must be a string")
    if len(value) > 2048:
        raise ValueError("Schedule is too long")
    lines = value.splitlines()
    if len(lines) != 2 or not lines[1].startswith("RRULE:"):
        raise ValueError("Expected one DTSTART followed by one RRULE")
    match = re.fullmatch(r"DTSTART(?:;TZID=([A-Za-z0-9_+\-/]+))?:(\d{8}T\d{6})(Z?)", lines[0])
    if not match or bool(match[1]) == bool(match[3]):
        raise ValueError("DTSTART requires either TZID or UTC Z")
    try:
        zone = ZoneInfo(match[1]) if match[1] else UTC
        start = datetime.strptime(match[2], "%Y%m%dT%H%M%S").replace(tzinfo=zone)
        _local_safe(start)
        canonical = lines[0] + "\n" + _rule_text(lines[1])
        fields = dict(part.split("=") for part in canonical.splitlines()[1][6:].split(";"))
        # Reject impossible/mismatching anchors before entering dateutil's iterator:
        # an UNTIL alone does not bound its internal search for an impossible day.
        if "BYMONTH" in fields and start.month not in map(int, fields["BYMONTH"].split(",")):
            raise ValueError("DTSTART does not match BYMONTH")
        if "BYMONTHDAY" in fields:
            days = set(map(int, fields["BYMONTHDAY"].split(",")))
            negative_day = start.day - monthrange(start.year, start.month)[1] - 1
            if start.day not in days and negative_day not in days:
                raise ValueError("DTSTART does not match BYMONTHDAY")
        if "BYDAY" in fields:
            weekday = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")[start.weekday()]
            if weekday not in fields["BYDAY"].split(","):
                raise ValueError("DTSTART does not match BYDAY")
        rule = rrule.rrulestr(canonical, tzids=lambda name: ZoneInfo(name))
    except (ZoneInfoNotFoundError, OverflowError, TypeError, ValueError) as error:
        raise ValueError(f"Invalid schedule: {error}") from error
    return canonical, start, rule


def _events(start: datetime, rule: rrule.rrule) -> Iterator[datetime]:
    # Bound dateutil's internal search too, including impossible combinations.
    horizon_year = min(start.year + 400, 9999)
    horizon = start.replace(year=horizon_year,
                            day=min(start.day, monthrange(horizon_year, start.month)[1]))
    until = rule._until  # dateutil's parsed RFC UNTIL, retained when bounding search
    bounded = rule.replace(count=None, until=min(until, horizon) if until else horizon)
    generated = 0
    for index, event in enumerate(bounded):
        if index >= MAX_EVENTS:
            raise ValueError("Schedule search exceeds 10000 events")
        generated += 1
        _local_safe(event)
        yield event.astimezone(UTC)
        if rule._count is not None and generated >= rule._count:
            return
    if (until is None or until > horizon) and (rule._count is None or generated < rule._count):
        raise ValueError("Schedule search exceeds 400 years")


def canonicalize(value: str, next_run_at: datetime, timezone_name: str = "Europe/Moscow") -> str:
    """Anchor short input; preserve canonical input; require an actual occurrence."""
    instant = aware_utc(next_run_at)
    if instant.microsecond:
        raise ValueError("Recurring times require whole seconds")
    if not isinstance(value, str):
        raise TypeError("rrule must be a string")
    if "\n" not in value and not value.startswith("DTSTART"):
        try:
            local = instant.astimezone(ZoneInfo(timezone_name))
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("Invalid IANA timezone") from error
        _local_safe(local)
        stamp = local.isoformat(timespec="seconds")[:19].replace("-", "").replace(":", "")
        value = f"DTSTART;TZID={timezone_name}:{stamp}\n" + _rule_text(value)
    canonical, start, rule = _parse(value)
    for event in _events(start, rule):
        if event == instant:
            return canonical
        if event > instant:
            break
    raise ValueError("next_run_at is not an occurrence of RRULE")


def next_occurrence(canonical: str, after: datetime) -> datetime | None:
    """Return the first UTC event strictly after after, or None if exhausted."""
    instant = aware_utc(after)
    _, start, rule = _parse(canonical)
    for event in _events(start, rule):
        if event > instant:
            return event
    return None
