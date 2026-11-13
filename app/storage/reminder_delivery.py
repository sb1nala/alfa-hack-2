"""Delivery claims and fenced completion. Never hold a DB lock during HTTP.

Lock order is always reminder -> event. xmin is an optimistic row version:
any pause, resume, schedule/text update invalidates a claimed snapshot, even
when values are later restored (ABA). Sent events are never reclaimed.
Failed events may be reclaimed only by a changed reminder version: a user edit
allows a fresh attempt with the latest schedule/text. A permanent error on an
unchanged version still stops the reminder, so scans cannot retry it forever.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from app.models import Reminder
from app.storage import db

LEASE = timedelta(seconds=90)
AUTHORIZATION_MARGIN = timedelta(seconds=20)


@dataclass(frozen=True)
class Claim:
    reminder: Reminder
    revision: str
    token: UUID
    attempts: int


@dataclass(frozen=True)
class SentSnapshot:
    reminder: Reminder
    revision: str
    sent_at: datetime


async def sent_to_advance(limit: int = 10) -> list[SentSnapshot]:
    """Find a changed schedule whose current occurrence was already delivered.

    Completion cannot overwrite a user mutation. A later pass can independently
    advance the latest version without sending the same event again.
    """
    rows = await db.pool().fetch('''
        SELECT r.*, r.xmin::text AS revision, d.sent_at FROM reminders r
        JOIN reminder_deliveries d ON d.reminder_id=r.id AND d.scheduled_at=r.next_run_at
        WHERE r.active AND d.state='sent' AND d.revision <> r.xmin::text
        ORDER BY r.next_run_at, r.id LIMIT $1
    ''', limit)
    return [SentSnapshot(
        Reminder(**{k: v for k, v in dict(row).items() if k not in {'revision', 'sent_at'}}),
        row['revision'], row['sent_at'],
    ) for row in rows]


async def advance_sent(item: SentSnapshot, next_run_at: datetime | None,
                       error_code: str | None) -> None:
    r = item.reminder
    async with db.pool().acquire() as connection:
        async with connection.transaction():
            changed = await connection.fetchval('''
                UPDATE reminders r SET active=$4, next_run_at=COALESCE($5,next_run_at)
                WHERE id=$1 AND xmin::text=$2 AND active AND next_run_at=$3
                  AND EXISTS (SELECT 1 FROM reminder_deliveries d WHERE d.reminder_id=r.id
                      AND d.scheduled_at=$3 AND d.state='sent')
                RETURNING id
            ''', r.id, item.revision, r.next_run_at, next_run_at is not None, next_run_at)
            if changed is not None and error_code is not None:
                await connection.execute('''
                    UPDATE reminder_deliveries SET error_code=$3
                    WHERE reminder_id=$1 AND scheduled_at=$2 AND state='sent'
                ''', r.id, r.next_run_at, error_code)


async def claim(now: datetime, limit: int = 10) -> list[Claim]:
    if not 1 <= limit <= 20:
        raise ValueError('claim limit must be between 1 and 20')
    async with db.pool().acquire() as connection:
        async with connection.transaction():
            rows = await connection.fetch('''
                SELECT r.*, r.xmin::text AS revision FROM reminders r
                LEFT JOIN reminder_deliveries d
                  ON d.reminder_id=r.id AND d.scheduled_at=r.next_run_at
                WHERE r.active AND r.next_run_at <= $1
                  AND NOT EXISTS (SELECT 1 FROM reminder_blocked_users b WHERE b.user_id=r.user_id)
                  AND NOT EXISTS (SELECT 1 FROM reminder_deliveries busy
                      WHERE busy.reminder_id=r.id AND busy.state='sending' AND busy.lease_until > $1)
                  AND (d.reminder_id IS NULL
                       OR (d.state='sending' AND d.lease_until <= $1)
                       OR (d.state='retry' AND d.retry_at <= $1)
                       OR (d.state='failed' AND d.revision <> r.xmin::text)
                       OR d.state='obsolete')
                ORDER BY (SELECT count(*) FROM reminders earlier
                    WHERE earlier.user_id=r.user_id AND earlier.active AND earlier.next_run_at <= $1
                      AND (earlier.next_run_at,earlier.id)<(r.next_run_at,r.id)),
                    r.next_run_at, r.id LIMIT $2 FOR UPDATE OF r SKIP LOCKED
            ''', now, limit)
            claims = []
            for row in rows:
                # Re-read after acquiring the reminder lock. The SELECT's
                # READ COMMITTED snapshot may predate a rival claim's commit,
                # even if that rival released its lock before we reached it.
                if await connection.fetchval('''
                    SELECT 1 FROM reminder_deliveries WHERE reminder_id=$1
                        AND state='sending' AND lease_until > $2
                ''', row['id'], now):
                    continue
                previous = await connection.fetchrow('''
                    SELECT * FROM reminder_deliveries
                    WHERE reminder_id=$1 AND scheduled_at=$2
                ''', row['id'], row['next_run_at'])
                if previous is not None and not (
                    (previous['state'] == 'sending' and previous['lease_until'] <= now)
                    or (previous['state'] == 'retry' and previous['retry_at'] <= now)
                    or (previous['state'] == 'failed' and previous['revision'] != row['revision'])
                    or previous['state'] == 'obsolete'
                ):
                    continue
                # An expired claim for a previous schedule cannot finish or
                # overlap the new claim. The partial unique index is a backstop.
                await connection.execute('''
                    UPDATE reminder_deliveries SET state='obsolete'
                    WHERE reminder_id=$1 AND state='sending' AND lease_until <= $2
                ''', row['id'], now)
                token = uuid4()
                event = await connection.fetchrow('''
                    INSERT INTO reminder_deliveries
                        (reminder_id,scheduled_at,revision,state,token,lease_until,retry_at)
                    VALUES ($1,$2,$3,'sending',$4,$5,$6)
                    ON CONFLICT (reminder_id,scheduled_at) DO UPDATE SET
                        revision=EXCLUDED.revision, state='sending', token=EXCLUDED.token,
                        lease_until=EXCLUDED.lease_until, attempts=reminder_deliveries.attempts+1,
                        retry_at=EXCLUDED.retry_at, error_code=NULL
                    RETURNING attempts
                ''', row['id'], row['next_run_at'], row['revision'], token, now + LEASE, now)
                model = Reminder(**{k: v for k, v in dict(row).items() if k != 'revision'})
                claims.append(Claim(model, row['revision'], token, event['attempts']))
            return claims


async def authorize(item: Claim, now: datetime) -> bool:
    """Last check immediately before bounded HTTP; no lock spans the request."""
    r = item.reminder
    return bool(await db.pool().fetchval('''
        SELECT 1 FROM reminders r JOIN reminder_deliveries d ON d.reminder_id=r.id
        WHERE r.id=$1 AND r.active AND r.xmin::text=$2 AND r.next_run_at=$3
          AND d.scheduled_at=$3 AND d.token=$4 AND d.state='sending'
          AND d.lease_until > $5
          AND NOT EXISTS (SELECT 1 FROM reminder_blocked_users b WHERE b.user_id=r.user_id)
    ''', r.id, item.revision, r.next_run_at, item.token, now + AUTHORIZATION_MARGIN))


async def complete(item: Claim, now: datetime, *, state: str, error_code: str | None = None,
                   retry_at: datetime | None = None, next_run_at: datetime | None = None,
                   message_id: int | None = None, blocked: bool = False) -> bool:
    """Atomically record outcome and conditionally advance/stop the reminder.

    A deleted reminder cascades its events. An old token or expired lease cannot
    complete a replacement attempt. Sent remains sent even on RRULE failure.
    """
    if state not in {'sent', 'retry', 'failed', 'obsolete'}:
        raise ValueError('invalid completion state')
    r = item.reminder
    async with db.pool().acquire() as connection:
        async with connection.transaction():
            current = await connection.fetchrow(
                'SELECT id FROM reminders WHERE id=$1 FOR UPDATE', r.id)
            if current is None:
                return False
            changed = await connection.fetchval('''
                UPDATE reminder_deliveries SET state=$5, error_code=$6,
                    retry_at=COALESCE($7,retry_at), sent_at=$8, message_id=$9
                WHERE reminder_id=$1 AND scheduled_at=$2 AND token=$3
                    AND state='sending' AND lease_until > $4 RETURNING reminder_id
            ''', r.id, r.next_run_at, item.token, now, state, error_code, retry_at,
                now if state == 'sent' else None, message_id)
            if changed is None:
                return False
            if blocked:
                await connection.execute('''
                    INSERT INTO reminder_blocked_users (user_id,blocked_at,error_code)
                    VALUES ($1,$2,'TelegramForbiddenError') ON CONFLICT (user_id) DO NOTHING
                ''', r.user_id, now)
            advanced_revision = None
            if state in {'sent', 'failed'}:
                advanced_revision = await connection.fetchval('''
                    UPDATE reminders SET active=$4, next_run_at=COALESCE($5,next_run_at)
                    WHERE id=$1 AND xmin::text=$2 AND active AND next_run_at=$3
                    RETURNING xmin::text
                ''', r.id, item.revision, r.next_run_at,
                    state == 'sent' and next_run_at is not None, next_run_at)
            if state == 'sent' and advanced_revision is not None:
                # Advance changes xmin; bind the delivered button to that result,
                # only if this worker actually advanced its unchanged snapshot.
                await connection.execute('''
                    INSERT INTO reminder_delivery_buttons (token,reminder_id,revision)
                    VALUES ($1,$2,$3) ON CONFLICT DO NOTHING
                ''', item.token, r.id, advanced_revision)
            return True


async def disable(reminder_id: int, user_id: int) -> str:
    """Owner check and mutation share a lock; repeated disabling is harmless."""
    async with db.pool().acquire() as connection:
        async with connection.transaction():
            row = await connection.fetchrow(
                'SELECT user_id, active FROM reminders WHERE id=$1 FOR UPDATE', reminder_id)
            if row is None:
                return 'missing'
            if row['user_id'] != user_id:
                return 'forbidden'
            if row['active']:
                await connection.execute('UPDATE reminders SET active=false WHERE id=$1', reminder_id)
            return 'disabled'
