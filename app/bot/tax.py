"""Диалог про налоговые напоминания: уточняем режим, показываем план, создаём по кнопке."""

import logging
import re
from datetime import datetime, timezone
from html import escape

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.models import UserProfile
from app.reminders import tax
from app.reminders.parse import looks_like_reminder
from app.storage import profiles

log = logging.getLogger(__name__)

_TAX_WORDS = re.compile(
    r"налог|взнос|деклараци|отч[её]т|усн|упрощ[её]нк|патент|ндс|ндфл|аусн|нпд|самозанят", re.I
)
_HAS_DIGITS = re.compile(r"\d")
PREVIEW_COUNT = 4

ASK_REGIME = "Чтобы подобрать сроки, скажите, какой у вас налоговый режим."
ASK_FORM = "У вас ИП или ООО? От этого зависят сроки."
NOTHING_TEXT = (
    "В моём календаре сейчас нет ближайших сроков для вашего режима. "
    "Могу напомнить о чём-то своём — просто напишите, о чём и когда."
)
FAILED_TEXT = "Не получилось сохранить. Попробуйте ещё раз чуть позже."
PRIVATE_ONLY_TEXT = "Налоговые напоминания настраиваются в личном чате со мной."


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def is_tax_request(text: str) -> bool:
    """«Напоминай о налогах» — да; «напомни 28 ноября в 10 оплатить налог» — нет, там свой срок."""
    return bool(looks_like_reminder(text) and _TAX_WORDS.search(text) and not _HAS_DIGITS.search(text))


def _rows(pairs: list[tuple[str, str]], width: int = 1) -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(text=label, callback_data=data) for label, data in pairs]
    return InlineKeyboardMarkup(
        inline_keyboard=[buttons[i : i + width] for i in range(0, len(buttons), width)]
    )


def regime_keyboard() -> InlineKeyboardMarkup:
    labels = {
        "usn_income": "УСН «доходы»",
        "usn_income_expense": "УСН «доходы минус расходы»",
        "ausn": "АУСН",
        "psn": "Патент",
        "npd": "Самозанятый (НПД)",
        "osno": "Общая система (ОСНО)",
    }
    return _rows([(label, f"tx:r:{regime}") for regime, label in labels.items()])


def form_keyboard() -> InlineKeyboardMarkup:
    return _rows([("ИП", "tx:f:ip"), ("ООО", "tx:f:ooo")], width=2)


def plan_keyboard() -> InlineKeyboardMarkup:
    return _rows(
        [("Подтвердить", "tx:ok"), ("Другой режим", "tx:chg"), ("Отмена", "tx:no")], width=2
    )


def off_keyboard() -> InlineKeyboardMarkup:
    return _rows([("Отключить налоговые напоминания", "tx:off")])


def _who(profile: UserProfile) -> str:
    form = tax.FORM_LABELS.get(tax.effective_form(profile) or "", "")
    regime = tax.REGIME_LABELS.get(profile.tax_regime or "", "")
    return regime if form == "самозанятый" else f"{form}, {regime}"


def plan_text(profile: UserProfile, now: datetime) -> str | None:
    """Что бот собирается поставить; None — ставить нечего."""
    planned = tax.plan(profile, now)
    if not planned:
        return None
    lines = [
        f"Ваш режим: {escape(_who(profile))}.",
        f"Поставлю напоминаний: {len(planned)}, до {planned[-1][0].due:%d.%m.%Y}. Каждое придёт "
        f"за {tax.DAYS_BEFORE} дня до срока, в {tax.REMIND_HOUR}:00 по вашему времени.",
        "",
        "Ближайшие сроки:",
    ]
    lines += [f"• {due.due:%d.%m.%Y} — {escape(due.title)}" for due, _ in planned[:PREVIEW_COUNT]]
    if len(planned) > PREVIEW_COUNT:
        lines.append(f"…и ещё {len(planned) - PREVIEW_COUNT}.")
    if tax.has_relative_events(profile):
        lines += ["", "Срок оплаты патента зависит от дат вашего патента — о нём напомню, если "
                      "напишете, например: «напомни 20 декабря в 10 оплатить патент»."]
    checked = tax.load_calendar()["checked_on"]
    lines += ["", f"Сроки сверены с сайтом ФНС {checked:%d.%m.%Y}. Перед оплатой проверьте дату: "
                  "её могут перенести."]
    return "\n".join(lines)


def _step(profile: UserProfile, now: datetime) -> tuple[str, InlineKeyboardMarkup | None]:
    need = tax.missing(profile)
    if need == "regime":
        return ASK_REGIME, regime_keyboard()
    if need == "legal_form":
        return ASK_FORM, form_keyboard()
    text = plan_text(profile, now)
    if text is None:
        return NOTHING_TEXT, None
    return text, plan_keyboard()


async def start(message: Message) -> None:
    """Начать настройку: спросить, чего не хватает в профиле, или сразу показать план."""
    if message.chat.type != "private":
        await message.answer(PRIVATE_ONLY_TEXT)
        return
    if message.from_user is None:
        return
    try:
        profile = await profiles.get(message.from_user.id)
    except Exception as error:
        log.warning("tax flow start failed: %s", type(error).__name__)
        await message.answer(FAILED_TEXT)
        return
    text, keyboard = _step(profile, utcnow())
    await message.answer(text, reply_markup=keyboard, disable_web_page_preview=True)


async def on_free_text(message: Message) -> bool:
    """Просьба напоминать о налогах обычной речью. True — сообщение обработано."""
    if message.chat.type != "private" or not message.text or not is_tax_request(message.text):
        return False
    await start(message)
    return True


async def _show(callback: CallbackQuery, text: str, keyboard: InlineKeyboardMarkup | None) -> None:
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(
                text, reply_markup=keyboard, disable_web_page_preview=True
            )
        except TelegramBadRequest:
            pass  # сообщение уже такое же или слишком старое — результат всё равно сохранён


async def on_callback(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    action, _, value = (callback.data or "")[3:].partition(":")
    now = utcnow()
    try:
        if action == "r" and value in tax.REGIME_LABELS:
            changes = {"tax_regime": value}
            profile = await profiles.get(user_id)
            # Форма, несовместимая с новым режимом, сбрасывается: спросим заново или подставим.
            if value in tax.FORM_BY_REGIME:
                changes["legal_form"] = tax.FORM_BY_REGIME[value]
            elif profile.legal_form == "self_employed":
                changes["legal_form"] = None
            profile = await profiles.update(user_id, changes)
            text, keyboard = _step(profile, now)
        elif action == "f" and value in ("ip", "ooo"):
            profile = await profiles.update(user_id, {"legal_form": value})
            text, keyboard = _step(profile, now)
        elif action == "chg":
            text, keyboard = ASK_REGIME, regime_keyboard()
        elif action == "no":
            text, keyboard = "Хорошо, налоговые напоминания не ставлю.", None
        elif action == "ok":
            profile = await profiles.get(user_id)
            if tax.missing(profile) is not None:
                text, keyboard = _step(profile, now)
            else:
                created = await tax.subscribe(profile, now)
                if created:
                    text = (f"Готово, поставил напоминаний: {created}. Под каждым будет кнопка "
                            "«Отключить», а все сразу можно отключить здесь.")
                else:
                    text = "Эти напоминания у вас уже стоят — новых не добавил."
                keyboard = off_keyboard()
        elif action == "off":
            disabled = await tax.unsubscribe(user_id)
            text = (f"Отключил налоговых напоминаний: {disabled}." if disabled
                    else "Активных налоговых напоминаний у вас нет.")
            keyboard = None
        else:
            await callback.answer("Эта кнопка устарела. Напишите: «напоминай о налогах».")
            return
    except Exception as error:
        log.warning("tax callback failed: %s", type(error).__name__)
        await callback.answer(FAILED_TEXT, show_alert=True)
        return
    await callback.answer()
    await _show(callback, text, keyboard)
