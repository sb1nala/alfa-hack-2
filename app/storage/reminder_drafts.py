"""Durable drafts: serialize confirm/cancel on one connection and transaction."""

from datetime import datetime, timedelta
from uuid import UUID, uuid4

from app.models import Reminder
from app.reminders.content import checked_text, confirmation, units
from app.reminders.schedule import aware_utc
from app.reminders.service import validate_new
from app.storage import db, reminders
from app.storage._common import ensure_user, validate_user_id


async def create(user_id: int, text: str, run_at: datetime, zone: str, now: datetime) -> UUID:
    validate_user_id(user_id)
    text = checked_text(text)
    model = validate_new(Reminder(0, user_id, 'custom', text, run_at, None))
    now = aware_utc(now)
    if model.next_run_at <= now:
        raise ValueError('Укажите время в будущем.')
    if units(confirmation(text, run_at, zone)) > 4096:
        raise ValueError('Текст слишком длинный.')
    draft_id = uuid4()
    async with db.pool().acquire() as connection:
        async with connection.transaction():
            await ensure_user(connection, user_id)
            # Пользователь сам пишет боту — значит, он его разблокировал. Без этого запрет,
            # поставленный после TelegramForbiddenError, остался бы навсегда: напоминание
            # «сохранено», но никогда не придёт.
            await connection.execute(
                'DELETE FROM reminder_blocked_users WHERE user_id=$1', user_id)
            await connection.execute(
                'INSERT INTO reminder_drafts (id,user_id,text,run_at,timezone,expires_at) '
                'VALUES ($1,$2,$3,$4,$5,$6)', draft_id, user_id, text,
                model.next_run_at, zone, now + timedelta(minutes=15))
    return draft_id


async def decide(draft_id: UUID, user_id: int, confirm: bool, now: datetime) -> str:
    validate_user_id(user_id)
    now = aware_utc(now)
    async with db.pool().acquire() as connection:
        async with connection.transaction():
            row = await connection.fetchrow(
                'SELECT * FROM reminder_drafts WHERE id=$1 FOR UPDATE', draft_id)
            if row is None:
                return 'missing'
            if row['user_id'] != user_id:
                return 'forbidden'
            if row['state'] != 'pending':
                if row['state'] == 'confirmed' and row['reminder_id'] is None:
                    return 'deleted'
                return row['state']
            if row['expires_at'] <= now or row['run_at'] <= now:
                state = 'expired'
                reminder_id = None
            elif not confirm:
                state = 'cancelled'
                reminder_id = None
            else:
                # Pure validation plus insert_on: no second pool connection.
                model = validate_new(Reminder(
                    0, user_id, 'custom', row['text'], row['run_at'], None))
                saved = await reminders.insert_on(connection, model)
                reminder_id = saved.id
                state = 'confirmed'
            await connection.execute(
                'UPDATE reminder_drafts SET state=$2, reminder_id=$3 WHERE id=$1',
                draft_id, state, reminder_id)
            return state
