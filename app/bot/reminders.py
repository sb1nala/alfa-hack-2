"""Explicit reminder commands and owner-checked durable callback buttons."""

import logging
from datetime import datetime, timezone
from html import escape
from uuid import UUID

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.reminders.command import parse_command
from app.reminders.content import confirmation
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


def disable_keyboard(reminder_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='Отключить', callback_data=f'rd:off:{reminder_id}')]])


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


async def on_reminder_callback(callback: CallbackQuery) -> None:
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
