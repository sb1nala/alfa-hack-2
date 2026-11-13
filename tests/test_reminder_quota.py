"""Durable quota and quiet windows with actual PostgreSQL and fake Telegram."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from aiogram.exceptions import TelegramRetryAfter, TelegramBadRequest
from tests.test_reminder_delivery import START, USER, METHOD, Clock
from app.models import Reminder
from app.reminders import service, worker
from app.storage import (
    reminder_delivery as delivery,
    reminder_quota as quota,
    profiles,
    reminders,
)
from app.storage.db import SCHEMA_PATH


async def create(user=USER, kind="custom", rule=None, when=START):
    return await service.create(
        Reminder(0, user, kind, "Синтетический план", when, rule)
    )


def bot():
    return SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=1))
    )


async def test_shared_limit_deletion_reopen_and_fairness(storage_db):
    pool, reopen = storage_db
    b = bot()
    clock = Clock()
    rs = [await create(kind=k) for k in ("custom", "learning", "tax")]
    await worker.run_once(b, clock)
    assert b.send_message.await_count == 2
    deferred = await pool.fetchrow(
        "SELECT * FROM reminder_deliveries WHERE state='retry'"
    )
    assert deferred["retry_at"] == START.replace(day=6, hour=6)
    await service.delete(rs[0].id)
    assert (
        await pool.fetchval("SELECT count(*) FROM reminder_quota WHERE state='sent'")
        == 2
    )
    await pool.close()
    pool = await reopen()
    for _ in range(25):
        await create()
    other = await create(user=USER + 1)
    await worker.run_once(b, clock, batch_size=2)
    assert not (await reminders.get(other.id)).active
    clock.now = START + timedelta(days=1)
    await worker.run_once(b, clock)
    assert (
        await pool.fetchval("SELECT count(*) FROM reminder_quota WHERE state='sent'")
        == 5
    )  # yesterday 2 + other 1 + today 2


async def test_parallel_last_slot(storage_db):
    pool, _ = storage_db
    b = bot()
    clock = Clock()
    await create()
    await worker.run_once(b, clock)
    for _ in range(8):
        await create()
    await asyncio.gather(*(worker.run_once(b, clock) for _ in range(5)))
    assert b.send_message.await_count == 2
    assert (
        await pool.fetchval(
            "SELECT count(*) FROM reminder_quota WHERE state<>'released'"
        )
        == 2
    )


@pytest.mark.parametrize(
    "zone,hour,expected", [("Europe/Moscow", 18, 6), ("Asia/Vladivostok", 11, 23)]
)
async def test_night_preserves_event_and_schedule(storage_db, zone, hour, expected):
    pool, _ = storage_db
    clock = Clock(START.replace(hour=hour))
    b = bot()
    await profiles.update(USER, {"timezone": zone})
    r = await create(when=clock(), rule="FREQ=DAILY;COUNT=5")
    await worker.run_once(b, clock)
    b.send_message.assert_not_awaited()
    d = await pool.fetchrow("SELECT * FROM reminder_deliveries")
    assert d["scheduled_at"] == r.next_run_at and d["retry_at"].hour == expected
    assert await reminders.get(r.id) == r
    clock.now = d["retry_at"]
    await worker.run_once(b, clock)
    assert b.send_message.await_count == 1
    assert (await reminders.get(r.id)).rrule == r.rrule
    assert (
        await pool.fetchval("SELECT scheduled_at FROM reminder_deliveries")
        == r.next_run_at
    )


@pytest.mark.parametrize(
    "error,charged",
    [
        (OSError("synthetic"), True),
        (TelegramRetryAfter(METHOD, "synthetic", retry_after=10), False),
        (TelegramBadRequest(METHOD, "synthetic"), False),
    ],
)
async def test_failure_accounting(storage_db, error, charged):
    pool, _ = storage_db
    b = bot()
    b.send_message.side_effect = error
    await create()
    await worker.run_once(b, Clock())
    assert (
        bool(
            await pool.fetchval(
                "SELECT count(*) FROM reminder_quota WHERE state<>'released'"
            )
        )
        == charged
    )


async def test_crash_reserved_recovery_and_unknown_does_not_refund(storage_db):
    pool, reopen = storage_db
    await create()
    clock = Clock()
    b = bot()
    (item,) = await delivery.claim(clock())
    assert await quota.reserve(item, clock()) == "Europe/Moscow"
    # Simulate death after commit: outcome unknown, old token must not be reused.
    await pool.close()
    pool = await reopen()
    clock.now += delivery.LEASE
    await worker.run_once(b, clock)
    assert b.send_message.await_count == 1
    assert (
        await pool.fetchval(
            "SELECT count(*) FROM reminder_quota WHERE state<>'released'"
        )
        == 2
    )
    await create()
    await worker.run_once(b, clock)
    assert b.send_message.await_count == 1


async def test_timezone_reset_does_not_open_empty_counter(storage_db):
    pool, _ = storage_db
    # Vladivostok late evening vs Moscow afternoon, both are allowed initially.
    clock = Clock(START.replace(hour=10))
    b = bot()
    await create()
    await create()
    await worker.run_once(b, clock)
    await profiles.update(USER, {"timezone": "Asia/Vladivostok"})
    await create()
    await worker.run_once(b, clock)
    assert b.send_message.await_count == 2
    await profiles.reset(USER)
    await create()
    await worker.run_once(b, clock)
    assert b.send_message.await_count == 2


async def test_schema_upgrade_and_reapply(storage_db):
    pool, _ = storage_db
    r = await create()
    b = bot()
    await worker.run_once(b, Clock())
    quota_before = await pool.fetch("SELECT * FROM reminder_quota")
    await pool.execute(SCHEMA_PATH.read_text())
    await pool.execute(SCHEMA_PATH.read_text())
    assert await pool.fetch("SELECT * FROM reminder_quota") == quota_before
    assert await reminders.get(r.id)
    # Upgrade from the exact old schema, preserving existing rows.
    for table in ("reminder_actions", "reminder_delivery_buttons", "reminder_quota"):
        await pool.execute(f"DROP TABLE {table}")
    before = await pool.fetch("SELECT * FROM reminders")
    await pool.execute(SCHEMA_PATH.read_text())
    assert await pool.fetch("SELECT * FROM reminders") == before


async def test_recheck_at_http_boundary_releases_before_http(storage_db, monkeypatch):
    pool, _ = storage_db
    r = await create()
    b = bot()
    clock = Clock(START.replace(hour=17, minute=59, second=59))
    original = delivery.authorize
    calls = 0

    async def authorize(item, now):
        nonlocal calls
        result = await original(item, now)
        calls += 1
        if calls == 2:
            clock.now = clock.now.replace(hour=18, minute=0, second=0)
        return result

    monkeypatch.setattr(delivery, "authorize", authorize)
    await worker.run_once(b, clock)
    b.send_message.assert_not_awaited()
    assert (
        await pool.fetchval(
            "SELECT count(*) FROM reminder_quota WHERE state<>'released'"
        )
        == 0
    )
    assert (await reminders.get(r.id)).active


async def test_zone_change_across_local_date_counts_saved_utc(storage_db):
    clock = Clock(START.replace(hour=17))
    b = bot()
    await create()
    await create()
    await worker.run_once(b, clock)
    assert b.send_message.await_count == 2
    await profiles.update(USER, {"timezone": "Asia/Vladivostok"})
    await create()
    await worker.run_once(b, clock)  # 03:00 next local date
    clock.now = clock.now.replace(hour=23)  # 09:00 Vladivostok; old Moscow day ended
    await worker.run_once(b, clock)
    assert (
        b.send_message.await_count == 2
    )  # both UTC instants fall in this new local day
