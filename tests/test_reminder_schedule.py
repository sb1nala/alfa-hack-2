"""Synthetic recurrence examples; no database or clock required."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.models import Reminder
from app.reminders import service
from app.reminders.schedule import aware_utc, canonicalize, next_occurrence
from app.storage import db


def dt(value):
    return datetime.fromisoformat(value)


START = dt('2026-01-05T07:00:00+00:00')  # Monday


@pytest.mark.parametrize('zone,hour', [('Europe/Moscow', '100000'), ('Asia/Vladivostok', '170000')])
def test_zone_and_roundtrip(zone, hour):
    rule = canonicalize('FREQ=WEEKLY;BYDAY=MO;COUNT=2', START, zone)
    assert rule.startswith(f'DTSTART;TZID={zone}:20260105T{hour}\n')
    assert canonicalize(rule, START, 'UTC') == rule
    second = START + timedelta(days=7)
    assert next_occurrence(rule, START) == second
    assert canonicalize(rule, second, 'UTC') == rule
    assert next_occurrence(rule, second) is None


def test_offsets_and_strict_comparison():
    other = START.astimezone(timezone(timedelta(hours=3)))
    assert aware_utc(other) == START
    rule = canonicalize('RRULE:FREQ=DAILY', other)
    assert canonicalize('FREQ=DAILY', START) == rule
    assert next_occurrence(rule, START - timedelta(microseconds=1)) == START
    assert next_occurrence(rule, START) == START + timedelta(days=1)
    assert next_occurrence(rule, START + timedelta(microseconds=1)) == START + timedelta(days=1)


@pytest.mark.parametrize('rule,start,expected', [
    ('FREQ=DAILY;INTERVAL=2', '2026-12-31T07:00:00Z', '2027-01-02T07:00:00Z'),
    ('FREQ=WEEKLY;INTERVAL=2;BYDAY=MO', '2026-01-05T07:00:00Z', '2026-01-19T07:00:00Z'),
    ('FREQ=MONTHLY;BYMONTHDAY=31', '2026-01-31T07:00:00Z', '2026-03-31T07:00:00Z'),
    ('FREQ=YEARLY;BYMONTH=1;BYMONTHDAY=5', '2026-01-05T07:00:00Z', '2027-01-05T07:00:00Z'),
    ('FREQ=DAILY;BYDAY=MO,TU', '2026-01-05T07:00:00Z', '2026-01-06T07:00:00Z'),
])
def test_calendar(rule, start, expected):
    assert next_occurrence(canonicalize(rule, dt(start)), dt(start)) == dt(expected)


def test_until_and_utc_canonical():
    text = 'DTSTART:20260105T070000Z\nRRULE:FREQ=DAILY;UNTIL=20260106T070000Z'
    rule = canonicalize(text, START, 'Asia/Vladivostok')
    assert rule.startswith('DTSTART:20260105T070000Z\n')
    end = START + timedelta(days=1)
    assert next_occurrence(rule, START) == end
    assert next_occurrence(rule, end) is None


@pytest.mark.parametrize('rule', [
    '', 'FREQ=HOURLY', 'FREQ=DAILY;INTERVAL=0', 'FREQ=DAILY;COUNT=-1',
    'FREQ=DAILY;COUNT=10001', 'FREQ=DAILY;INTERVAL=101',
    'FREQ=DAILY;COUNT=2;UNTIL=20260106T070000Z',
    'FREQ=DAILY;UNTIL=20260106T070000', 'FREQ=DAILY;UNTIL=20260106',
    'FREQ=DAILY;BYHOUR=10', 'FREQ=DAILY;BYDAY=1MO',
    'FREQ=MONTHLY;BYMONTHDAY=0', 'FREQ=YEARLY;BYMONTH=13',
    'FREQ=DAILY;FREQ=WEEKLY', 'FREQ=WEEKLY;BYDAY=TU',
    'FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=30',
    'RRULE:FREQ=DAILY\nRRULE:FREQ=WEEKLY',
    'DTSTART:20260105T070000Z\nRRULE:FREQ=DAILY\nRDATE:20260106T070000Z',
    'DTSTART:20260105T070000\nRRULE:FREQ=DAILY',
    'DTSTART;TZID=Bad/Zone:20260105T070000\nRRULE:FREQ=DAILY',
    'DTSTART:20260105T070000Z\nRRULE:FREQ=DAILY\nEXDATE:20260106T070000Z',
])
def test_invalid_rules(rule):
    with pytest.raises(ValueError):
        canonicalize(rule, START)


def test_invalid_times_and_membership():
    with pytest.raises(ValueError):
        aware_utc(datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        canonicalize('FREQ=DAILY', START.replace(microsecond=1))
    rule = canonicalize('FREQ=DAILY;COUNT=2', START)
    for invalid in (START + timedelta(hours=1), START + timedelta(days=2)):
        with pytest.raises(ValueError):
            canonicalize(rule, invalid)
    with pytest.raises(ValueError):
        next_occurrence(rule, datetime(2026, 1, 1))


@pytest.mark.parametrize('local', ['2026-03-29T02:30:00', '2026-10-25T02:30:00'])
def test_dst_anchor_rejected(local):
    text = f'DTSTART;TZID=Europe/Berlin:{datetime.fromisoformat(local):%Y%m%dT%H%M%S}\nRRULE:FREQ=DAILY'
    with pytest.raises(ValueError, match='DST'):
        canonicalize(text, START)


@pytest.mark.parametrize('start', ['2026-03-28T02:30:00+01:00', '2026-10-24T02:30:00+02:00'])
def test_dst_future_event_rejected(start):
    first = dt(start)
    rule = canonicalize('FREQ=DAILY', first, 'Europe/Berlin')
    with pytest.raises(ValueError, match='DST'):
        next_occurrence(rule, first)


def test_dst_safe_hour_keeps_wall_clock():
    first = dt('2026-03-28T10:00:00+01:00')
    rule = canonicalize('FREQ=DAILY', first, 'Europe/Berlin')
    following = next_occurrence(rule, first)
    assert following == dt('2026-03-29T08:00:00Z')
    assert following.astimezone(ZoneInfo('Europe/Berlin')).hour == 10


def test_search_budget():
    rule = canonicalize('FREQ=DAILY', START)
    with pytest.raises(ValueError, match='10000'):
        next_occurrence(rule, START + timedelta(days=10001))
    rule = canonicalize('FREQ=YEARLY;INTERVAL=100', START)
    with pytest.raises(ValueError, match='400 years'):
        next_occurrence(rule, START.replace(year=2500))


@pytest.mark.parametrize('field,value,error', [
    ('id', 1, ValueError), ('id', False, TypeError), ('user_id', True, TypeError),
    ('user_id', 2**63, ValueError), ('kind', 'unknown', ValueError),
    ('text', '  ', ValueError), ('text', None, TypeError), ('active', 1, TypeError),
    ('next_run_at', datetime(2026, 1, 1), ValueError), ('rrule', 7, TypeError),
])
async def test_create_validation_without_database(monkeypatch, field, value, error):
    def fail():
        pytest.fail('validation accessed database')
    monkeypatch.setattr(db, 'pool', fail)
    reminder = Reminder(0, 100, 'custom', 'Вымышленный план', START, None)
    setattr(reminder, field, value)
    with pytest.raises(error):
        await service.create(reminder)


async def test_other_validation_without_database(monkeypatch):
    monkeypatch.setattr(db, 'pool', lambda: pytest.fail('database accessed'))
    for operation, args, error in [
        (service.list_for, (True,), TypeError), (service.pause, (1, 0), TypeError),
        (service.delete, (0,), ValueError), (service.pause, (-1, True), ValueError),
        (service.set_schedule, (1, None, datetime(2026, 1, 1)), ValueError),
        (service.due, (datetime(2026, 1, 1),), ValueError),
    ]:
        with pytest.raises(error):
            await operation(*args)


def test_impossible_dense_rule_rejected_before_iteration(monkeypatch):
    from app.reminders import schedule
    monkeypatch.setattr(schedule.rrule, 'rrulestr', lambda *a, **k: pytest.fail('impossible rule iterated'))
    with pytest.raises(ValueError, match='BYMONTHDAY'):
        canonicalize('FREQ=DAILY;BYMONTH=2;BYMONTHDAY=30', dt('2026-02-01T10:00:00Z'))


def test_negative_monthday_and_leap_year():
    start = dt('2028-02-29T10:00:00Z')
    rule = canonicalize('FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=29;COUNT=2', start)
    assert next_occurrence(rule, start) == dt('2032-02-29T10:00:00Z')
    start = dt('2026-01-31T10:00:00Z')
    rule = canonicalize('FREQ=MONTHLY;BYMONTHDAY=-1', start)
    assert next_occurrence(rule, start) == dt('2026-02-28T10:00:00Z')
