"""Atomic per-user quota; no database transaction spans Telegram HTTP.

Every HTTP attempt reserves independently. Uncertain results (including a crash
between commit and HTTP) remain charged. RetryAfter and explicit Telegram
rejections refund, network failures do not. Lease recovery uses a new token and
therefore a new slot, never reuses an uncertain slot. UTC instants in the current
local day AND unexpired previously reserved day windows count after zone edits.
This deliberately conservative union also applies after deleting the profile.
"""

from datetime import datetime, timedelta
from app.reminders.policy import allowed_at, day_bounds
from app.storage import db
from app.storage.reminder_delivery import Claim, complete


async def reserve(item: Claim, now: datetime) -> str | None:
    async with db.pool().acquire() as c:
        async with c.transaction():
            # All attempts for an owner serialize, across all reminder kinds.
            await c.fetchval(
                "SELECT user_id FROM users WHERE user_id=$1 FOR UPDATE",
                item.reminder.user_id,
            )
            zone = (
                await c.fetchval(
                    "SELECT timezone FROM profiles WHERE user_id=$1",
                    item.reminder.user_id,
                )
                or "Europe/Moscow"
            )
            due = allowed_at(now, zone)
            if due == now:
                start, end = day_bounds(now, zone)
                count = await c.fetchval(
                    """SELECT count(*) FROM reminder_quota
                    WHERE user_id=$1 AND state<>'released'
                      AND (reserved_at >= $2 AND reserved_at < $3 OR day_end > $4)""",
                    item.reminder.user_id,
                    start,
                    end,
                    now,
                )
                if count >= 2:
                    due = allowed_at(now, zone, tomorrow=True)
                else:
                    await c.execute(
                        """INSERT INTO reminder_quota VALUES ($1,$2,$3,$4,'started')""",
                        item.token,
                        item.reminder.user_id,
                        now,
                        end,
                    )
                    return zone
    await complete(item, now, state="retry", retry_at=due, error_code="delivery_window")
    return None


async def finish(item: Claim, state: str) -> None:
    await db.pool().execute(
        "UPDATE reminder_quota SET state=$2 WHERE token=$1", item.token, state
    )


async def current_zone(item: Claim, now: datetime) -> str | None:
    """Refresh profile after authorization and reject a crossed reservation day."""
    row = await db.pool().fetchrow(
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
        now + timedelta(seconds=20),
    )
    if row is None:
        return None
    start, end = day_bounds(now, row["zone"])
    # A zone change may put the reservation outside the new day; an old still
    # open day window remains charged, but a fresh attempt gets a fresh check.
    if not (start <= row["reserved_at"] < end and now < row["day_end"]):
        return None
    return row["zone"]
