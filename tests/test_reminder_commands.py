"""No-network command, rendering and aiogram routing tests."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, EditMessageReplyMarkup, SendMessage
from aiogram.types import Update

from app.bot import handlers
from app.bot import reminders as ui
from app.models import Answer, UserProfile
from app.reminders.command import USAGE, parse_command
from app.reminders.content import checked_text, confirmation, delivery, units
from app.storage import profiles, reminder_delivery, reminder_drafts

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize('zone,expected', [('Europe/Moscow', 7), ('Asia/Vladivostok', 0), ('UTC', 10)])
def test_local_to_utc(zone, expected):
    text, instant = parse_command('/remind 2026-01-02 10:00  Проверить <план>', zone, NOW)
    assert text == 'Проверить <план>'
    assert instant == datetime(2026, 1, 2, expected, tzinfo=timezone.utc)


@pytest.mark.parametrize('text', [
    '/remind', '/remind завтра план', '/remind 2026-1-02 10:00 план',
    '/remind 2026-01-02 1:00 план', '/remind 2026-02-30 10:00 план',
    '/remind 2026-01-02 25:00 план', '/remind 2026-01-02 10:00',
])
def test_format_errors_have_example(text):
    with pytest.raises(ValueError, match='/remind 2027-01-15 10:00'):
        parse_command(text, 'Europe/Moscow', NOW)


@pytest.mark.parametrize('text', ['/remind 2025-01-02 10:00 план', '/remind 2026-01-01 03:00 план'])
def test_past_and_equal_now(text):
    with pytest.raises(ValueError, match='будущем'):
        parse_command(text, 'Europe/Moscow', NOW)


@pytest.mark.parametrize('local', ['2026-03-29 02:30', '2026-10-25 02:30'])
def test_dst_gap_and_fold(local):
    with pytest.raises(ValueError, match='Выберите другое время'):
        parse_command(f'/remind {local} план', 'Europe/Berlin', NOW)


@pytest.mark.parametrize('body', [' ', '\x00', 'x' * 3401, '&' * 681, '😀' * 1701])
def test_invalid_or_long_text(body):
    with pytest.raises(ValueError):
        checked_text(body)


def test_html_escaped_and_telegram_limit_reserved():
    body = '<b>План</b> & "идея"'
    assert '&lt;b&gt;План&lt;/b&gt;' in delivery(body)
    for body in ['x' * 3400, '&' * 680, '😀' * 1700]:
        assert units(confirmation(body, NOW, 'Europe/Moscow')) <= 4096
        assert units(delivery(body)) <= 4096
    assert '01.01.2026 03:00, московское время' in confirmation('план', NOW, 'Europe/Moscow')
    assert '01.01.2026 10:00, владивостокское время' in confirmation('план', NOW, 'Asia/Vladivostok')
    assert 'часовой пояс UTC' in confirmation('план', NOW, 'UTC')
    assert 'Europe/' not in USAGE and 'профил' not in USAGE


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, (AnswerCallbackQuery, EditMessageReplyMarkup)):
            return True
        return SimpleNamespace(message_id=1)

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        yield b''


@pytest.fixture
async def dispatch(monkeypatch):
    session = FakeSession()
    bot = Bot('123456:fake-test-token', session=session)
    dispatcher = Dispatcher()
    # A detached router shares registered observers; never re-parent the module router.
    dispatcher.message.handlers = handlers.router.message.handlers.copy()
    dispatcher.callback_query.handlers = handlers.router.callback_query.handlers.copy()
    reply = AsyncMock(return_value=Answer('no_answer', 'Ответ'))
    monkeypatch.setattr(handlers.core, 'reply', reply)
    monkeypatch.setattr(ui, 'utcnow', lambda: NOW)
    monkeypatch.setattr(profiles, 'get', AsyncMock(return_value=UserProfile(101)))
    monkeypatch.setattr(reminder_drafts, 'create', AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(reminder_drafts, 'decide', AsyncMock(return_value='confirmed'))
    monkeypatch.setattr(reminder_delivery, 'disable', AsyncMock(return_value='disabled'))

    async def feed(text=None, *, data=None, group=False, inaccessible=False):
        message = {'message_id': 1, 'date': 0 if inaccessible else 1,
                   'chat': {'id': 101, 'type': 'group' if group else 'private'}}
        user = {'id': 101, 'is_bot': False, 'first_name': 'Тест'}
        if data is None:
            update = {'update_id': 1, 'message': {**message, 'from': user, 'text': text}}
        else:
            update = {'update_id': 1, 'callback_query': {
                'id': 'test-callback', 'from': user, 'chat_instance': 'test',
                'message': message, 'data': data}}
        await dispatcher.feed_update(bot, Update.model_validate(update))
        return session.calls

    yield feed, reply
    await bot.session.close()


async def test_remind_bypasses_assistant_and_creates_only_draft(dispatch):
    feed, reply = dispatch
    calls = await feed('/remind 2026-01-02 10:00 Проверить <b>план</b>')
    reply.assert_not_awaited()
    reminder_drafts.create.assert_awaited_once()
    args = reminder_drafts.create.await_args.args
    assert args[2] == datetime(2026, 1, 2, 7, tzinfo=timezone.utc)
    assert args[3] == 'Europe/Moscow'
    assert '&lt;b&gt;план&lt;/b&gt;' in calls[-1].text
    buttons = calls[-1].reply_markup.inline_keyboard[0]
    assert [b.text for b in buttons] == ['Подтвердить', 'Отмена']
    assert all(len(b.callback_data.encode()) <= 64 for b in buttons)


async def test_bad_command_is_not_a_question(dispatch):
    feed, reply = dispatch
    calls = await feed('/remind неверно')
    reply.assert_not_awaited()
    reminder_drafts.create.assert_not_awaited()
    assert '/remind 2027-01-15 10:00' in calls[-1].text


async def test_group_command(dispatch):
    feed, reply = dispatch
    calls = await feed('/remind 2026-01-02 10:00 план', group=True)
    assert 'личном чате' in calls[-1].text
    profiles.get.assert_not_awaited()
    reminder_drafts.create.assert_not_awaited()
    reply.assert_not_awaited()


@pytest.mark.parametrize('data', [f'rd:c:{uuid4().hex}', f'rd:x:{uuid4().hex}', 'rd:off:123',
                                'rd:c:broken', 'rd:off:-1', 'rd:off:9223372036854775808', 'rd:old:key'])
async def test_callbacks_acknowledged_without_assistant(dispatch, data):
    feed, reply = dispatch
    calls = await feed(data=data)
    reply.assert_not_awaited()
    assert len([c for c in calls if isinstance(c, AnswerCallbackQuery)]) == 1


@pytest.mark.parametrize('result', ['confirmed', 'cancelled', 'expired', 'forbidden', 'missing'])
async def test_old_or_foreign_buttons(dispatch, result):
    feed, reply = dispatch
    reminder_drafts.decide.return_value = result
    calls = await feed(data=f'rd:c:{uuid4().hex}', inaccessible=True)
    assert calls[-1].text == ui.RESULTS[result]
    assert isinstance(calls[-1], AnswerCallbackQuery)
    assert reminder_drafts.decide.await_args.args[1] == 101
    reply.assert_not_awaited()


async def test_disable_checks_owner_result(dispatch):
    feed, _ = dispatch
    reminder_delivery.disable.return_value = 'forbidden'
    calls = await feed(data='rd:off:123')
    reminder_delivery.disable.assert_awaited_once_with(123, 101)
    assert calls[-1].text == ui.RESULTS['forbidden']
    assert not any(isinstance(c, EditMessageReplyMarkup) for c in calls)


async def test_callback_database_error_acknowledges_and_keeps_button(dispatch):
    feed, _ = dispatch
    reminder_drafts.decide.side_effect = RuntimeError('secret')
    calls = await feed(data=f'rd:c:{uuid4().hex}')
    assert isinstance(calls[-1], AnswerCallbackQuery)
    assert 'ещё раз' in calls[-1].text and 'secret' not in calls[-1].text


async def test_existing_start_help_and_question(dispatch):
    feed, reply = dispatch
    await feed('/start')
    reply.assert_not_awaited()
    calls = await feed('/help')
    assert '/remind' in calls[-1].text
    reply.assert_not_awaited()
    await feed('Вопрос о плане')
    reply.assert_awaited_once_with(101, 'Вопрос о плане')
