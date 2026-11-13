"""Real PostgreSQL, controlled clocks and fake Telegram. No real sends or sleeps."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
import pytest
from aiogram.exceptions import (
    TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter,
)
from aiogram.methods import SendMessage

from app.models import Reminder
from app.reminders import service, worker
from app.storage import reminder_delivery as store
from app.storage import reminders

START = datetime(2026, 1, 5, 7, tzinfo=timezone.utc)
USER = 9_000_000_001
METHOD = SendMessage(chat_id=USER, text='Synthetic')


class Clock:
    def __init__(self, now=START):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def bot():
    return SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=123)))


async def create(*, rule=None, user=USER, when=START, text='Проверить <b>план</b> & идею'):
    return await service.create(Reminder(0, user, 'custom', text, when, rule))


async def event(pool, reminder):
    return await pool.fetchrow('SELECT * FROM reminder_deliveries WHERE reminder_id=$1', reminder.id)


async def test_one_off_success_repeated_pass_and_pool_reopen(storage_db, bot, clock):
    pool, reopen = storage_db
    saved = await create()
    assert await worker.run_once(bot, clock) == 1
    row = await event(pool, saved)
    assert row['state'] == 'sent' and row['message_id'] == 123 and row['sent_at'] == START
    assert not (await reminders.get(saved.id)).active
    assert await worker.run_once(bot, clock) == 0
    await pool.close()
    await reopen()
    assert await worker.run_once(bot, clock) == 0
    bot.send_message.assert_awaited_once()
    args, kwargs = bot.send_message.await_args
    assert args[0] == USER and '&lt;b&gt;' in args[1] and '&amp;' in args[1]
    assert kwargs['reply_markup'].inline_keyboard[0][0].callback_data == f'rd:off:{saved.id}'
    assert kwargs['request_timeout'] < store.LEASE.total_seconds()


async def test_count_anchor_downtime_and_completion_clock(storage_db, bot, clock):
    saved = await create(rule='FREQ=DAILY;COUNT=5')
    clock.now = START + timedelta(days=2, hours=23, minutes=59, seconds=55)

    async def send(*args, **kwargs):
        clock.now += timedelta(seconds=10)
        return SimpleNamespace(message_id=1)

    bot.send_message.side_effect = send
    await worker.run_once(bot, clock)
    current = await reminders.get(saved.id)
    assert current.rrule == saved.rrule and 'DTSTART' in current.rrule and 'COUNT=5' in current.rrule
    assert current.next_run_at == START + timedelta(days=4)
    assert current.active
    await worker.run_once(bot, clock)
    bot.send_message.assert_awaited_once()
    clock.now = START + timedelta(days=10)
    bot.send_message.side_effect = None
    await worker.run_once(bot, clock)
    assert not (await reminders.get(saved.id)).active
    assert bot.send_message.await_count == 2  # Calendar COUNT, not successful-delivery count.


@pytest.mark.parametrize('rule', ['FREQ=DAILY;COUNT=1', 'FREQ=DAILY;UNTIL=20260105T070000Z'])
async def test_count_until_end(storage_db, bot, clock, rule):
    saved = await create(rule=rule)
    await worker.run_once(bot, clock)
    assert not (await reminders.get(saved.id)).active
    assert (await reminders.get(saved.id)).rrule == saved.rrule
    await worker.run_once(bot, clock)
    bot.send_message.assert_awaited_once()


async def test_claim_is_exclusive_parallel_scans(storage_db, bot, clock):
    await create()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def send(*args, **kwargs):
        entered.set()
        await release.wait()
        return SimpleNamespace(message_id=1)

    bot.send_message.side_effect = send
    task = asyncio.create_task(worker.run_once(bot, clock))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert await worker.run_once(bot, clock) == 0
        assert all(not claims for claims in await asyncio.gather(*(store.claim(START) for _ in range(12))))
    finally:
        release.set()
        await task
    assert await worker.run_once(bot, clock) == 0
    bot.send_message.assert_awaited_once()


async def test_many_simultaneous_claims_only_one_wins(storage_db):
    await create()
    results = await asyncio.gather(*(store.claim(START) for _ in range(30)))
    assert sum(map(len, results)) == 1


async def test_expired_lease_reopen_and_stale_token_fenced(storage_db, bot, clock):
    pool, reopen = storage_db
    saved = await create()
    old, = await store.claim(clock())
    assert await store.claim(clock()) == []
    await pool.close()
    pool = await reopen()
    clock.now += store.LEASE
    fresh, = await store.claim(clock())
    assert old.token != fresh.token and fresh.attempts == 2
    assert not await store.authorize(old, clock())
    assert not await store.complete(old, clock(), state='sent')
    await worker._send(fresh, bot, clock)
    assert (await event(pool, saved))['state'] == 'sent'
    await worker.run_once(bot, clock)
    bot.send_message.assert_awaited_once()


async def test_http_deadline_below_lease_and_timeout_retry(storage_db, bot, clock, monkeypatch):
    pool, _ = storage_db
    saved = await create()
    assert worker.HTTP_TIMEOUT + 20 < store.LEASE.total_seconds()
    monkeypatch.setattr(worker, 'HTTP_TIMEOUT', 0)  # Deterministic cancellation at first await.

    async def blocked(*args, **kwargs):
        await asyncio.Event().wait()

    bot.send_message.side_effect = blocked
    await worker.run_once(bot, clock)
    row = await event(pool, saved)
    assert row['state'] == 'retry' and row['error_code'] == 'TimeoutError'


@pytest.mark.parametrize('failure,delay', [
    (TelegramNetworkError(METHOD, 'secret payload'), 30),
    (TelegramRetryAfter(METHOD, 'secret payload', retry_after=120), 120),
])
async def test_retry_and_retry_after(storage_db, bot, clock, failure, delay, caplog):
    pool, _ = storage_db
    saved = await create()
    bot.send_message.side_effect = failure
    await worker.run_once(bot, clock)
    row = await event(pool, saved)
    assert row['state'] == 'retry' and row['retry_at'] == START + timedelta(seconds=delay)
    assert (await reminders.get(saved.id)).active
    assert await worker.run_once(bot, clock) == 0
    clock.now += timedelta(seconds=delay)
    bot.send_message.side_effect = None
    await worker.run_once(bot, clock)
    assert (await event(pool, saved))['state'] == 'sent'
    assert bot.send_message.await_count == 2
    assert 'secret payload' not in caplog.text


async def test_backoff_increases_and_is_bounded(storage_db, bot, clock):
    pool, _ = storage_db
    saved = await create()
    bot.send_message.side_effect = OSError('secret')
    for delay in [30, 60, 120]:
        await worker.run_once(bot, clock)
        row = await event(pool, saved)
        assert row['retry_at'] == clock() + timedelta(seconds=delay)
        clock.now = row['retry_at']
    assert worker.backoff(100000) == timedelta(hours=1)


async def test_forbidden_blocks_only_recipient_persistently(storage_db, bot, clock):
    pool, reopen = storage_db
    bad = await create()
    await create(user=USER, when=START + timedelta(hours=1))
    good = await create(user=USER + 1)

    async def send(user_id, *args, **kwargs):
        if user_id == USER:
            raise TelegramForbiddenError(METHOD, 'blocked secret')
        return SimpleNamespace(message_id=1)

    bot.send_message.side_effect = send
    await worker.run_once(bot, clock)
    assert (await event(pool, bad))['state'] == 'failed'
    assert (await event(pool, good))['state'] == 'sent'
    assert await pool.fetchval('SELECT count(*) FROM reminder_blocked_users') == 1
    await pool.close()
    await reopen()
    clock.now += timedelta(days=1)
    await create(user=USER, when=clock())
    assert await worker.run_once(bot, clock) == 0
    assert bot.send_message.await_count == 2


@pytest.mark.parametrize('bad_text', [False, True])
async def test_permanent_error_isolated(storage_db, bot, clock, bad_text):
    pool, _ = storage_db
    bad = await create(text='x' * 4000 if bad_text else 'bad')
    good = await create(user=USER + 1)

    async def send(user_id, *args, **kwargs):
        if user_id == USER:
            raise TelegramBadRequest(METHOD, 'bad message secret')
        return SimpleNamespace(message_id=1)

    bot.send_message.side_effect = send
    await worker.run_once(bot, clock)
    assert (await event(pool, bad))['state'] == 'failed'
    assert not (await reminders.get(bad.id)).active
    assert (await event(pool, good))['state'] == 'sent'
    await worker.run_once(bot, clock)
    assert (await event(pool, bad))['attempts'] == 1


@pytest.mark.parametrize('rule', [
    'invalid persisted schedule',
    'DTSTART;TZID=Europe/Berlin:20260328T023000\nRRULE:FREQ=DAILY',
    'DTSTART:19000101T070000Z\nRRULE:FREQ=DAILY',
])
async def test_schedule_error_records_sent_and_stops_only_problem(storage_db, bot, clock, rule):
    pool, _ = storage_db
    bad = await create()
    await pool.execute('UPDATE reminders SET rrule=$2 WHERE id=$1', bad.id, rule)
    good = await create(user=USER + 1)
    clock.now = datetime(2026, 3, 28, 2, tzinfo=timezone.utc)
    await worker.run_once(bot, clock)
    row = await event(pool, bad)
    assert row['state'] == 'sent' and row['error_code'] == 'schedule_error'
    assert not (await reminders.get(bad.id)).active
    assert (await reminders.get(bad.id)).rrule == rule
    assert (await event(pool, good))['state'] == 'sent'
    await worker.run_once(bot, clock)
    assert bot.send_message.await_count == 2


@pytest.mark.parametrize('mutation', ['disable', 'delete', 'schedule', 'pause_resume', 'text'])
async def test_changes_during_http_survive_completion(storage_db, bot, clock, mutation):
    pool, _ = storage_db
    saved = await create(rule='FREQ=DAILY;COUNT=4')
    entered = asyncio.Event()
    release = asyncio.Event()
    next_time = START + timedelta(days=2)

    async def send(*args, **kwargs):
        entered.set()
        await release.wait()
        return SimpleNamespace(message_id=1)

    bot.send_message.side_effect = send
    task = asyncio.create_task(worker.run_once(bot, clock))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        if mutation == 'disable':
            assert await store.disable(saved.id, USER) == 'disabled'
        elif mutation == 'delete':
            await service.delete(saved.id)
        elif mutation == 'schedule':
            await service.set_schedule(saved.id, None, next_time)
        elif mutation == 'pause_resume':
            await service.pause(saved.id, False)
            await service.pause(saved.id, True)
        else:
            await pool.execute("UPDATE reminders SET text='new text' WHERE id=$1", saved.id)
        expected = await reminders.get(saved.id)
    finally:
        release.set()
        await task
    assert await reminders.get(saved.id) == expected
    if mutation == 'disable':
        assert expected.active is False
    if mutation == 'delete':
        assert expected is None
        assert await event(pool, saved) is None
    else:
        assert (await event(pool, saved))['state'] == 'sent'


async def test_schedule_change_cannot_overlap_live_old_http(storage_db, bot, clock):
    await create()
    saved, = await service.list_for(USER)
    old, = await store.claim(clock())
    await service.set_schedule(saved.id, None, START - timedelta(minutes=1))
    assert await store.claim(clock()) == []
    clock.now += store.LEASE
    fresh, = await store.claim(clock())
    assert fresh.reminder.next_run_at != old.reminder.next_run_at
    assert not await store.complete(old, clock(), state='sent')
    await worker._send(fresh, bot, clock)
    bot.send_message.assert_awaited_once()


async def test_disable_before_http_and_missing_owner(storage_db, bot, clock):
    saved = await create()
    item, = await store.claim(clock())
    assert await store.disable(saved.id, USER + 1) == 'forbidden'
    assert (await reminders.get(saved.id)).active
    assert await store.disable(saved.id, USER) == 'disabled'
    assert await store.disable(saved.id, USER) == 'disabled'
    await worker._send(item, bot, clock)
    bot.send_message.assert_not_awaited()
    await service.delete(saved.id)
    assert await store.disable(saved.id, USER) == 'missing'


async def test_success_and_next_time_rollback_together(storage_db, bot, clock):
    pool, _ = storage_db
    saved = await create(rule='FREQ=DAILY;COUNT=2')
    await pool.execute('ALTER TABLE reminders ADD CONSTRAINT test_stop_advance CHECK (next_run_at <= '
                       "'2026-01-05T07:00:00Z'::timestamptz)")
    await worker.run_once(bot, clock)
    assert (await event(pool, saved))['state'] == 'sending'
    assert await reminders.get(saved.id) == saved
    await pool.execute('ALTER TABLE reminders DROP CONSTRAINT test_stop_advance')
    clock.now += store.LEASE
    await worker.run_once(bot, clock)
    assert (await event(pool, saved))['state'] == 'sent'
    assert (await reminders.get(saved.id)).next_run_at == START + timedelta(days=1)
    assert bot.send_message.await_count == 2  # Documented ambiguous-send window after DB failure.
    await worker.run_once(bot, clock)
    assert bot.send_message.await_count == 2


async def test_cancelled_http_propagates_and_claim_recovers(storage_db, bot, clock):
    pool, _ = storage_db
    saved = await create()
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def send(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    bot.send_message.side_effect = send
    task = asyncio.create_task(worker.run_once(bot, clock))
    await asyncio.wait_for(entered.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    assert (await event(pool, saved))['state'] == 'sending'
    clock.now += store.LEASE
    bot.send_message.side_effect = None
    await worker.run_once(bot, clock)
    assert (await event(pool, saved))['state'] == 'sent'


async def test_batch_is_bounded(storage_db, bot, clock):
    for _ in range(7):
        await create()
    assert await worker.run_once(bot, clock, batch_size=3) == 3
    assert bot.send_message.await_count == 3
    assert await worker.run_once(bot, clock, batch_size=3) == 3
    assert bot.send_message.await_count == 6


async def test_short_remaining_lease_does_not_strand_event(storage_db, bot, clock):
    pool, _ = storage_db
    saved = await create()
    item, = await store.claim(clock())
    clock.now += store.LEASE - timedelta(seconds=19)
    await worker._send(item, bot, clock)
    bot.send_message.assert_not_awaited()
    assert (await event(pool, saved))['state'] == 'obsolete'
    # No HTTP started, so a fresh lease may safely be acquired immediately.
    assert await worker.run_once(bot, clock) == 1
    bot.send_message.assert_awaited_once()
    assert (await event(pool, saved))['state'] == 'sent'


async def test_database_failure_for_one_event_does_not_stop_others(storage_db, bot, clock, monkeypatch):
    pool, _ = storage_db
    bad = await create()
    good = await create(user=USER + 1)
    original = store.complete

    async def finish(item, *args, **kwargs):
        if item.reminder.id == bad.id:
            raise OSError('secret DATABASE_URL')
        return await original(item, *args, **kwargs)

    monkeypatch.setattr(store, 'complete', finish)
    await worker.run_once(bot, clock)
    assert (await event(pool, bad))['state'] == 'sending'
    assert (await event(pool, good))['state'] == 'sent'
    assert bot.send_message.await_count == 2


@pytest.mark.parametrize('mutation', ['disable', 'schedule'])
async def test_permanent_failure_does_not_overwrite_changed_reminder(storage_db, bot, clock, mutation):
    pool, _ = storage_db
    saved = await create()

    async def send(*args, **kwargs):
        if mutation == 'disable':
            await store.disable(saved.id, USER)
        else:
            await service.set_schedule(saved.id, None, START + timedelta(days=1))
        raise TelegramBadRequest(METHOD, 'synthetic failure')

    bot.send_message.side_effect = send
    await worker.run_once(bot, clock)
    current = await reminders.get(saved.id)
    assert (await event(pool, saved))['state'] == 'failed'
    if mutation == 'disable':
        assert not current.active
    else:
        assert current.active and current.next_run_at == START + timedelta(days=1)


@pytest.mark.parametrize('mutation', ['pause_resume', 'rule_same_instant'])
async def test_changed_version_recovers_failed_occurrence(storage_db, bot, clock, mutation):
    pool, _ = storage_db
    saved = await create(rule='FREQ=DAILY;COUNT=5')

    async def send(*args, **kwargs):
        if mutation == 'pause_resume':
            await service.pause(saved.id, False)
            await service.pause(saved.id, True)
        else:
            await service.set_schedule(saved.id, 'FREQ=WEEKLY;COUNT=3', START)
        raise TelegramBadRequest(METHOD, 'synthetic failure')

    bot.send_message.side_effect = send
    assert await worker.run_once(bot, clock) == 1
    failed = await event(pool, saved)
    current = await reminders.get(saved.id)
    assert failed['state'] == 'failed' and failed['attempts'] == 1
    assert current.active and current.next_run_at == START
    assert await service.due(clock()) == [current]
    latest_rule = current.rrule

    bot.send_message.side_effect = None
    # Concurrent scans must acquire exactly one replacement lease.
    passes = await asyncio.gather(*(worker.run_once(bot, clock) for _ in range(12)))
    assert sum(passes) == 1
    row = await event(pool, saved)
    assert row['state'] == 'sent' and row['attempts'] == 2
    assert row['token'] != failed['token']
    assert row['error_code'] is None
    current = await reminders.get(saved.id)
    assert current.active and current.rrule == latest_rule
    assert current.next_run_at == START + timedelta(days=1 if mutation == 'pause_resume' else 7)
    assert await service.due(clock()) == []
    for _ in range(3):
        assert await worker.run_once(bot, clock) == 0
    assert bot.send_message.await_count == 2


@pytest.mark.parametrize('changed', [False, True])
async def test_permanent_failure_stops_unchanged_version(storage_db, bot, clock, changed):
    pool, _ = storage_db
    saved = await create(rule='FREQ=DAILY;COUNT=5')

    async def send(*args, **kwargs):
        if changed and bot.send_message.await_count == 1:
            await service.pause(saved.id, False)
            await service.pause(saved.id, True)
        raise TelegramBadRequest(METHOD, 'synthetic permanent failure')

    bot.send_message.side_effect = send
    assert await worker.run_once(bot, clock) == 1
    if changed:
        assert await worker.run_once(bot, clock) == 1
    stopped = await reminders.get(saved.id)
    assert not stopped.active
    assert stopped.rrule == saved.rrule and stopped.next_run_at == START
    for _ in range(3):
        clock.now += timedelta(days=1)
        passes = await asyncio.gather(*(worker.run_once(bot, clock) for _ in range(5)))
        assert sum(passes) == 0
    row = await event(pool, saved)
    assert row['state'] == 'failed' and row['attempts'] == (2 if changed else 1)
    assert bot.send_message.await_count == (2 if changed else 1)


async def test_failed_recovery_lease_and_old_token_are_fenced(storage_db, bot, clock):
    pool, _ = storage_db
    saved = await create(rule='FREQ=DAILY;COUNT=5')
    old, = await store.claim(clock())
    await service.pause(saved.id, False)
    await service.pause(saved.id, True)
    assert await store.complete(old, clock(), state='failed', error_code='TelegramBadRequest')
    results = await asyncio.gather(*(store.claim(clock()) for _ in range(30)))
    assert sum(map(len, results)) == 1
    fresh, = [item for claims in results for item in claims]
    assert fresh.token != old.token and fresh.attempts == 2
    assert not await store.authorize(old, clock())
    assert not await store.complete(old, clock(), state='failed', blocked=True)
    assert await pool.fetchval('SELECT count(*) FROM reminder_blocked_users') == 0
    assert await store.claim(clock()) == []
    clock.now += store.LEASE
    replacement, = await store.claim(clock())
    assert replacement.token != fresh.token
    assert not await store.authorize(fresh, clock())
    assert not await store.complete(fresh, clock(), state='sent')
    await worker._send(replacement, bot, clock)
    for _ in range(3):
        assert await worker.run_once(bot, clock) == 0
    assert (await event(pool, saved))['state'] == 'sent'
    bot.send_message.assert_awaited_once()


@pytest.mark.parametrize('mutation', ['disable', 'delete', 'blocked', 'schedule'])
async def test_failed_recovery_respects_latest_user_state(storage_db, bot, clock, mutation):
    pool, _ = storage_db
    saved = await create(rule='FREQ=DAILY;COUNT=5')

    async def send(*args, **kwargs):
        await service.pause(saved.id, False)
        await service.pause(saved.id, True)
        if mutation == 'blocked':
            raise TelegramForbiddenError(METHOD, 'synthetic blocked recipient')
        raise TelegramBadRequest(METHOD, 'synthetic failure')

    bot.send_message.side_effect = send
    await worker.run_once(bot, clock)
    assert (await event(pool, saved))['state'] == 'failed'
    if mutation == 'disable':
        await store.disable(saved.id, USER)
    elif mutation == 'delete':
        await service.delete(saved.id)
    elif mutation == 'schedule':
        await service.set_schedule(saved.id, 'FREQ=WEEKLY;COUNT=3', START + timedelta(days=2))
    expected = await reminders.get(saved.id)
    for _ in range(3):
        assert await worker.run_once(bot, clock) == 0
    assert await reminders.get(saved.id) == expected
    bot.send_message.assert_awaited_once()
    if mutation == 'schedule':
        clock.now = expected.next_run_at
        bot.send_message.side_effect = None
        assert await worker.run_once(bot, clock) == 1
        current = await reminders.get(saved.id)
        assert current.active and current.rrule == expected.rrule
        assert current.next_run_at == expected.next_run_at + timedelta(days=7)
    elif mutation == 'delete':
        assert await event(pool, saved) is None
    elif mutation == 'blocked':
        # Even another user edit cannot override recipient suppression.
        await service.set_schedule(saved.id, 'FREQ=WEEKLY;COUNT=3', START)
        assert await worker.run_once(bot, clock) == 0
        bot.send_message.assert_awaited_once()


@pytest.mark.parametrize('mutation', ['pause_resume', 'rule_same_instant'])
async def test_changed_same_instant_advances_latest_rule_without_resending(storage_db, bot, clock, mutation):
    saved = await create(rule='FREQ=DAILY;COUNT=5')

    async def send(*args, **kwargs):
        if mutation == 'pause_resume':
            await service.pause(saved.id, False)
            await service.pause(saved.id, True)
        else:
            await service.set_schedule(saved.id, 'FREQ=WEEKLY;COUNT=3', START)
        return SimpleNamespace(message_id=1)

    bot.send_message.side_effect = send
    await worker.run_once(bot, clock)
    current = await reminders.get(saved.id)
    assert current.next_run_at == START  # Stale sender did not overwrite the mutation.
    latest_rule = current.rrule
    await worker.run_once(bot, clock)
    current = await reminders.get(saved.id)
    assert current.active and current.rrule == latest_rule
    assert current.next_run_at == START + timedelta(days=1 if mutation == 'pause_resume' else 7)
    bot.send_message.assert_awaited_once()


async def test_reconciliation_is_fenced_against_later_disable(storage_db, bot, clock):
    saved = await create(rule='FREQ=DAILY;COUNT=3')
    item, = await store.claim(clock())
    await service.pause(saved.id, False)
    await service.pause(saved.id, True)
    await store.complete(item, clock(), state='sent')
    snapshot, = await store.sent_to_advance()
    await store.disable(saved.id, USER)
    expected = await reminders.get(saved.id)
    await store.advance_sent(snapshot, START + timedelta(days=1), None)
    assert await reminders.get(saved.id) == expected
    await worker.run_once(bot, clock)
    bot.send_message.assert_not_awaited()
