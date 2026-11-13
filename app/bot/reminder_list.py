"""Список напоминаний пользователя: посмотреть, сменить частоту, отключить."""

import logging
import re
from datetime import datetime, timezone
from html import escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.models import Reminder
from app.reminders import service
from app.reminders.content import WEEKDAY_LABELS, repeat_label
from app.storage import profiles, reminder_delivery, reminders

log = logging.getLogger(__name__)

MAX_SHOWN = 8  # больше не помещается в удобную клавиатуру
MAX_LINE = 70
WEEKDAY_CODES = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
FREQUENCIES = {
    "once": "Один раз",
    "daily": "Каждый день",
    "weekly": "Каждую неделю",
    "monthly": "Каждый месяц",
}

EMPTY_TEXT = (
    "Активных напоминаний у вас нет.\n\n"
    "Чтобы поставить, напишите, например: «напомни завтра в 10 сверить кассу» "
    "или «напоминай о налогах»."
)
FAILED_TEXT = "Не получилось сохранить. Попробуйте ещё раз чуть позже."
GONE_TEXT = "Этого напоминания уже нет."
PRIVATE_ONLY_TEXT = "Список напоминаний доступен в личном чате со мной."
_LIST_REQUEST = re.compile(
    r"(?:мои|список|покажи|какие|все)\s+(?:мои\s+|у меня\s+)?напоминани", re.I
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def is_list_request(text: str) -> bool:
    return bool(_LIST_REQUEST.search(text))


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("Europe/Moscow")


def _short(text: str) -> str:
    first = text.strip().split("\n", 1)[0]
    return first if len(first) <= MAX_LINE else first[: MAX_LINE - 1].rstrip() + "…"


def _when(reminder: Reminder, zone: ZoneInfo) -> str:
    local = reminder.next_run_at.astimezone(zone)
    return f"{local:%d.%m.%Y %H:%M}, {WEEKDAY_LABELS[local.weekday()]}"


def rule_for(frequency: str, reminder: Reminder, zone: ZoneInfo) -> str | None:
    """Правило повтора, которое начинается с уже назначенного времени напоминания."""
    local = reminder.next_run_at.astimezone(zone)
    if frequency == "daily":
        return "FREQ=DAILY"
    if frequency == "weekly":
        return f"FREQ=WEEKLY;BYDAY={WEEKDAY_CODES[local.weekday()]}"
    if frequency == "monthly":
        return f"FREQ=MONTHLY;BYMONTHDAY={local.day}"
    return None


def render(items: list[Reminder], zone_name: str, note: str = "") -> tuple[str, InlineKeyboardMarkup | None]:
    """Текст списка и клавиатура. Налоговые показываются одной строкой: их бывает больше двадцати."""
    zone = _zone(zone_name)
    active = [item for item in items if item.active]
    own = [item for item in active if item.kind != "tax"]
    tax = [item for item in active if item.kind == "tax"]
    if not active:
        return (f"{escape(note)}\n\n{EMPTY_TEXT}" if note else EMPTY_TEXT), None

    lines = [escape(note), ""] if note else []
    rows: list[list[InlineKeyboardButton]] = []
    if own:
        lines.append("<b>Ваши напоминания</b>")
        for number, item in enumerate(own[:MAX_SHOWN], start=1):
            repeat = repeat_label(item.rrule) or "один раз"
            lines.append(f"{number}. {escape(_short(item.text))}\n    {_when(item, zone)} · {repeat}")
            rows.append([
                InlineKeyboardButton(text=f"{number}. Частота", callback_data=f"rm:f:{item.id}"),
                InlineKeyboardButton(text=f"{number}. Отключить", callback_data=f"rm:off:{item.id}"),
            ])
        if len(own) > MAX_SHOWN:
            lines.append(f"…и ещё {len(own) - MAX_SHOWN}. Они появятся здесь, когда список станет короче.")
    if tax:
        if own:
            lines.append("")
        nearest = min(tax, key=lambda item: item.next_run_at)
        lines.append(f"<b>Налоговые напоминания</b>: {len(tax)}, ближайшее — {_when(nearest, zone)}.")
        rows.append([InlineKeyboardButton(text="Отключить налоговые", callback_data="tx:off")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def frequency_keyboard(reminder_id: int) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text=label, callback_data=f"rm:s:{reminder_id}:{code}")
        for code, label in FREQUENCIES.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=[
        buttons[:2], buttons[2:], [InlineKeyboardButton(text="Назад к списку", callback_data="rm:list")],
    ])


async def _list(user_id: int, note: str = "") -> tuple[str, InlineKeyboardMarkup | None]:
    profile = await profiles.get(user_id)
    return render(await service.list_for(user_id), profile.timezone, note)


async def show(message: Message) -> None:
    """Команда /reminders и просьба «покажи мои напоминания»."""
    if message.chat.type != "private":
        await message.answer(PRIVATE_ONLY_TEXT)
        return
    if message.from_user is None:
        return
    try:
        text, keyboard = await _list(message.from_user.id)
    except Exception as error:
        log.warning("reminder list failed: %s", type(error).__name__)
        await message.answer(FAILED_TEXT)
        return
    await message.answer(text, reply_markup=keyboard)


async def on_free_text(message: Message) -> bool:
    """«Покажи мои напоминания» обычной речью. True — сообщение обработано."""
    if message.chat.type != "private" or not message.text or not is_list_request(message.text):
        return False
    await show(message)
    return True


async def _owned(reminder_id: int, user_id: int) -> Reminder | None:
    """Напоминание, только если оно существует, активно и принадлежит пользователю."""
    if not 0 < reminder_id < 2**63:
        return None
    reminder = await reminders.get(reminder_id)
    if reminder is None or reminder.user_id != user_id or not reminder.active:
        return None
    return reminder


async def on_callback(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    alert = ""
    try:
        reminder_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        if action == "list":
            text, keyboard = await _list(user_id)
        elif action == "f":
            reminder = await _owned(reminder_id, user_id)
            if reminder is None:
                alert = GONE_TEXT
                text, keyboard = await _list(user_id)
            else:
                text = (f"Как часто напоминать?\n\n<b>{escape(_short(reminder.text))}</b>\n"
                        f"Сейчас: {repeat_label(reminder.rrule) or 'один раз'}.")
                keyboard = frequency_keyboard(reminder.id)
        elif action == "s" and len(parts) == 4 and parts[3] in FREQUENCIES:
            reminder = await _owned(reminder_id, user_id)
            if reminder is None:
                alert = GONE_TEXT
                text, keyboard = await _list(user_id)
            else:
                profile = await profiles.get(user_id)
                rule = rule_for(parts[3], reminder, _zone(profile.timezone))
                await service.set_schedule(reminder.id, rule, reminder.next_run_at)
                text, keyboard = await _list(
                    user_id, f"Готово: теперь «{_short(reminder.text)}» — "
                             f"{FREQUENCIES[parts[3]].lower()}.")
        elif action == "off":
            reminder = await _owned(reminder_id, user_id)
            if reminder is None:
                alert = GONE_TEXT
                text, keyboard = await _list(user_id)
            else:
                await reminder_delivery.disable(reminder.id, user_id)
                text, keyboard = await _list(user_id, f"Отключил: «{_short(reminder.text)}».")
        else:
            await callback.answer("Эта кнопка устарела. Откройте список заново: /reminders")
            return
    except Exception as error:
        log.warning("reminder list callback failed: %s", type(error).__name__)
        await callback.answer(FAILED_TEXT, show_alert=True)
        return
    await callback.answer(alert or None)
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(text, reply_markup=keyboard)
        except TelegramBadRequest:
            pass  # сообщение уже такое же или слишком старое — результат всё равно сохранён
