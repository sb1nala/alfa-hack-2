"""Список напоминаний: вид, смена частоты и отключение с проверкой владельца."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import Update

from app.bot import handlers
from app.bot import reminder_list as ui
from app.models import Answer, Reminder, UserProfile
from app.reminders import service
from app.reminders.schedule import canonicalize
from app.storage import profiles, reminder_delivery, reminders
from tests.test_reminder_commands import FakeSession

MSK = ZoneInfo('Europe/Moscow')
# Понедельник, 16 ноября 2026, 09:30 по Москве.
RUN = datetime(2026, 11, 16, 6, 30, tzinfo=timezone.utc)
USER = 8_200_000_001


def reminder(id_, text='Сверить кассу', kind='custom', rrule=None, active=True, run=RUN, user=101):
    return Reminder(id_, user, kind, text, run, rrule, active)


def labels(keyboard):
    return [[button.text for button in row] for row in keyboard.inline_keyboard]


def data(keyboard):
    return [button.callback_data for row in keyboard.inline_keyboard for button in row]


# --- вид списка ---


def test_empty_list_explains_how_to_create():
    text, keyboard = ui.render([], 'Europe/Moscow')
    assert text == ui.EMPTY_TEXT and keyboard is None
    text, keyboard = ui.render([reminder(1, active=False)], 'Europe/Moscow')
    assert text == ui.EMPTY_TEXT and keyboard is None


def test_own_reminders_are_numbered_with_local_time_repeat_and_buttons():
    weekly = canonicalize('FREQ=WEEKLY;BYDAY=MO', RUN, 'Europe/Moscow')
    text, keyboard = ui.render(
        [reminder(7), reminder(9, 'Проверять <заказы>', rrule=weekly)], 'Europe/Moscow')
    assert '1. Сверить кассу\n    16.11.2026 09:30, понедельник · один раз' in text
    assert '2. Проверять &lt;заказы&gt;\n    16.11.2026 09:30, понедельник · каждую неделю' in text
    assert labels(keyboard) == [['1. Частота', '1. Отключить'], ['2. Частота', '2. Отключить']]
    assert data(keyboard) == ['rm:f:7', 'rm:off:7', 'rm:f:9', 'rm:off:9']


def test_time_is_shown_in_the_user_zone():
    text, _ = ui.render([reminder(1)], 'Asia/Vladivostok')
    assert '16.11.2026 16:30, понедельник' in text
    assert '16.11.2026 09:30' in ui.render([reminder(1)], 'Mars/Olympus')[0]


def test_tax_reminders_are_one_line_with_the_nearest_date():
    items = [reminder(n, f'Налог {n}', kind='tax', run=RUN + timedelta(days=30 * n)) for n in (3, 1, 2)]
    text, keyboard = ui.render([reminder(50), *items], 'Europe/Moscow')
    assert '<b>Налоговые напоминания</b>: 3, ближайшее — 16.12.2026 09:30' in text
    assert 'Налог 1' not in text
    assert labels(keyboard)[-1] == ['Отключить налоговые'] and data(keyboard)[-1] == 'tx:off'


def test_long_list_and_long_text_are_shortened():
    items = [reminder(n, 'Очень длинное дело ' * 10 + '\nвторая строка') for n in range(1, 12)]
    text, keyboard = ui.render(items, 'Europe/Moscow')
    assert len(keyboard.inline_keyboard) == ui.MAX_SHOWN
    assert '…и ещё 3.' in text and 'вторая строка' not in text
    assert all(len(line) <= ui.MAX_LINE + 4 for line in text.split('\n') if line[:1].isdigit())


def test_note_is_shown_above_the_list_and_alone_when_nothing_is_left():
    text, _ = ui.render([reminder(1)], 'Europe/Moscow', 'Отключил: «Сверить <кассу>».')
    assert text.startswith('Отключил: «Сверить &lt;кассу&gt;».\n\n<b>Ваши напоминания</b>')
    text, keyboard = ui.render([], 'Europe/Moscow', 'Отключил: «Дело».')
    assert text.startswith('Отключил: «Дело».') and ui.EMPTY_TEXT in text and keyboard is None


@pytest.mark.parametrize(('frequency', 'rule'), [
    ('once', None), ('daily', 'FREQ=DAILY'), ('weekly', 'FREQ=WEEKLY;BYDAY=MO'),
    ('monthly', 'FREQ=MONTHLY;BYMONTHDAY=16'),
])
def test_rule_starts_from_the_time_already_set(frequency, rule):
    assert ui.rule_for(frequency, reminder(1), MSK) == rule
    if rule:  # расписание принимает правило: назначенное время — его событие
        assert canonicalize(rule, RUN, 'Europe/Moscow')


def test_weekday_and_day_follow_the_user_zone():
    late = reminder(1, run=datetime(2026, 11, 16, 20, 0, tzinfo=timezone.utc))  # во Владивостоке уже вторник
    assert ui.rule_for('weekly', late, ZoneInfo('Asia/Vladivostok')) == 'FREQ=WEEKLY;BYDAY=TU'
    assert ui.rule_for('monthly', late, ZoneInfo('Asia/Vladivostok')) == 'FREQ=MONTHLY;BYMONTHDAY=17'


@pytest.mark.parametrize(('text', 'expected'), [
    ('покажи мои напоминания', True), ('Мои напоминания', True), ('какие у меня напоминания', True),
    ('список напоминаний', True), ('напомни завтра в 10 сверить кассу', False),
    ('напоминай о налогах', False), ('что такое франшиза', False),
])
def test_list_request_detection(text, expected):
    assert ui.is_list_request(text) is expected


# --- диалог ---


@pytest.fixture
async def chat(monkeypatch):
    session = FakeSession()
    bot = Bot('123456:fake-test-token', session=session)
    dispatcher = Dispatcher()
    dispatcher.message.handlers = handlers.router.message.handlers.copy()
    dispatcher.callback_query.handlers = handlers.router.callback_query.handlers.copy()
    store = {7: reminder(7), 9: reminder(9, 'Чужое дело', user=202)}

    async def get(reminder_id):
        return store.get(reminder_id)

    async def list_for(user_id):
        return [item for item in store.values() if item.user_id == user_id]

    async def set_schedule(reminder_id, rrule, next_run_at):
        store[reminder_id] = reminder(reminder_id, store[reminder_id].text, rrule=rrule)
        return store[reminder_id]

    async def disable(reminder_id, user_id):
        store[reminder_id] = reminder(reminder_id, store[reminder_id].text, active=False)
        return 'disabled'

    reply = AsyncMock(return_value=Answer('no_answer', 'Ответ'))
    monkeypatch.setattr(handlers.core, 'reply', reply)
    monkeypatch.setattr(profiles, 'get', AsyncMock(return_value=UserProfile(101)))
    monkeypatch.setattr(reminders, 'get', get)
    monkeypatch.setattr(service, 'list_for', list_for)
    monkeypatch.setattr(service, 'set_schedule', AsyncMock(side_effect=set_schedule))
    monkeypatch.setattr(reminder_delivery, 'disable', AsyncMock(side_effect=disable))

    async def feed(text=None, *, data=None, group=False):
        message = {'message_id': 1, 'date': 1,
                   'chat': {'id': 101, 'type': 'group' if group else 'private'}}
        user = {'id': 101, 'is_bot': False, 'first_name': 'Тест'}
        if data is None:
            update = {'update_id': 1, 'message': {**message, 'from': user, 'text': text}}
        else:
            update = {'update_id': 1, 'callback_query': {
                'id': 'cb', 'from': user, 'chat_instance': 'test', 'data': data,
                'message': {**message, 'from': {'id': 1, 'is_bot': True, 'first_name': 'Бот'},
                            'text': 'старый текст'}}}
        session.calls.clear()
        await dispatcher.feed_update(bot, Update.model_validate(update))
        return list(session.calls)

    yield feed, store, reply
    await bot.session.close()


def edited(calls):
    return next(call for call in calls if isinstance(call, EditMessageText))


def answered(calls):
    return next(call for call in calls if isinstance(call, AnswerCallbackQuery))


async def test_command_and_plain_words_show_only_own_reminders(chat):
    feed, _, reply = chat
    for request in ('/reminders', 'покажи мои напоминания'):
        calls = await feed(request)
        assert isinstance(calls[-1], SendMessage)
        assert 'Сверить кассу' in calls[-1].text and 'Чужое дело' not in calls[-1].text
    reply.assert_not_awaited()


async def test_changing_frequency_goes_through_a_choice_and_back_to_the_list(chat):
    feed, store, _ = chat
    choice = edited(await feed(data='rm:f:7'))
    assert 'Как часто напоминать?' in choice.text and 'Сейчас: один раз.' in choice.text
    assert labels(choice.reply_markup) == [
        ['Один раз', 'Каждый день'], ['Каждую неделю', 'Каждый месяц'], ['Назад к списку']]
    done = edited(await feed(data='rm:s:7:weekly'))
    service.set_schedule.assert_awaited_once_with(7, 'FREQ=WEEKLY;BYDAY=MO', RUN)
    assert done.text.startswith('Готово: теперь «Сверить кассу» — каждую неделю.')
    assert store[7].rrule == 'FREQ=WEEKLY;BYDAY=MO'
    back = edited(await feed(data='rm:s:7:once'))
    assert store[7].rrule is None and '— один раз.' in back.text


async def test_disable_removes_the_reminder_from_the_list(chat):
    feed, store, _ = chat
    done = edited(await feed(data='rm:off:7'))
    reminder_delivery.disable.assert_awaited_once_with(7, 101)
    assert done.text.startswith('Отключил: «Сверить кассу».') and ui.EMPTY_TEXT in done.text
    assert store[7].active is False


@pytest.mark.parametrize('data', ['rm:f:9', 'rm:s:9:daily', 'rm:off:9', 'rm:off:404', 'rm:f:0'])
async def test_foreign_or_missing_reminder_is_never_touched(chat, data):
    feed, store, _ = chat
    calls = await feed(data=data)
    assert answered(calls).text == ui.GONE_TEXT
    assert 'Чужое дело' not in edited(calls).text
    service.set_schedule.assert_not_awaited()
    reminder_delivery.disable.assert_not_awaited()
    assert store[9].active and store[9].rrule is None


@pytest.mark.parametrize('data', ['rm:', 'rm:zzz', 'rm:s:7:hourly', 'rm:s:7', 'rm:off:abc'])
async def test_broken_buttons_are_answered_and_change_nothing(chat, data):
    feed, store, _ = chat
    calls = await feed(data=data)
    assert isinstance(calls[-1], (AnswerCallbackQuery, EditMessageText))
    service.set_schedule.assert_not_awaited()
    reminder_delivery.disable.assert_not_awaited()
    assert store[7].active and store[7].rrule is None


async def test_storage_failure_is_reported_without_details(chat, monkeypatch):
    feed, *_ = chat
    monkeypatch.setattr(service, 'set_schedule', AsyncMock(side_effect=RuntimeError('secret dsn')))
    calls = await feed(data='rm:s:7:daily')
    assert answered(calls).text == ui.FAILED_TEXT and 'secret' not in answered(calls).text


async def test_group_chat_gets_no_list(chat):
    feed, _, reply = chat
    assert (await feed('/reminders', group=True))[-1].text == ui.PRIVATE_ONLY_TEXT
    await feed('покажи мои напоминания', group=True)
    reply.assert_awaited_once()


# --- настоящая база ---


async def test_frequency_change_is_saved_with_the_user_zone_and_keeps_the_time(storage_db):
    await profiles.update(USER, {'timezone': 'Asia/Vladivostok'})
    saved = await service.create(Reminder(0, USER, 'custom', 'Вымышленная сверка', RUN, None))
    rule = ui.rule_for('weekly', saved, ZoneInfo('Asia/Vladivostok'))
    changed = await service.set_schedule(saved.id, rule, saved.next_run_at)
    assert changed.next_run_at == RUN
    # Расписание хранит поля правила в алфавитном порядке.
    assert changed.rrule == 'DTSTART;TZID=Asia/Vladivostok:20261116T163000\nRRULE:BYDAY=MO;FREQ=WEEKLY'
    assert (await service.set_schedule(saved.id, None, saved.next_run_at)).rrule is None
    assert await ui._owned(saved.id, USER) is not None
    assert await ui._owned(saved.id, USER + 1) is None
    await reminder_delivery.disable(saved.id, USER)
    assert await ui._owned(saved.id, USER) is None
