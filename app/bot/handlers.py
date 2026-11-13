"""Обработчики сообщений. Роль B."""

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove

from app.assistant import core
from app.bot.render import render_answer
from app.bot import onboarding, reminder_list
from app.bot import profile as profile_ui
from app.bot import reminders as reminder_ui
from app.bot import tax as tax_ui
from app.bot.reminders import on_free_text, on_reminder_callback
from app.models import Answer

router = Router()

HELP_TEXT = (
    "Я помощник «Альфа-Курса»: отвечаю на вопросы о бизнесе по материалам платформы.\n"
    "Можно пользоваться ботом без анкеты — просто напишите свой вопрос.\n\n"
    "/start — знакомство и необязательная настройка\n"
    "/profile — посмотреть, изменить или удалить профиль\n"
    "/help — эта подсказка\n"
    "/cancel — прекратить текущий ввод без удаления сохранённых данных\n"
    "Эти команды, /remind, /reminders и /taxes прекращают текущий ввод профиля.\n"
    "Чтобы поставить напоминание, просто попросите: «напомни завтра в 10 сверить кассу» "
    "или «напоминай каждый понедельник в 9:30 проверять заказы».\n"
    "Можно и точной командой: /remind 2027-01-15 10:00 Проверить план\n"
    "Время — в часовом поясе из /profile, по умолчанию Europe/Moscow (Москва). "
    "Смена пояса применяется к новым напоминаниям; существующие расписания и черновики сохраняются. "
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
    if message.from_user:
        reminder_ui.cancel_pending(message.from_user.id)
    await profile_ui.open_screen(message, 'start')


@router.message(Command('profile'))
async def on_profile(message: Message) -> None:
    if message.from_user:
        reminder_ui.cancel_pending(message.from_user.id)
    await profile_ui.open_screen(message, 'profile')


async def stop_input(message: Message) -> bool:
    """All explicit commands terminate UI input; a DB failure leaves it retryable."""
    if message.from_user is None or message.chat.type != 'private':
        return True
    reminder_ui.cancel_pending(message.from_user.id)
    try:
        await profile_ui.storage.cancel(message.from_user.id)
    except Exception:
        await message.answer(profile_ui.FAILED_TEXT + ' Повторите команду.')
        return False
    return True


@router.message(Command("help"))
async def on_help(message: Message) -> None:
    if not await stop_input(message):
        return
    await message.answer(HELP_TEXT)


@router.message(Command('cancel'))
async def on_cancel(message: Message) -> None:
    if await stop_input(message):
        await message.answer('Текущий ввод прекращён. Сохранённые данные остались. Задайте вопрос о бизнесе.')


@router.message(Command('remind'))
async def on_remind(message: Message) -> None:
    if await stop_input(message):
        await reminder_ui.on_remind(message)


@router.message(Command('taxes'))
async def on_taxes(message: Message) -> None:
    if await stop_input(message):
        await tax_ui.start(message)


@router.message(Command('reminders'))
async def on_reminders(message: Message) -> None:
    if await stop_input(message):
        await reminder_list.show(message)


router.callback_query.register(profile_ui.on_callback, F.data.startswith('pf:'))
router.callback_query.register(on_reminder_callback, F.data.startswith('rd:'))
router.callback_query.register(tax_ui.on_callback, F.data.startswith('tx:'))
router.callback_query.register(reminder_list.on_callback, F.data.startswith('rm:'))
router.callback_query.register(onboarding.on_callback, F.data.startswith('ob:'))


@router.callback_query()
async def on_unknown_callback(callback: CallbackQuery) -> None:
    await callback.answer('Эта кнопка больше не поддерживается. Откройте /help.', show_alert=True)


@router.message(F.text.startswith('/'))
async def on_unknown_command(message: Message) -> None:
    if await stop_input(message):
        await message.answer('Неизвестная команда. Доступные команды — /help.')


@router.message(F.text)
async def on_text(message: Message) -> None:
    if message.from_user is None or message.text is None:
        return
    if await profile_ui.on_text(message):
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
