"""Parameterized reminder SQL and record conversion; uses the shared pool."""

from datetime import datetime

import asyncpg

from app.models import Reminder
from app.storage import db
from app.storage._common import ensure_user


def _model(row: asyncpg.Record | None) -> Reminder | None:
    return Reminder(**dict(row)) if row is not None else None


async def insert(reminder: Reminder) -> Reminder:
    async with db.pool().acquire() as connection:
        async with connection.transaction():
            await ensure_user(connection, reminder.user_id)
            row = await connection.fetchrow(
                "INSERT INTO reminders (user_id, kind, text, next_run_at, rrule, active) "
                "VALUES ($1,$2,$3,$4,$5,$6) RETURNING *",
                reminder.user_id, reminder.kind, reminder.text, reminder.next_run_at,
                reminder.rrule, reminder.active,
            )
    return _model(row)


async def get(reminder_id: int) -> Reminder | None:
    return _model(await db.pool().fetchrow("SELECT * FROM reminders WHERE id=$1", reminder_id))


async def list_for(user_id: int) -> list[Reminder]:
    rows = await db.pool().fetch(
        "SELECT * FROM reminders WHERE user_id=$1 ORDER BY next_run_at, id", user_id)
    return [_model(row) for row in rows]


async def set_schedule(
    reminder_id: int, rrule: str | None, next_run_at: datetime,
) -> Reminder | None:
    return _model(await db.pool().fetchrow(
        "UPDATE reminders SET rrule=$2, next_run_at=$3 WHERE id=$1 RETURNING *",
        reminder_id, rrule, next_run_at))


async def pause(reminder_id: int, active: bool) -> bool:
    return await db.pool().fetchval(
        "UPDATE reminders SET active=$2 WHERE id=$1 RETURNING id", reminder_id, active) is not None


async def delete(reminder_id: int) -> None:
    await db.pool().execute("DELETE FROM reminders WHERE id=$1", reminder_id)


async def due(now: datetime) -> list[Reminder]:
    rows = await db.pool().fetch(
        "SELECT * FROM reminders WHERE active AND next_run_at <= $1 ORDER BY next_run_at, id", now)
    return [_model(row) for row in rows]
