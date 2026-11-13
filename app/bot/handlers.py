"""Обработчики сообщений. Роль B."""

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove

from app.assistant import core
from app.bot.render import render_answer
from app.bot import reminder_list
from app.bot import tax as tax_ui
from app.bot.reminders import on_free_text, on_remind, on_reminder_callback
from app.models import Answer

router = Router()

START_TEXT = (
    "Здравствуйте! Я помощник «Альфа-Курса» для предпринимателей.\n\n"
    "Задайте вопрос о бизнесе — отвечу по материалам платформы и подскажу, что изучить дальше."
)
HELP_TEXT = (
    "Напишите вопрос обычным текстом.\n\n/start — начать заново\n/help — эта подсказка\n"
    "Чтобы поставить напоминание, просто попросите: «напомни завтра в 10 сверить кассу» "
    "или «напоминай каждый понедельник в 9:30 проверять заказы».\n"
    "Можно и точной командой: /remind 2027-01-15 10:00 Проверить план\n"
    "Время — ваше местное; если я не знаю ваш город, считаю по московскому. "
    "Перед сохранением попрошу подтвердить.\n"
    "/taxes — напоминания о сроках налогов и взносов по вашему режиму; "
    "можно и словами: «напоминай о налогах».\n"
    "/reminders — ваши напоминания: посмотреть, сменить частоту или отключить."
)


def options_keyboard(answer: Answer) -> ReplyKeyboardMarkup | ReplyKeyboardRemove:
    """Варианты ответа на уточняющий вопрос — кнопками; нажатие отправляет текст варианта."""
    if not answer.options:
        return ReplyKeyboardRemove()
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=option)] for option in answer.options],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


@router.message(CommandStart())
async def on_start(message: Message) -> None:
    await message.answer(START_TEXT)


@router.message(Command("help"))
async def on_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


router.message.register(on_remind, Command('remind'))
router.callback_query.register(on_reminder_callback, F.data.startswith('rd:'))
router.message.register(tax_ui.start, Command('taxes'))
router.callback_query.register(tax_ui.on_callback, F.data.startswith('tx:'))
router.message.register(reminder_list.show, Command('reminders'))
router.callback_query.register(reminder_list.on_callback, F.data.startswith('rm:'))


@router.message(F.text)
async def on_text(message: Message) -> None:
    if message.from_user is None or message.text is None:
        return
    # «Покажи мои напоминания» — до разбора «напомни…»: слова похожи, а смысл другой.
    if await reminder_list.on_free_text(message):
        return
    # Сначала налоговые напоминания: им не нужна языковая модель, сроки берутся из календаря.
    if await tax_ui.on_free_text(message):
        return
    if await on_free_text(message):
        return
    answer = await core.reply(message.from_user.id, message.text)
    await message.answer(
        render_answer(answer), disable_web_page_preview=True, reply_markup=options_keyboard(answer)
    )
