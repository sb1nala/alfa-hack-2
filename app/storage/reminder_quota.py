"""Atomic per-user quota; no database transaction spans Telegram HTTP.

Every HTTP attempt reserves independently. Uncertain results (including a crash
between commit and HTTP) remain charged. RetryAfter and explicit Telegram
rejections refund, network failures do not. Lease recovery uses a new token and
therefore a new slot, never reuses an uncertain slot. Reservations in the current
local day or with an unexpired previously authorized window count after zone edits.
This deliberately conservative union also applies after deleting the profile.
"""

from datetime import datetime
from app.reminders.policy import allowed_at, day_bounds
from app.storage import db
from app.storage.reminder_delivery import AUTHORIZATION_MARGIN, Claim, complete

DAILY_LIMIT = 2
_LOCK_OWNER = "SELECT user_id FROM users WHERE user_id=$1 FOR UPDATE"
_COUNT_CHARGED = """SELECT count(*) FROM reminder_quota
    WHERE user_id=$1 AND state<>'released'
      AND (reserved_at >= $2 AND reserved_at < $3 OR day_end > $4)"""
_FINISH_STARTED = "UPDATE reminder_quota SET state=$2 WHERE token=$1 AND state='started'"


async def reserve(item: Claim, now: datetime) -> str | None:
    user_id = item.reminder.user_id
    async with db.pool().acquire() as c:
        async with c.transaction():
            # All attempts for an owner serialize, across all reminder kinds.
            await c.fetchval(_LOCK_OWNER, user_id)
            zone = (
                await c.fetchval(
                    "SELECT timezone FROM profiles WHERE user_id=$1",
                    user_id,
                )
                or "Europe/Moscow"
            )
            due = allowed_at(now, zone)
            if due == now:
                start, end = day_bounds(now, zone)
                count = await c.fetchval(_COUNT_CHARGED, user_id, start, end, now)
                if count >= DAILY_LIMIT:
                    due = allowed_at(now, zone, tomorrow=True)
                else:
                    await c.execute(
                        """INSERT INTO reminder_quota VALUES ($1,$2,$3,$4,'started')""",
                        item.token,
                        user_id,
                        now,
                        end,
                    )
                    return zone
    await complete(item, now, state="retry", retry_at=due, error_code="delivery_window")
    return None


async def finish(item: Claim, state: str) -> None:
    await db.pool().execute(_FINISH_STARTED, item.token, state)


async def current_zone(item: Claim, now: datetime) -> tuple[str | None, datetime | None]:
    """Return the authorized zone or release this attempt and return its retry time."""
    user_id = item.reminder.user_id
    async with db.pool().acquire() as c:
        async with c.transaction():
            await c.fetchval(_LOCK_OWNER, user_id)
            row = await c.fetchrow(
                """SELECT q.reserved_at, q.day_end,
                COALESCE(p.timezone,'Europe/Moscow') AS zone FROM reminder_quota q
                LEFT JOIN profiles p ON p.user_id=q.user_id
                JOIN reminders r ON r.id=$2 AND r.user_id=q.user_id
                JOIN reminder_deliveries d ON d.reminder_id=r.id AND d.scheduled_at=$3
                WHERE q.token=$1 AND q.state='started' AND r.active
                  AND r.xmin::text=$4 AND r.next_run_at=$3
                  AND d.token=$1 AND d.state='sending' AND d.lease_until > $5
                  AND NOT EXISTS (SELECT 1 FROM reminder_blocked_users b WHERE b.user_id=r.user_id)""",
                item.token,
                item.reminder.id,
                item.reminder.next_run_at,
                item.revision,
                now + AUTHORIZATION_MARGIN,
            )
            retry_at = None
            if row is not None:
                zone = row["zone"]
                due = allowed_at(now, zone)
                if due != now:
                    retry_at = due
                else:
                    start, end = day_bounds(now, zone)
                    if start <= row["reserved_at"] < end and now < row["day_end"]:
                        # The count already includes this token exactly once.
                        count = await c.fetchval(_COUNT_CHARGED, user_id, start, end, now)
                        if count <= DAILY_LIMIT:
                            # Keep both the original and newly authorized windows charged.
                            if end > row["day_end"]:
                                await c.execute(
                                    "UPDATE reminder_quota SET day_end=$2 WHERE token=$1 AND state='started'",
                                    item.token, end,
                                )
                            return zone, None
                        retry_at = allowed_at(now, zone, tomorrow=True)
            # No HTTP started. Release under the same owner lock so a competing
            # check sees the refund before deciding whether it can send.
            await c.execute(_FINISH_STARTED, item.token, 'released')
            return None, retry_at
