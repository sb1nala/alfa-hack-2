"""Bounded delivery attempts with durable retry, quota and fenced leases.

An HTTP request already started cannot be reliably cancelled by «Отключить».
If Telegram accepted a message but the DB commit fails, recovery may duplicate
it within the daily attempt quota: Telegram sendMessage has no idempotency key.
Ordinary completed events are never sent twice. Cancelled attempts remain leased and recover after expiry.
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest, TelegramForbiddenError, TelegramNotFound, TelegramRetryAfter,
)

from app.bot.reminders import disable_keyboard
from app.models import Reminder
from app.reminders.content import delivery
from app.reminders.schedule import aware_utc, next_occurrence
from app.storage import reminder_delivery as store
from app.storage import reminder_quota as quota
from app.reminders.policy import allowed_at

log = logging.getLogger(__name__)
HTTP_TIMEOUT = 15  # Hard outer deadline, below 90s lease with a 20s authorization margin.
BATCH_SIZE = 10


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(3600, 30 * 2 ** min(max(attempts - 1, 0), 7)))


def following(reminder: Reminder, completed_at: datetime) -> tuple[datetime | None, str | None]:
    if reminder.rrule is not None:
        try:
            return next_occurrence(reminder.rrule, completed_at), None
        except (ValueError, TypeError, OverflowError):
            return None, 'schedule_error'
    return None, None


async def _send(item: store.Claim, bot: Bot, clock: Callable[[], datetime]) -> None:
    if not await store.authorize(item, aware_utc(clock())):
        await store.complete(item, aware_utc(clock()), state='obsolete')
        return
    try:
        text = delivery(item.reminder.text)
    except ValueError:
        await store.complete(item, aware_utc(clock()), state='failed', error_code='invalid_text')
        return
    zone = await quota.reserve(item, aware_utc(clock()))
    if zone is None:
        return
    # Recheck after the database roundtrip, directly before starting HTTP.
    now = aware_utc(clock())
    if not await store.authorize(item, now):
        await quota.finish(item, 'released')
        await store.complete(item, now, state='obsolete')
        return
    zone = await quota.current_zone(item, aware_utc(clock()))
    now = aware_utc(clock())
    if zone is None:
        await quota.finish(item, 'released')
        await store.complete(item, now, state='retry', retry_at=now)
        return
    permitted = allowed_at(now, zone)
    if permitted != now:
        await quota.finish(item, 'released')
        await store.complete(item, now, state='retry', retry_at=permitted)
        return
    try:
        async with asyncio.timeout(HTTP_TIMEOUT):
            message = await bot.send_message(
                item.reminder.user_id, text, parse_mode='HTML',
                reply_markup=disable_keyboard(item.reminder.id, item.token.hex), request_timeout=12)
    except TelegramRetryAfter as error:
        await quota.finish(item, 'released')
        now = aware_utc(clock())
        await store.complete(item, now, state='retry', error_code='TelegramRetryAfter',
                             retry_at=now + max(backoff(item.attempts),
                                                timedelta(seconds=error.retry_after)))
        return
    except TelegramForbiddenError:
        await quota.finish(item, 'released')
        await store.complete(item, aware_utc(clock()), state='failed',
                             error_code='TelegramForbiddenError', blocked=True)
        return
    except (TelegramBadRequest, TelegramNotFound) as error:
        await quota.finish(item, 'released')
        await store.complete(item, aware_utc(clock()), state='failed',
                             error_code=type(error).__name__)
        return
    except Exception as error:
        # No exception text: Telegram/network exceptions may contain sensitive data.
        now = aware_utc(clock())
        await store.complete(item, now, state='retry', error_code=type(error).__name__,
                             retry_at=now + backoff(item.attempts))
        return
    await quota.finish(item, 'sent')
    completed_at = aware_utc(clock())
    # Delivery succeeded: even a calculation failure records sent and stops.
    next_run, error_code = following(item.reminder, completed_at)
    await store.complete(item, aware_utc(clock()), state='sent', error_code=error_code,
                         next_run_at=next_run, message_id=message.message_id)


async def run_once(bot: Bot, clock: Callable[[], datetime] = utcnow,
                   batch_size: int = BATCH_SIZE) -> int:
    """One bounded batch: at most one overdue event per reminder, no catch-up loop."""
    if not 1 <= batch_size <= 20:
        raise ValueError('batch_size must be between 1 and 20')
    for receipt in await store.sent_to_advance(batch_size):
        try:
            # Use the latest schedule, not the stale sender's snapshot. A sent
            # occurrence stays sent after pause/resume or a rule change keeping
            # the same instant; it must neither resend nor strand the schedule.
            next_run, error_code = following(
                receipt.reminder, max(receipt.sent_at, aware_utc(clock())))
            await store.advance_sent(receipt, next_run, error_code)
        except Exception as error:
            log.warning('reminder reconciliation failed: id=%d error=%s',
                        receipt.reminder.id, type(error).__name__)
    items = await store.claim(aware_utc(clock()), batch_size)

    async def isolated(item: store.Claim) -> None:
        try:
            await _send(item, bot, clock)
        except Exception as error:
            # In particular, a DB error after Telegram leaves the claim recoverable.
            log.warning('reminder delivery failed: id=%d error=%s',
                        item.reminder.id, type(error).__name__)

    # Each acquired lease starts immediately; no queued HTTP behind other claims.
    await asyncio.gather(*(isolated(item) for item in items))
    return len(items)


async def run(bot: Bot, clock: Callable[[], datetime] = utcnow,
              interval: float = 30) -> None:
    while True:
        try:
            await run_once(bot, clock)
        except Exception as error:
            log.warning('reminder scan failed: %s', type(error).__name__)
        await asyncio.sleep(interval)
