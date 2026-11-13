"""No-network aiogram routing tests with a substituted Telegram transport."""

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import Update

from app import events
from app.bot import handlers
from app.bot import profile as ui
from app.bot import reminders as reminder_ui
from app.models import Answer, UserProfile
from app.storage import profile_ui as storage
from tests.test_reminder_commands import FakeSession

NOW = datetime(2026, 11, 13, 9, tzinfo=timezone.utc)
USER = 101


def state(mode='menu', field=None, *, text=False):
    return storage.Session(USER, uuid4(), 0, mode, field, text, 'offered', NOW + storage.TTL)


def callback(session, action):
    return f'pf:{session.token.hex}:{session.revision}:{action}'


@pytest.fixture
async def chat(monkeypatch):
    transport = FakeSession()
    bot = Bot('123456:fake-test-token', session=transport)
    dispatcher = Dispatcher()
    dispatcher.message.handlers = handlers.router.message.handlers.copy()
    dispatcher.callback_query.handlers = handlers.router.callback_query.handlers.copy()
    mocks = SimpleNamespace(
        current=AsyncMock(return_value=None), lookup=AsyncMock(return_value=None),
        open_menu=AsyncMock(return_value=state('offer')), cancel=AsyncMock(),
        transition=AsyncMock(), get=AsyncMock(return_value=UserProfile(USER)),
        metric=AsyncMock(), reply=AsyncMock(return_value=Answer('no_answer', 'Ответ')),
        draft=AsyncMock(return_value=uuid4()),
    )
    for name in ('current', 'lookup', 'open_menu', 'cancel', 'transition'):
        monkeypatch.setattr(storage, name, getattr(mocks, name))
    monkeypatch.setattr(ui.service, 'get', mocks.get)
    monkeypatch.setattr(events, 'log_event', mocks.metric)
    monkeypatch.setattr(handlers.core, 'reply', mocks.reply)
    monkeypatch.setattr(ui, 'utcnow', lambda: NOW)
    monkeypatch.setattr(reminder_ui, 'utcnow', lambda: NOW)
    monkeypatch.setattr(reminder_ui.profiles, 'get', mocks.get)
    monkeypatch.setattr(reminder_ui.reminder_drafts, 'create', mocks.draft)
    reminder_ui.reset_pending()

    async def feed(text=None, *, data=None, owner=USER, group=False, inaccessible=False):
        start = len(transport.calls)
        message = {'message_id': 1, 'date': 0 if inaccessible else 1,
                   'chat': {'id': USER, 'type': 'group' if group else 'private'}}
        user = {'id': owner, 'is_bot': False, 'first_name': 'Вымышленный пользователь'}
        if data is None:
            update = {'update_id': 1, 'message': {**message, 'from': user, 'text': text}}
        else:
            update = {'update_id': 1, 'callback_query': {
                'id': 'profile-test', 'from': user, 'chat_instance': 'test', 'message': message, 'data': data}}
        await dispatcher.feed_update(bot, Update.model_validate(update))
        return transport.calls[start:]

    yield feed, mocks
    await bot.session.close()


async def choose(feed, mocks, before, action, after, *, fields=(), reset=False):
    mocks.lookup.return_value = before
    mocks.transition.return_value = storage.Result('ok', after, fields, reset)
    return await feed(data=callback(before, action))


async def test_first_start_and_all_three_questions(chat):
    feed, m = chat
    offer = state('offer')
    m.open_menu.return_value = offer
    calls = await feed('/start')
    assert 'три коротких вопроса' in calls[-1].text
    assert 'Europe/Moscow' in calls[-1].text
    assert 'Пропустить всю анкету' in str(calls[-1].reply_markup)
    first = replace(offer, revision=1, mode='onboarding', field='business_stage')
    calls = await choose(feed, m, offer, 'begin', first)
    assert 'Вопрос 1 из 3' in calls[-1].text
    assert [r[0].text for r in calls[-1].reply_markup.inline_keyboard[:4]] == [
        'Идея', 'Начинаю', 'Уже работаю', 'Развиваю']
    second = replace(first, revision=2, field='legal_form')
    calls = await choose(feed, m, first, 's2', second, fields=('business_stage',))
    assert m.transition.await_args.kwargs['value'] == 'running'
    assert 'Вопрос 2 из 3' in calls[-1].text and 'Пока не указана' in str(calls[-1].reply_markup)
    third = replace(second, revision=3, field='timezone')
    await choose(feed, m, second, 's0', third, fields=('legal_form',))
    assert m.transition.await_args.kwargs['value'] == 'ip'
    done = replace(third, revision=4, mode='idle', field=None, onboarding_status='completed')
    calls = await choose(feed, m, third, 's3', done, fields=('timezone',))
    assert m.transition.await_args.kwargs['value'] == 'Asia/Vladivostok'
    assert ui.ZONE_NOTE in calls[-1].text and 'задайте вопрос' in calls[-1].text
    m.reply.assert_not_awaited()
    m.draft.assert_not_awaited()
    assert m.metric.await_args_list[0].args == (USER, events.BOT_STARTED, None)
    updates = [c.args for c in m.metric.await_args_list if c.args[1] == events.PROFILE_UPDATED]
    assert updates == [(USER, events.PROFILE_UPDATED, {'fields': [f]})
                       for f in ('business_stage', 'legal_form', 'timezone')]


@pytest.mark.parametrize('action', ['skip', 'all'])
async def test_skip_question_or_whole_form(chat, action):
    feed, m = chat
    first = state('onboarding', 'business_stage')
    after = replace(first, revision=1, field='legal_form') if action == 'skip' else state('idle')
    calls = await choose(feed, m, first, action, after)
    assert m.transition.await_args.args[3] == action
    assert not m.transition.await_args.kwargs['value']
    m.metric.assert_not_awaited()
    m.reply.assert_not_awaited()
    if action == 'all':
        assert 'Europe/Moscow' in calls[-1].text and 'задайте вопрос' in calls[-1].text


async def test_repeated_start_does_not_impose_questions(chat):
    feed, m = chat
    m.open_menu.return_value = state('home')
    calls = await feed('/start')
    assert 'С возвращением' in calls[-1].text and '/profile' in calls[-1].text
    assert 'Повторить настройку' in str(calls[-1].reply_markup)
    m.transition.assert_not_awaited()


@pytest.mark.parametrize('full', [False, True])
async def test_profile_read_labels_html_and_missing_values(chat, full):
    feed, m = chat
    m.open_menu.return_value = state()
    if full:
        m.get.return_value = UserProfile(USER, 'growing', '<b>Мастерская</b>', 'ooo',
                                        'usn_income_expense', 'experienced', 'Найти <клиентов>', 'Asia/Vladivostok')
    calls = await feed('/profile')
    text = calls[-1].text
    assert all(label in text for label in ui.LABELS.values())
    if full:
        assert 'Развиваю' in text and 'С опытом' in text and 'УСН «доходы минус расходы»' in text
        assert '&lt;b&gt;Мастерская&lt;/b&gt;' in text and '&lt;клиентов&gt;' in text
        assert 'growing' not in text and 'experienced' not in text and 'usn_income_expense' not in text
    else:
        assert text.count('Не указано') == 6 and 'Europe/Moscow' in text
    m.get.assert_awaited_once_with(USER)
    m.transition.assert_not_awaited()
    m.reply.assert_not_awaited()


@pytest.mark.parametrize('field', storage.FIELDS)
async def test_each_field_has_edit_screen_and_only_selected_field_is_saved(chat, field):
    feed, m = chat
    menu = state()
    editing = replace(menu, revision=1, mode='edit', field=field,
                      expects_text=field in {'industry', 'current_goal'})
    calls = await choose(feed, m, menu, f'f{storage.FIELDS.index(field)}', editing)
    assert m.transition.await_args.kwargs['field'] == field
    assert ui.LABELS[field] in calls[-1].text
    buttons = str(calls[-1].reply_markup)
    assert ('Очистить поле' in buttons) == (field != 'timezone')
    after = replace(editing, revision=2, mode='menu', field=None, expects_text=False)
    if editing.expects_text:
        m.current.return_value = editing
        m.transition.return_value = storage.Result('ok', after, (field,))
        calls = await feed('<Новая задача>')
        assert m.transition.await_args.kwargs['value'] == '<Новая задача>'
    else:
        calls = await choose(feed, m, editing, 's0', after, fields=(field,))
        assert m.transition.await_args.kwargs['value'] == ui.CHOICES[field][0][0]
    assert m.transition.await_args.kwargs['field'] == field
    assert 'Сохранено.' in calls[-1].text
    m.reply.assert_not_awaited()


@pytest.mark.parametrize('field', storage.FIELDS[:-1])
async def test_clear_every_nullable_field(chat, field):
    feed, m = chat
    editing = state('edit', field, text=field in {'industry', 'current_goal'})
    await choose(feed, m, editing, 'clear', state(), fields=(field,))
    assert m.transition.await_args.kwargs['value'] is None
    assert m.transition.await_args.kwargs['field'] == field
    m.reply.assert_not_awaited()


async def test_custom_zone_prompts_explicitly_and_invalid_zone_can_be_retried(chat):
    feed, m = chat
    editing = state('edit', 'timezone')
    custom = replace(editing, revision=1, expects_text=True)
    calls = await choose(feed, m, editing, 'other', custom)
    assert 'Введите IANA-зону' in calls[-1].text
    m.current.return_value = custom
    m.transition.side_effect = ValueError('timezone must be an existing IANA zone')
    calls = await feed('Mars/Olympus')
    assert 'Некорректная IANA-зона' in calls[-1].text
    m.metric.assert_not_awaited()
    m.transition.side_effect = None
    m.transition.return_value = storage.Result('ok', state(), ('timezone',))
    calls = await feed('Europe/Berlin')
    assert ui.ZONE_NOTE in calls[-1].text
    m.reply.assert_not_awaited()


@pytest.mark.parametrize('value', [' ' * 5, 'x' * 301, '\x00'])
async def test_invalid_or_long_text_is_not_saved(chat, value):
    feed, m = chat
    m.current.return_value = state('edit', 'industry', text=True)
    calls = await feed(value)
    assert 'до 300 символов' in calls[-1].text
    m.transition.assert_not_awaited()
    m.reply.assert_not_awaited()


async def test_render_legacy_long_html_stays_below_telegram_limit():
    text = ui.render(UserProfile(USER, industry='&😀<"' * 10000, current_goal='😀>' * 10000,
                                 timezone='&' * 10000))
    assert len(text.encode('utf-16-le')) // 2 < 4096
    assert '<"' not in text and '&amp;' in text and '…' in text


@pytest.mark.parametrize('command', ['/start', '/profile', '/help', '/cancel',
                                     '/remind 2026-11-14 10:00 План', '/unknown'])
async def test_commands_terminate_input_without_saving_command_as_field(chat, command):
    feed, m = chat
    m.current.return_value = state('edit', 'current_goal', text=True)
    calls = await feed(command)
    if command in {'/start', '/profile'}:
        m.open_menu.assert_awaited_once()
    else:
        m.cancel.assert_awaited_once_with(USER)
    m.transition.assert_not_awaited()
    m.reply.assert_not_awaited()
    if command.startswith('/remind '):
        m.draft.assert_awaited_once()
    if command == '/help':
        assert 'без анкеты' in calls[-1].text and '/cancel' in calls[-1].text
        assert 'тихие часы' not in calls[-1].text


async def test_buttons_allow_normal_dialog_but_typed_option_is_service_input(chat):
    feed, m = chat
    m.current.return_value = state('onboarding', 'business_stage')
    await feed('Как зарегистрировать бизнес?')
    m.reply.assert_awaited_once_with(USER, 'Как зарегистрировать бизнес?')
    m.reply.reset_mock()
    await feed('Идея')
    m.reply.assert_not_awaited()
    m.transition.assert_not_awaited()


@pytest.mark.parametrize('status', ['stale', 'expired', 'invalid'])
async def test_old_repeated_expired_callbacks_are_acknowledged(chat, status):
    feed, m = chat
    editing = state('edit', 'experience')
    m.lookup.return_value = editing
    m.transition.return_value = storage.Result(status)
    calls = await feed(data=callback(editing, 's0'))
    assert len(calls) == 1 and isinstance(calls[0], AnswerCallbackQuery)
    assert calls[0].text == ui.STATUS_TEXT[status]
    m.reply.assert_not_awaited()
    m.metric.assert_not_awaited()


async def test_foreign_callback_never_writes(chat):
    feed, m = chat
    editing = state('edit', 'industry', text=True)
    m.lookup.return_value = editing
    calls = await feed(data=callback(editing, 'clear'), owner=202)
    assert calls[0].text == ui.STATUS_TEXT['forbidden']
    m.transition.assert_not_awaited()
    m.get.assert_not_awaited()


@pytest.mark.parametrize('data', ['pf:broken', 'pf:abc:0:s0', 'pf:a:b:c:d'])
async def test_malformed_callbacks_are_answered(chat, data):
    feed, m = chat
    calls = await feed(data=data)
    assert len(calls) == 1 and isinstance(calls[0], AnswerCallbackQuery)
    m.reply.assert_not_awaited()


@pytest.mark.parametrize('command', ['/start', '/profile'])
async def test_groups_never_show_profile(chat, command):
    feed, m = chat
    calls = await feed(command, group=True)
    assert 'личном чате' in calls[-1].text
    m.get.assert_not_awaited()
    m.open_menu.assert_not_awaited()


async def test_group_and_inaccessible_profile_callback(chat):
    feed, m = chat
    for kw in ({'group': True}, {'inaccessible': True}):
        calls = await feed(data=callback(state(), 'delete'), **kw)
        assert len(calls) == 1 and isinstance(calls[0], AnswerCallbackQuery)
    m.transition.assert_not_awaited()


async def test_delete_requires_confirmation_and_cancel_has_no_reset_metric(chat):
    feed, m = chat
    menu = state()
    deleting = replace(menu, revision=1, mode='delete')
    calls = await choose(feed, m, menu, 'delete', deleting)
    assert 'Напоминания' in calls[-1].text and 'Europe/Moscow' in calls[-1].text
    assert [b.text for b in calls[-1].reply_markup.inline_keyboard[0]] == ['Удалить', 'Отмена']
    m.metric.assert_not_awaited()
    await choose(feed, m, deleting, 'back', replace(menu, revision=2))
    m.metric.assert_not_awaited()
    calls = await choose(feed, m, deleting, 'yes', state(), reset=True)
    assert 'Профиль удалён' in calls[-1].text
    m.metric.assert_awaited_once_with(USER, events.PROFILE_RESET, None)
    m.reply.assert_not_awaited()


async def test_failed_write_keeps_retry_possible_and_does_not_emit_success(chat):
    feed, m = chat
    editing = state('edit', 'legal_form')
    m.lookup.return_value = editing
    m.transition.side_effect = RuntimeError('private database details')
    calls = await feed(data=callback(editing, 's1'))
    assert 'ещё раз' in calls[0].text and 'private database' not in calls[0].text
    m.metric.assert_not_awaited()
    assert not any(isinstance(c, SendMessage) for c in calls)
    m.transition.side_effect = None
    await choose(feed, m, editing, 's1', state(), fields=('legal_form',))
    m.metric.assert_awaited_once_with(USER, events.PROFILE_UPDATED, {'fields': ['legal_form']})


async def test_metrics_failure_does_not_undo_success(chat):
    feed, m = chat
    m.metric.side_effect = RuntimeError('unavailable')
    editing = state('edit', 'legal_form')
    calls = await choose(feed, m, editing, 's0', state(), fields=('legal_form',))
    assert 'Сохранено' in calls[-1].text
    m.transition.assert_awaited_once()


async def test_expired_text_is_not_sent_to_assistant(chat):
    feed, m = chat
    expired = replace(state('edit', 'industry', text=True), expires_at=NOW)
    m.current.return_value = expired
    calls = await feed('Мастерская')
    assert 'истекло' in calls[-1].text
    m.cancel.assert_awaited_once_with(USER, token=expired.token, revision=expired.revision)
    m.transition.assert_not_awaited()
    m.reply.assert_not_awaited()


async def test_input_database_failure_is_retryable_without_success_or_assistant(chat):
    feed, m = chat
    editing = state('edit', 'industry', text=True)
    m.current.return_value = editing
    m.transition.side_effect = RuntimeError('private details')
    calls = await feed('Мастерская')
    assert 'Повторите ввод' in calls[-1].text and 'private details' not in calls[-1].text
    m.metric.assert_not_awaited()
    m.reply.assert_not_awaited()
    m.transition.side_effect = None
    m.transition.return_value = storage.Result('ok', state(), ('industry',))
    calls = await feed('Мастерская')
    assert 'Сохранено.' in calls[-1].text
    m.metric.assert_awaited_once_with(USER, events.PROFILE_UPDATED, {'fields': ['industry']})


async def test_expired_and_old_callbacks_are_checked_before_decoding_field_options(chat):
    feed, m = chat
    s = replace(state('edit', 'industry', text=True), revision=2)
    m.lookup.return_value = s
    calls = await feed(data=f'pf:{s.token.hex}:1:s0')
    assert calls[0].text == ui.STATUS_TEXT['stale']
    m.lookup.return_value = replace(s, expires_at=NOW)
    calls = await feed(data=callback(s, 's0'))
    assert calls[0].text == ui.STATUS_TEXT['expired']
    m.transition.assert_not_awaited()


async def test_unknown_callback_is_acknowledged(chat):
    feed, m = chat
    calls = await feed(data='old:unsupported')
    assert len(calls) == 1 and isinstance(calls[0], AnswerCallbackQuery)
    m.reply.assert_not_awaited()


async def test_post_commit_screen_failure_reports_committed_operation(chat):
    feed, m = chat
    m.current.return_value = state('edit', 'industry', text=True)
    m.transition.return_value = storage.Result('ok', state(), ('industry',))
    m.get.side_effect = RuntimeError('temporarily unavailable')
    calls = await feed('Мастерская')
    assert 'Действие выполнено' in calls[-1].text
    m.metric.assert_awaited_once()


async def test_buttons_fit_telegram_callback_limit_for_all_fields(chat):
    feed, m = chat
    for field in storage.FIELDS:
        s = replace(state('edit', field, text=field in {'industry', 'current_goal'}), revision=2**31 - 1)
        m.open_menu.return_value = s
        calls = await feed('/profile')
        for row in calls[-1].reply_markup.inline_keyboard:
            assert all(len(button.callback_data.encode()) <= 64 for button in row)


async def test_start_storage_failure_keeps_greeting_without_unstored_buttons(chat):
    feed, m = chat
    m.open_menu.side_effect = RuntimeError('private details')
    calls = await feed('/start')
    assert ui.START_TEXT in calls[-1].text and ui.FAILED_TEXT in calls[-1].text
    assert calls[-1].reply_markup is None
    assert 'private details' not in calls[-1].text
    m.transition.assert_not_awaited()
    m.reply.assert_not_awaited()
