"""Обработчики сообщений. Роль B."""

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove

from app.assistant import core
from app.bot.render import render_answer
from app.bot.reminders import on_remind, on_reminder_callback
from app.models import Answer

router = Router()

START_TEXT = (
    "Здравствуйте! Я помощник «Альфа-Курса» для предпринимателей.\n\n"
    "Задайте вопрос о бизнесе — отвечу по материалам платформы и подскажу, что изучить дальше."
)
HELP_TEXT = (
    "Напишите вопрос обычным текстом.\n\n/start — начать заново\n/help — эта подсказка\n"
    "/remind ГГГГ-ММ-ДД ЧЧ:ММ Текст — разовое напоминание в личном чате, "
    "по времени профиля (без профиля — Europe/Moscow), после подтверждения."
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


@router.message(F.text)
async def on_text(message: Message) -> None:
    if message.from_user is None or message.text is None:
        return
    answer = await core.reply(message.from_user.id, message.text)
    await message.answer(
        render_answer(answer), disable_web_page_preview=True, reply_markup=options_keyboard(answer)
    )
