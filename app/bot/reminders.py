"""Explicit reminder commands and owner-checked durable callback buttons."""

import logging
from datetime import datetime, timedelta, timezone
from html import escape
from uuid import UUID

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.llm import client as llm
from app.reminders.command import parse_command
from app.reminders.content import confirmation
from app.reminders.parse import NotReminder, looks_like_reminder, parse_request
from app.storage import profiles, reminder_delivery, reminder_drafts

log = logging.getLogger(__name__)
RESULTS = {
    'confirmed': 'Напоминание подтверждено и сохранено.',
    'cancelled': 'Создание напоминания отменено.',
    'expired': 'Срок подтверждения или время напоминания прошли. Создайте новое через /remind.',
    'forbidden': 'Эта кнопка принадлежит другому пользователю.',
    'missing': 'Запись больше не существует. Создайте новое напоминание через /remind.',
    'disabled': 'Напоминание отключено.',
    'deleted': 'Подтверждённое напоминание уже удалено.',
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def draft_keyboard(draft_id: UUID) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='Подтвердить', callback_data=f'rd:c:{draft_id.hex}'),
        InlineKeyboardButton(text='Отмена', callback_data=f'rd:x:{draft_id.hex}'),
    ]])


def disable_keyboard(reminder_id: int, revision: str | None = None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='Отключить', callback_data=f'rd:off:{reminder_id}'),
        *([InlineKeyboardButton(text='Реже', callback_data=f'rm:less:{reminder_id}:{revision}')] if revision else [])]])


async def on_remind(message: Message) -> None:
    if message.chat.type != 'private':
        await message.answer('Напоминания создаются в личном чате. Напишите мне команду /remind там.')
        return
    if message.from_user is None or message.text is None:
        return
    try:
        profile = await profiles.get(message.from_user.id)
        now = utcnow()
        body, run_at = parse_command(message.text, profile.timezone, now)
        draft_id = await reminder_drafts.create(
            message.from_user.id, body, run_at, profile.timezone, now)
    except ValueError as error:
        await message.answer(escape(str(error)))
        return
    except Exception as error:
        log.warning('reminder draft creation failed: %s', type(error).__name__)
        await message.answer('Не удалось сохранить черновик. Попробуйте команду ещё раз чуть позже.')
        return
    await message.answer(confirmation(body, run_at, profile.timezone),
                         reply_markup=draft_keyboard(draft_id))


# Просьба, в которой не хватило времени или текста: ждём уточнения в следующем сообщении.
# Память в процессе, на десять минут; после перезапуска бот просто переспросит.
PENDING_TTL = timedelta(minutes=10)
_pending: dict[int, tuple[str, datetime]] = {}

NO_MODEL_TEXT = (
    'Сейчас я понимаю напоминания только в точном виде:\n'
    '/remind 2027-01-15 10:00 Проверить план'
)
PARSE_FAILED_TEXT = (
    'Не получилось разобрать просьбу. Попробуйте ещё раз или напишите точно:\n'
    '/remind 2027-01-15 10:00 Проверить план'
)


def reset_pending() -> None:
    _pending.clear()


def cancel_pending(user_id: int) -> None:
    """Explicit commands must not become the answer to a reminder clarification."""
    _pending.pop(user_id, None)


async def on_free_text(message: Message) -> bool:
    """Просьба о напоминании обычной речью. True — сообщение обработано, False — это вопрос."""
    if message.chat.type != 'private' or message.from_user is None or not message.text:
        return False
    user_id = message.from_user.id
    now = utcnow()
    waiting = _pending.get(user_id)
    if waiting is not None and waiting[1] <= now:
        del _pending[user_id]
        waiting = None
    if waiting is None and not looks_like_reminder(message.text):
        return False
    if not llm.is_configured():
        _pending.pop(user_id, None)
        if waiting is not None:
            return False
        await message.answer(NO_MODEL_TEXT)
        return True
    request = message.text if waiting is None else f'{waiting[0]}\n{message.text}'
    try:
        profile = await profiles.get(user_id)
        parsed = await parse_request(request, profile, now)
    except NotReminder:
        _pending.pop(user_id, None)
        return False
    except Exception as error:
        _pending.pop(user_id, None)
        log.warning('reminder request parsing failed: %s', type(error).__name__)
        await message.answer(PARSE_FAILED_TEXT)
        return True
    if isinstance(parsed, str):
        _pending[user_id] = (request, now + PENDING_TTL)
        await message.answer(escape(parsed))
        return True
    _pending.pop(user_id, None)
    try:
        draft_id = await reminder_drafts.create(
            user_id, parsed.text, parsed.next_run_at, profile.timezone, now, parsed.rrule)
    except ValueError as error:
        await message.answer(escape(str(error)))
        return True
    except Exception as error:
        log.warning('reminder draft creation failed: %s', type(error).__name__)
        await message.answer('Не удалось сохранить напоминание. Попробуйте ещё раз чуть позже.')
        return True
    await message.answer(
        confirmation(parsed.text, parsed.next_run_at, profile.timezone, parsed.rrule),
        reply_markup=draft_keyboard(draft_id))
    return True


async def on_reminder_callback(callback: CallbackQuery) -> None:
    if callback.message is None or callback.message.chat.type != 'private':
        await callback.answer('Управление доступно в личном чате.', show_alert=True)
        return
    result = 'missing'
    try:
        _, action, key = (callback.data or '').split(':', 2)
        if action == 'off':
            reminder_id = int(key)
            if not 0 < reminder_id < 2**63:
                raise ValueError('invalid id')
            result = await reminder_delivery.disable(reminder_id, callback.from_user.id)
        elif action in {'c', 'x'}:
            result = await reminder_drafts.decide(
                UUID(key), callback.from_user.id, action == 'c', utcnow())
    except (ValueError, TypeError):
        result = 'missing'
    except Exception as error:
        log.warning('reminder callback failed: %s', type(error).__name__)
        await callback.answer('Не удалось сохранить результат. Нажмите кнопку ещё раз позже.', show_alert=True)
        return
    # A callback response also works for inaccessible/deleted Telegram messages.
    await callback.answer(RESULTS[result], show_alert=True)
    if result in {'confirmed', 'cancelled', 'expired', 'disabled', 'missing', 'deleted'}:
        if isinstance(callback.message, Message):
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except TelegramBadRequest:
                pass  # Old/already edited message; the durable result is authoritative.
