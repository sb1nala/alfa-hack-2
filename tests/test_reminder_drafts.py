"""Confirmation durability and races on isolated real PostgreSQL."""

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
import pytest

from app.reminders import service
from app.storage import profiles, reminder_drafts as drafts, reminders

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
RUN = NOW + timedelta(hours=1)
USER = 8_000_000_001


async def new():
    return await drafts.create(USER, 'Проверить вымышленный план', RUN, 'Europe/Moscow', NOW)


async def test_draft_not_active_confirm_and_reopen(storage_db):
    pool, reopen = storage_db
    key = await new()
    assert await service.list_for(USER) == []
    assert await service.due(RUN) == []
    await pool.close()
    pool = await reopen()
    assert await drafts.decide(key, USER, True, NOW) == 'confirmed'
    saved, = await service.list_for(USER)
    assert saved.text == 'Проверить вымышленный план' and saved.next_run_at == RUN
    await pool.close()
    await reopen()
    assert await drafts.decide(key, USER, True, NOW) == 'confirmed'
    assert await drafts.decide(key, USER, False, NOW) == 'confirmed'
    assert await service.list_for(USER) == [saved]


async def test_owner_missing_and_cancel(storage_db):
    pool, _ = storage_db
    key = await new()
    assert await drafts.decide(key, USER + 1, True, NOW) == 'forbidden'
    assert await drafts.decide(key, USER + 1, False, NOW) == 'forbidden'
    assert await drafts.decide(uuid4(), USER, True, NOW) == 'missing'
    assert await drafts.decide(key, USER, False, NOW) == 'cancelled'
    assert await drafts.decide(key, USER, True, NOW) == 'cancelled'
    assert await pool.fetchval('SELECT count(*) FROM reminders') == 0


@pytest.mark.parametrize('elapsed,run', [(timedelta(minutes=15), RUN),
                                       (timedelta(minutes=1), NOW + timedelta(minutes=1))])
async def test_expiry_and_due_time_rechecked(storage_db, elapsed, run):
    key = await drafts.create(USER, 'План', run, 'UTC', NOW)
    assert await drafts.decide(key, USER, True, NOW + elapsed) == 'expired'
    assert await service.list_for(USER) == []


async def test_two_concurrent_confirmations(storage_db):
    pool, _ = storage_db
    key = await new()
    assert await asyncio.gather(*(drafts.decide(key, USER, True, NOW) for _ in range(12))) == [
        'confirmed'] * 12
    assert await pool.fetchval('SELECT count(*) FROM reminders') == 1


async def test_cancel_confirm_only_one_wins(storage_db):
    pool, _ = storage_db
    key = await new()
    results = await asyncio.gather(drafts.decide(key, USER, True, NOW),
                                   drafts.decide(key, USER, False, NOW))
    assert results[0] == results[1]
    assert await pool.fetchval('SELECT count(*) FROM reminders') == (results[0] == 'confirmed')


async def test_confirm_rollback_if_draft_update_fails(storage_db):
    pool, _ = storage_db
    key = await new()
    await pool.execute("ALTER TABLE reminder_drafts ADD CONSTRAINT test_fail CHECK (state <> 'confirmed')")
    with pytest.raises(asyncpg.CheckViolationError):
        await drafts.decide(key, USER, True, NOW)
    assert await pool.fetchval('SELECT count(*) FROM reminders') == 0
    assert await pool.fetchval('SELECT state FROM reminder_drafts WHERE id=$1', key) == 'pending'


async def test_profile_change_keeps_confirmed_instant(storage_db):
    key = await new()
    await profiles.update(USER, {'timezone': 'Asia/Vladivostok'})
    assert await drafts.decide(key, USER, True, NOW) == 'confirmed'
    assert (await service.list_for(USER))[0].next_run_at == RUN


async def test_deleted_confirmed_reminder_old_button(storage_db):
    key = await new()
    await drafts.decide(key, USER, True, NOW)
    reminder, = await service.list_for(USER)
    await reminders.delete(reminder.id)
    assert await drafts.decide(key, USER, True, NOW) == 'deleted'
    assert await service.list_for(USER) == []  # Never recreate from an old confirmed button.
