"""Список напоминаний пользователя: посмотреть, сменить частоту, отключить."""

import logging
import re
from datetime import datetime, timezone
from html import escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from uuid import UUID
from app import events
from app.storage import reminder_controls as controls_store
from app.models import Reminder
from app.reminders.content import WEEKDAY_LABELS, repeat_label
from app.storage import profiles

log = logging.getLogger(__name__)

MAX_SHOWN = 8  # больше не помещается в удобную клавиатуру
MAX_LINE = 70
EMPTY_TEXT = (
    "Напоминаний у вас нет.\n\n"
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


def _describe(rule: str | None) -> str:
    if not rule:
        return "один раз"
    fields = dict(
        x.split("=") for x in rule.splitlines()[-1].removeprefix("RRULE:").split(";")
    )
    text = repeat_label(rule) or "повтор"
    interval = int(fields.get("INTERVAL", "1"))
    if interval > 1:
        unit = {
            "DAILY": "дней",
            "WEEKLY": "недель",
            "MONTHLY": "месяцев",
            "YEARLY": "лет",
        }.get(fields.get("FREQ"), "периодов")
        text = f"каждые {interval} {unit} (интервал {interval})"
    if "BYDAY" in fields:
        text += ", дни: " + ", ".join(
            {
                "MO": "пн",
                "TU": "вт",
                "WE": "ср",
                "TH": "чт",
                "FR": "пт",
                "SA": "сб",
                "SU": "вс",
            }.get(day, day)
            for day in fields["BYDAY"].split(",")
        )
    if "BYMONTHDAY" in fields:
        text += ", число: " + fields["BYMONTHDAY"]
    if "COUNT" in fields:
        text += ", всего календарных событий: " + fields["COUNT"]
    if "UNTIL" in fields:
        text += ", до " + datetime.strptime(fields["UNTIL"], "%Y%m%dT%H%M%SZ").strftime(
            "%d.%m.%Y %H:%M UTC"
        )
    return text


def describe(rule: str | None) -> str:
    try:
        return _describe(rule)
    except (ValueError, TypeError):
        return "Расписание требует проверки"


def render(
    items: list[Reminder], zone_name: str, note: str = "", page: int = 0, views=None
) -> tuple[str, InlineKeyboardMarkup | None]:
    zone = _zone(zone_name)
    if not items:
        return (escape(note) + "\n\n" if note else "") + EMPTY_TEXT, None
    page = max(0, min(page, (len(items) - 1) // MAX_SHOWN))
    lines = ([escape(note), ""] if note else []) + ["<b>Ваши напоминания</b>"]
    rows = []
    for index, item in enumerate(
        items[page * MAX_SHOWN : (page + 1) * MAX_SHOWN], page * MAX_SHOWN + 1
    ):
        v = views[index - 1] if views else None
        status = v.status if v else ("Активно" if item.active else "Пауза")
        kind = {"custom": "своё", "learning": "учебное", "tax": "налоговое"}[item.kind]
        lines.append(
            f"{index}. {escape(_short(item.text))}\n{kind} · {status}\n{_when(item, zone)} {zone.key} · {escape(describe(item.rrule))}"
        )
        if v and v.retry_at and v.retry_at > item.next_run_at:
            lines.append(
                "Доставка не ранее: "
                + v.retry_at.astimezone(zone).strftime("%d.%m.%Y %H:%M")
                + " "
                + zone.key
            )
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{index}. Открыть", callback_data=f"rm:v:{item.id}"
                )
            ]
        )
    nav = []
    if page:
        nav.append(InlineKeyboardButton(text="←", callback_data=f"rm:list:{page - 1}"))
    if (page + 1) * MAX_SHOWN < len(items):
        nav.append(InlineKeyboardButton(text="→", callback_data=f"rm:list:{page + 1}"))
    if nav:
        rows.append(nav)
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def _list(user_id: int, note: str = "", page: int = 0):
    profile = await profiles.get(user_id)
    views = await controls_store.list_for(user_id)
    return render([v.reminder for v in views], profile.timezone, note, page, views)


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
    if (
        message.chat.type != "private"
        or not message.text
        or not is_list_request(message.text)
    ):
        return False
    await show(message)
    return True


async def on_callback(callback: CallbackQuery) -> None:
    if (
        not isinstance(callback.message, Message)
        or callback.message.chat.type != "private"
    ):
        await callback.answer(PRIVATE_ONLY_TEXT, show_alert=True)
        return
    user = callback.from_user.id
    parts = (callback.data or "").split(":")
    alert = ""
    try:
        if len(parts) in {2, 3} and parts[1] == "list":
            text, keyboard = await _list(
                user, page=int(parts[2]) if len(parts) == 3 else 0
            )
        elif len(parts) in {3, 4} and parts[1] in {"v", "less", "f"}:
            id_ = int(parts[2])
            v = next(
                (
                    v
                    for v in await controls_store.list_for(user)
                    if v.reminder.id == id_
                ),
                None,
            )
            if v is None:
                raise ValueError(GONE_TEXT)
            r = v.reminder
            if parts[1] == "f":
                if len(parts) != 4 or parts[3] != v.revision:
                    raise ValueError("Кнопка устарела. Откройте /reminders.")
                if r.kind == "tax":
                    raise ValueError(
                        "Эта запись не поддерживает смену частоты: /reminders или /taxes."
                    )
                text = "Выберите интервал в единицах текущего правила. Перед сохранением покажу результат."
                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=str(n),
                                callback_data=f"rm:p:{r.id}:{v.revision}:i{n}",
                            )
                            for n in (1, 2, 3, 7)
                        ],
                        [
                            InlineKeyboardButton(
                                text=str(n),
                                callback_data=f"rm:p:{r.id}:{v.revision}:i{n}",
                            )
                            for n in (14, 30, 100)
                        ],
                    ]
                )
                keyboard.inline_keyboard.extend(
                    [
                        [
                            InlineKeyboardButton(
                                text=label,
                                callback_data=f"rm:p:{r.id}:{v.revision}:{code}",
                            )
                        ]
                        for code, label in [
                            ("daily", "Каждый день"),
                            ("weekly", "Каждую неделю"),
                            ("monthly", "Каждый месяц"),
                            ("once", "Заменить разовым (убрать COUNT/UNTIL)"),
                        ]
                    ]
                )
            elif parts[1] == "less":
                if (
                    len(parts) != 4
                    or await controls_store.button_revision(user, id_, UUID(parts[3]))
                    != v.revision
                ):
                    raise ValueError("Кнопка устарела. Откройте /reminders.")
                token, rule, run = await controls_store.propose(
                    user, r.id, v.revision, "less", utcnow()
                )
                text, keyboard = proposal(token, "less", rule, run)
            else:
                zone = _zone((await profiles.get(user)).timezone)
                text = (
                    f"<b>{escape(_short(r.text))}</b>\n{v.status} · "
                    + {"custom": "своё", "learning": "учебное", "tax": "налоговое"}[
                        r.kind
                    ]
                )
                text += f"\n{_when(r, zone)} {zone.key}\n{escape(describe(r.rrule))}"
                if v.retry_at and v.retry_at > r.next_run_at:
                    text += (
                        "\nПеренос: "
                        + v.retry_at.astimezone(zone).strftime("%d.%m.%Y %H:%M")
                        + " "
                        + zone.key
                    )
                rows = []

                def button(label, action):
                    return InlineKeyboardButton(
                        text=label, callback_data=f"rm:p:{r.id}:{v.revision}:{action}"
                    )

                if r.kind != "tax":
                    rows.append(
                        [
                            InlineKeyboardButton(
                                text="Частота",
                                callback_data=f"rm:f:{r.id}:{v.revision}",
                            ),
                            button("Реже ×2", "less"),
                        ]
                    )
                if r.active:
                    rows.append([button("Пауза", "pause")])
                elif v.status != "Завершено":
                    rows.append([button("Возобновить", "resume")])
                    if not r.rrule and r.kind != "tax" and r.next_run_at <= utcnow():
                        text += "\nСрок прошёл. Выберите доставку сейчас или задайте новое время через /remind."
                        rows.append([button("Доставить сейчас", "now")])
                if r.kind == "tax":
                    text += "\nНалоговые даты не меняются. Подписка: /taxes."
                rows.append([button("Удалить…", "delete")])
                rows.append(
                    [InlineKeyboardButton(text="К списку", callback_data="rm:list")]
                )
                keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
        elif len(parts) == 5 and parts[1] == "p":
            token, rule, run = await controls_store.propose(
                user, int(parts[2]), parts[3], parts[4], utcnow()
            )
            text, keyboard = proposal(token, parts[4], rule, run)
        elif len(parts) == 3 and parts[1] == "no":
            await controls_store.cancel(user, UUID(parts[2]))
            text, keyboard = await _list(user, "Изменение отменено.")
        elif len(parts) == 3 and parts[1] == "yes":
            id_, kind, action = await controls_store.apply(
                user, UUID(parts[2]), utcnow()
            )
            try:
                if action == "delete":
                    await events.log_event(
                        user,
                        events.REMINDER_DISABLED,
                        {"reminder_id": id_, "kind": kind},
                    )
                else:
                    await events.log_event(
                        user,
                        events.REMINDER_CHANGED,
                        {
                            "reminder_id": id_,
                            "action": (
                                "less_often"
                                if action.startswith("i")
                                or action in {"daily", "weekly", "monthly", "once"}
                                else {
                                    "less": "less_often",
                                    "pause": "pause",
                                    "resume": "resume",
                                    "now": "resume",
                                }[action]
                            ),
                        },
                    )
            except Exception:
                pass
            text, keyboard = await _list(user, "Готово. Изменение сохранено.")
        else:
            raise ValueError("Кнопка устарела. Откройте /reminders.")
    except (ValueError, OverflowError) as error:
        await callback.answer(
            str(error)
            if re.search("[А-Яа-я]", str(error))
            else "Кнопка устарела. Откройте /reminders.",
            show_alert=True,
        )
        return
    except Exception as error:
        log.warning("reminder control failed: %s", type(error).__name__)
        await callback.answer(FAILED_TEXT, show_alert=True)
        return
    await callback.answer(alert or None)
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest:
        pass


def proposal(token, action, rule, run):
    label = (
        "Изменить интервал"
        if action.startswith("i") or action in {"daily", "weekly", "monthly", "once"}
        else {
            "less": "Изменить частоту",
            "pause": "Поставить на паузу",
            "resume": "Возобновить",
            "now": "Доставить сейчас",
            "delete": "Удалить без возможности восстановления",
        }[action]
    )
    text = f"<b>{label}?</b>"
    if rule and "UNTIL=" in rule and action != "less":
        text += "\nПри смене частоты число событий до конечной даты может измениться. Конечная дата UNTIL сохранится."
    if action == "once":
        text += "\nПовтор и его COUNT/UNTIL будут удалены: останется одно событие."
    if (
        action in {"less", "resume", "now"}
        or action.startswith("i")
        or action in {"daily", "weekly", "monthly", "once"}
    ):
        text += (
            f"\n{escape(describe(rule))}\nСледующий срок: {run:%d.%m.%Y %H:%M:%S} UTC"
        )
    text += "\nДоставка только с 09:00 до 21:00 по текущему профилю, до двух инициативных сообщений за местный день. Возможен перенос."
    return text, InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Подтвердить", callback_data=f"rm:yes:{token.hex}"
                ),
                InlineKeyboardButton(text="Отмена", callback_data=f"rm:no:{token.hex}"),
            ]
        ]
    )
