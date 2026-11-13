"""Знакомство при /start и команда /profile.

Три коротких вопроса кнопками, каждый можно пропустить, как и всё знакомство целиком.
Состояния в памяти нет: каждая кнопка сама знает свой шаг, поэтому перезапуск бота ничего не ломает.
"""

import logging
from html import escape

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app import events
from app.assistant.core import business_summary
from app.models import UserProfile
from app.recs import service as recs
from app.storage import profiles

log = logging.getLogger(__name__)

STARTER_COUNT = 3

GREETING = (
    "Здравствуйте! Я помощник «Альфа-Курса» для предпринимателей.\n\n"
    "Отвечаю на вопросы о бизнесе по материалам платформы, подсказываю, что изучить дальше, "
    "и напоминаю о делах и сроках налогов."
)
INTRO = (
    "Чтобы советовать точнее, задам три коротких вопроса. "
    "Любой можно пропустить — я всё равно помогу."
)
QUESTIONS = {
    "stage": "На каком этапе ваш бизнес?",
    "form": "Как оформлен бизнес?",
    "regime": "Какой у вас налоговый режим?",
}
STAGES = {
    "idea": "Только планирую",
    "starting": "Недавно начал",
    "running": "Уже работает",
    "growing": "Расширяюсь",
}
# По чему искать стартовую подборку для каждой стадии.
STAGE_QUERIES = {
    "idea": "как открыть свой бизнес",
    "starting": "первые клиенты для бизнеса",
    "running": "как увеличить продажи",
    "growing": "как масштабировать бизнес",
}
FORMS = {"ip": "ИП", "ooo": "ООО", "self_employed": "Самозанятый"}
REGIMES = {
    "usn_income": "УСН «доходы»",
    "usn_income_expense": "УСН «доходы минус расходы»",
    "ausn": "АУСН",
    "psn": "Патент",
    "osno": "Общая система (ОСНО)",
}
HOW_TO_USE = (
    "Теперь просто напишите вопрос, например: «как выбрать налоговый режим» или "
    "«как нанять первого сотрудника».\n"
    "Напоминание — так: «напомни завтра в 10 сверить кассу» или «напоминай о налогах».\n"
    "Все команды — /help."
)
FORGOTTEN_TEXT = "Готово, я забыл сведения о вашем бизнесе. Напоминания остались на месте."
EMPTY_PROFILE_TEXT = (
    "Я пока ничего не знаю о вашем бизнесе. Расскажите в сообщении или ответьте на три вопроса."
)
FAILED_TEXT = "Не получилось сохранить. Попробуйте ещё раз чуть позже."


def _keyboard(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data) for label, data in row] for row in rows
    ])


def stage_keyboard() -> InlineKeyboardMarkup:
    options = [(label, f"ob:s:{code}") for code, label in STAGES.items()]
    return _keyboard([options[:2], options[2:], [("Пропустить", "ob:skip:stage")],
                      [("Пропустить знакомство", "ob:end")]])


def form_keyboard() -> InlineKeyboardMarkup:
    options = [(label, f"ob:f:{code}") for code, label in FORMS.items()]
    return _keyboard([options, [("Пропустить", "ob:skip:form")]])


def regime_keyboard() -> InlineKeyboardMarkup:
    options = [[(label, f"ob:r:{code}")] for code, label in REGIMES.items()]
    return _keyboard([*options, [("Не знаю или пропустить", "ob:skip:regime")]])


def profile_keyboard(known: bool) -> InlineKeyboardMarkup:
    if not known:
        return _keyboard([[("Ответить на три вопроса", "ob:restart")]])
    return _keyboard([[("Обновить", "ob:restart"), ("Забыть", "ob:reset")]])


def next_step(done: str, profile: UserProfile) -> str | None:
    """Какой вопрос задать после шага done; None — знакомство закончено.

    Тому, кто только планирует бизнес, про оформление и налоги не задаём: отвечать пока нечего.
    Самозанятому режим не нужен — он один.
    """
    if done == "stage":
        return None if profile.business_stage == "idea" else "form"
    if done == "form":
        return None if profile.legal_form == "self_employed" else "regime"
    return None


def _question(step: str) -> tuple[str, InlineKeyboardMarkup]:
    keyboards = {"stage": stage_keyboard, "form": form_keyboard, "regime": regime_keyboard}
    return QUESTIONS[step], keyboards[step]()


def starter_pack(profile: UserProfile) -> str:
    """Стартовая подборка для нового пользователя: популярное, с учётом того, что уже известно."""
    # Если стадия известна, подбираем под неё; когда не вышло или ничего не нашлось — популярное.
    items = []
    query = STAGE_QUERIES.get(profile.business_stage or "", "")
    if query:
        try:
            items = recs.recommend(profile, query, set(), STARTER_COUNT)
        except Exception as error:
            log.warning("stage starter pack failed: %s", type(error).__name__)
    try:
        if not items:
            items = recs.recommend(profile, "", set(), STARTER_COUNT)
    except Exception as error:  # подборка — дополнение к знакомству
        log.warning("starter pack failed: %s", type(error).__name__)
        return ""
    if not items:
        return ""
    lines = []
    for item in items:
        url = events.tracked_url(item.material.url, "starter", item.material.kind)
        mark = " (платный)" if item.material.is_paid else ""
        lines.append(f'• <a href="{escape(url, quote=True)}">{escape(item.material.title)}</a>{mark}')
    return "С чего можно начать:\n" + "\n".join(lines)


def finish_text(profile: UserProfile) -> str:
    known = business_summary(profile)
    parts = [f"Спасибо! Запомнил: {escape(known)}." if known else "Хорошо, обойдёмся без анкеты."]
    pack = starter_pack(profile)
    if pack:
        parts.append(pack)
    parts.append(HOW_TO_USE)
    return "\n\n".join(parts)


def profile_text(profile: UserProfile) -> str:
    known = business_summary(profile)
    if not known:
        return EMPTY_PROFILE_TEXT
    return (f"Вот что я знаю о вашем бизнесе:\n{escape(known)}.\n\n"
            "Эти сведения я беру из ваших сообщений и ответов и использую, чтобы точнее отвечать.")


async def _log(user_id: int, name: str, payload: dict | None = None) -> None:
    try:
        await events.log_event(user_id, name, payload)
    except Exception as error:
        log.warning("event was not logged: %s", type(error).__name__)


async def start(message: Message) -> None:
    """Команда /start: приветствие и первый вопрос, а знакомому пользователю — без анкеты."""
    if message.from_user is None:
        return
    if message.chat.type != "private":
        await message.answer(GREETING)
        return
    user_id = message.from_user.id
    try:
        profile = await profiles.get(user_id)
    except Exception as error:
        log.warning("profile read failed on start: %s", type(error).__name__)
        profile = UserProfile(user_id)
    await _log(user_id, events.BOT_STARTED)
    known = business_summary(profile)
    if known:
        await message.answer(
            f"{GREETING}\n\nЯ помню ваш бизнес: {escape(known)}.\n\n{HOW_TO_USE}",
            reply_markup=profile_keyboard(True), disable_web_page_preview=True)
        return
    await message.answer(f"{GREETING}\n\n{INTRO}")
    text, keyboard = _question("stage")
    await message.answer(text, reply_markup=keyboard)


async def show_profile(message: Message) -> None:
    """Команда /profile: что бот знает о бизнесе, с кнопками «Обновить» и «Забыть»."""
    if message.from_user is None:
        return
    if message.chat.type != "private":
        await message.answer("Сведения о бизнесе я показываю только в личном чате.")
        return
    try:
        profile = await profiles.get(message.from_user.id)
    except Exception as error:
        log.warning("profile read failed: %s", type(error).__name__)
        await message.answer(FAILED_TEXT)
        return
    await message.answer(
        profile_text(profile), reply_markup=profile_keyboard(bool(business_summary(profile))))


async def on_callback(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    action, _, value = (callback.data or "")[3:].partition(":")
    keyboard: InlineKeyboardMarkup | None = None
    try:
        if action == "s" and value in STAGES:
            profile = await profiles.update(user_id, {"business_stage": value})
            step = next_step("stage", profile)
        elif action == "f" and value in FORMS:
            changes = {"legal_form": value}
            if value == "self_employed":
                changes["tax_regime"] = "npd"  # у самозанятого режим один
            profile = await profiles.update(user_id, changes)
            step = next_step("form", profile)
        elif action == "r" and value in REGIMES:
            profile = await profiles.update(user_id, {"tax_regime": value})
            step = None
        elif action == "skip" and value in QUESTIONS:
            profile = await profiles.get(user_id)
            # Пропуск одного вопроса не отменяет следующие: мы просто не узнали ответ.
            step = {"stage": "form", "form": "regime", "regime": None}[value]
        elif action == "end":
            profile = await profiles.get(user_id)
            step = None
        elif action == "restart":
            text, keyboard = _question("stage")
            await callback.answer()
            await _edit(callback, text, keyboard)
            return
        elif action == "reset":
            await profiles.reset(user_id)
            await _log(user_id, events.PROFILE_RESET)
            await callback.answer()
            await _edit(callback, FORGOTTEN_TEXT, profile_keyboard(False))
            return
        else:
            await callback.answer("Эта кнопка устарела. Начните заново: /start")
            return
    except Exception as error:
        log.warning("onboarding callback failed: %s", type(error).__name__)
        await callback.answer(FAILED_TEXT, show_alert=True)
        return
    if action in {"s", "f", "r"}:
        await _log(user_id, events.PROFILE_UPDATED, {"fields": sorted(_changed(action))})
    await callback.answer()
    if step is None:
        await _edit(callback, finish_text(profile), None)
    else:
        text, keyboard = _question(step)
        await _edit(callback, text, keyboard)


def _changed(action: str) -> list[str]:
    return {"s": ["business_stage"], "f": ["legal_form"], "r": ["tax_regime"]}[action]


async def _edit(callback: CallbackQuery, text: str, keyboard: InlineKeyboardMarkup | None) -> None:
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(
                text, reply_markup=keyboard, disable_web_page_preview=True)
        except TelegramBadRequest:
            pass  # сообщение уже такое же или слишком старое — ответ всё равно сохранён
