"""Обработчики сообщений. Роль B."""

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from app.assistant import core
from app.bot.render import render_answer

router = Router()

START_TEXT = (
    "Здравствуйте! Я помощник «Альфа-Курса» для предпринимателей.\n\n"
    "Задайте вопрос о бизнесе — отвечу по материалам платформы и подскажу, что изучить дальше."
)
HELP_TEXT = "Напишите вопрос обычным текстом.\n\n/start — начать заново\n/help — эта подсказка"


@router.message(CommandStart())
async def on_start(message: Message) -> None:
    await message.answer(START_TEXT)


@router.message(Command("help"))
async def on_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(F.text)
async def on_text(message: Message) -> None:
    if message.from_user is None or message.text is None:
        return
    answer = await core.reply(message.from_user.id, message.text)
    await message.answer(render_answer(answer), disable_web_page_preview=True)
