from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import pytest
from app.reminders.policy import allowed_at, day_bounds
from app.reminders import controls
from app.reminders.schedule import canonicalize, _parse, _events
from app.models import Reminder


@pytest.mark.parametrize("zone", ["Europe/Moscow", "Asia/Vladivostok", "Europe/Berlin"])
@pytest.mark.parametrize(
    "hour,minute,second,shift",
    [(8, 59, 59, True), (9, 0, 0, False), (20, 59, 0, False), (21, 0, 0, True)],
)
def test_boundaries(zone, hour, minute, second, shift):
    now = datetime(2026, 11, 13, hour, minute, second, tzinfo=ZoneInfo(zone))
    result = allowed_at(now, zone)
    assert (result > now) == shift
    if shift:
        assert result.astimezone(ZoneInfo(zone)).hour == 9
    assert result.tzinfo is timezone.utc


@pytest.mark.parametrize("month,day,hours", [(3, 29, 23), (10, 25, 25)])
def test_calendar_dst(month, day, hours):
    now = datetime(2026, month, day, 12, tzinfo=ZoneInfo("Europe/Berlin"))
    start, end = day_bounds(now, "Europe/Berlin")
    assert end - start == timedelta(hours=hours)
    assert (
        allowed_at(now, "Europe/Berlin", tomorrow=True)
        .astimezone(ZoneInfo("Europe/Berlin"))
        .hour
        == 9
    )


def test_aware_required():
    with pytest.raises(ValueError):
        allowed_at(datetime(2026, 1, 1), "Europe/Moscow")


def test_less_finite_anchor():
    start = datetime(2026, 1, 5, 7, tzinfo=timezone.utc)
    original = canonicalize("FREQ=DAILY;COUNT=8", start, "Europe/Moscow")
    r = Reminder(1, 1, "custom", "test", start, original, False)
    changed, run = controls.less_often(r, start)
    assert changed.splitlines()[0] == original.splitlines()[0]
    _, anchor, rule = _parse(changed)
    events = list(_events(anchor, rule))
    assert len(events) == 4 and events[-1] == start + timedelta(days=6)
    assert run == start + timedelta(days=2)
    with pytest.raises(ValueError):
        controls.resume(r, start + timedelta(days=9))


@pytest.mark.parametrize(
    "kind,rule",
    [("tax", "FREQ=DAILY"), ("custom", None), ("custom", "FREQ=DAILY;INTERVAL=100")],
)
def test_forbidden_less(kind, rule):
    now = datetime(2026, 1, 5, 7, tzinfo=timezone.utc)
    r = Reminder(1, 1, kind, "test", now, canonicalize(rule, now) if rule else None)
    with pytest.raises(ValueError):
        controls.less_often(r, now)


def test_until_frequency_and_less_remain_finite():
    now = datetime(2026, 1, 5, 7, tzinfo=timezone.utc)
    rule = canonicalize("FREQ=DAILY;UNTIL=20260120T070000Z", now, "Europe/Moscow")
    r = Reminder(1, 1, "custom", "test", now, rule)
    for action in ("weekly", "monthly"):
        try:
            changed, run = controls.frequency_change(r, now, action, "Asia/Vladivostok")
        except ValueError:
            assert action == "monthly"
        else:
            assert "UNTIL=20260120T070000Z" in changed
            assert changed.splitlines()[0] == rule.splitlines()[0]
    changed, run = controls.less_often(r, now)
    assert "UNTIL=20260120T070000Z" in changed and run > now


def test_less_does_not_round_to_nonmultiple_interval():
    now = datetime(2026, 1, 5, 7, tzinfo=timezone.utc)
    r = Reminder(
        1, 1, "custom", "test", now, canonicalize("FREQ=DAILY;INTERVAL=51", now)
    )
    with pytest.raises(ValueError, match="100"):
        controls.less_often(r, now)
