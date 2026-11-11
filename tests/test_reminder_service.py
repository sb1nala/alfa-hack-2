"""Public service contract against an isolated real PostgreSQL schema."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta, timezone

import asyncpg
import pytest

from app.models import Reminder
from app.reminders import service
from app.reminders.schedule import next_occurrence
from app.storage import events, profiles, shown_materials
from tests.test_reminder_schedule import START

USER = 6_000_000_001


def draft(**changes):
    return replace(Reminder(0, USER, 'custom', "Вымышленный 'план'; --", START, None), **changes)


async def test_create_one_off_and_recurring(storage_db):
    pool, _ = storage_db
    source = draft(next_run_at=START.astimezone(timezone(timedelta(hours=10))))
    original = deepcopy(source)
    saved = await service.create(source)
    assert source == original and source.id == 0
    assert saved is not source and saved.id > 0 and saved.next_run_at == START
    assert saved.next_run_at.utcoffset() == timedelta(0)
    assert await pool.fetchval('SELECT count(*) FROM users') == 1
    assert await pool.fetchval('SELECT count(*) FROM profiles') == 0
    recurring = await service.create(draft(rrule='FREQ=WEEKLY;BYDAY=MO;COUNT=2'))
    assert recurring.id > saved.id
    assert recurring.rrule.startswith('DTSTART;TZID=Europe/Moscow:20260105T100000\n')
    assert next_occurrence(recurring.rrule, START) == START + timedelta(days=7)
    micro = await service.create(draft(next_run_at=START.replace(microsecond=123456)))
    assert micro.next_run_at.microsecond == 123456


async def test_validation_and_transaction_failures_leave_no_rows(storage_db):
    pool, _ = storage_db
    for source in (draft(rrule='FREQ=WEEKLY;BYDAY=TU'), draft(active=1), draft(id=20)):
        with pytest.raises((ValueError, TypeError)):
            await service.create(source)
    with pytest.raises(asyncpg.PostgresError):
        await service.create(draft(text='bad\x00text'))
    assert await pool.fetchval('SELECT count(*) FROM users') == 0
    assert await pool.fetchval('SELECT count(*) FROM reminders') == 0


async def test_reads_due_order_isolation_and_pause(storage_db):
    pool, _ = storage_db
    assert await service.list_for(USER) == []
    assert await service.due(START) == []
    assert await pool.fetchval('SELECT count(*) FROM users') == 0
    late = await service.create(draft(next_run_at=START + timedelta(days=1)))
    first = await service.create(draft())
    second = await service.create(draft(user_id=USER + 1))
    paused = await service.create(draft(active=False))
    assert await service.list_for(USER) == [first, paused, late]
    assert await service.list_for(USER + 1) == [second]
    assert await service.due(START - timedelta(microseconds=1)) == []
    assert await service.due(START) == [first, second]
    assert await service.due(START) == [first, second]
    assert await service.due(START + timedelta(days=2)) == [first, second, late]
    await service.pause(first.id, False)
    await service.pause(first.id, False)
    assert await service.due(START) == [second]
    await service.pause(first.id, True)
    assert await service.due(START) == [first, second]
    assert (await service.list_for(USER))[0] == first


async def test_schedule_changes_and_profile_independence(storage_db):
    await profiles.update(USER, {'timezone': 'Asia/Vladivostok'})
    saved = await service.create(draft(kind='learning', active=False, rrule='FREQ=DAILY;COUNT=3'))
    assert 'TZID=Asia/Vladivostok:20260105T170000' in saved.rrule
    await profiles.update(USER, {'timezone': 'UTC'})
    assert (await service.list_for(USER))[0] == saved
    second = START + timedelta(days=1)
    changed = await service.set_schedule(saved.id, saved.rrule, second)
    assert changed == replace(saved, next_run_at=second)
    assert next_occurrence(changed.rrule, START + timedelta(days=2)) is None
    with pytest.raises(ValueError):
        await service.set_schedule(saved.id, saved.rrule, START + timedelta(hours=1))
    assert (await service.list_for(USER))[0] == changed
    fresh = await service.set_schedule(saved.id, 'FREQ=DAILY', second)
    assert fresh.rrule.startswith('DTSTART;TZID=UTC:20260106T070000\n')
    once = await service.set_schedule(saved.id, None, START)
    assert once == replace(saved, rrule=None)
    await service.pause(saved.id, True)
    assert await service.due(START) == [replace(once, active=True)]


async def test_pause_preserves_recurrence(storage_db):
    saved = await service.create(draft(rrule='FREQ=DAILY'))
    await service.pause(saved.id, False)
    assert await service.list_for(USER) == [replace(saved, active=False)]
    await service.pause(saved.id, True)
    assert await service.due(START + timedelta(days=10)) == [saved]


async def test_delete_preserves_related_data_and_missing_errors(storage_db):
    pool, _ = storage_db
    await profiles.update(USER, {'industry': 'Вымышленная мастерская'})
    await events.append(USER, 'synthetic')
    await shown_materials.mark_shown(USER, 'fictional:one')
    saved = await service.create(draft())
    other = await service.create(draft(user_id=USER + 1))
    await service.delete(saved.id)
    await service.delete(saved.id)
    assert await service.list_for(USER) == []
    assert await service.list_for(USER + 1) == [other]
    for table, count in [('users', 2), ('profiles', 1), ('events', 1), ('shown_materials', 1)]:
        assert await pool.fetchval(f'SELECT count(*) FROM {table}') == count
    for operation in (lambda: service.pause(saved.id, False),
                      lambda: service.set_schedule(saved.id, None, START)):
        with pytest.raises(service.ReminderNotFoundError):
            await operation()


async def test_persistence_and_count_after_new_pool(storage_db):
    pool, reopen = storage_db
    saved = await service.create(draft(rrule='FREQ=DAILY;COUNT=3'))
    expected = next_occurrence(saved.rrule, START)
    advanced = await service.set_schedule(saved.id, saved.rrule, expected)
    await pool.close()
    await reopen()
    assert await service.list_for(USER) == [advanced]
    assert next_occurrence(advanced.rrule, START) == expected
    end = START + timedelta(days=2)
    assert next_occurrence(advanced.rrule, expected) == end
    assert next_occurrence(advanced.rrule, end) is None
    assert await service.due(expected) == [advanced]
