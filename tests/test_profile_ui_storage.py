"""Real PostgreSQL, isolated schemas; controlled clock, no real Telegram calls."""

import asyncio
import json
from datetime import timedelta

import asyncpg
import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import AnswerCallbackQuery
from aiogram.types import Update
from unittest.mock import AsyncMock

from app import events
from app.bot import handlers
from app.bot import profile as ui
from app.bot import reminders as reminder_ui
from app.models import Answer, Reminder, UserProfile
from app.reminders.schedule import canonicalize
from app.storage import profile_ui as storage, profiles, reminder_drafts, reminders, shown_materials
from app.storage.db import SCHEMA_PATH
from tests.test_profile_ui import NOW, USER, callback
from tests.test_reminder_commands import FakeSession


async def act(s, action, **kwargs):
    return await storage.transition(s.user_id, s.token, s.revision, action, NOW, **kwargs)


async def editing(field, user=USER):
    menu = await storage.open_menu(user, 'profile', NOW)
    return (await act(menu, 'field', field=field)).session


async def test_first_start_skipping_and_repeat_are_independent_of_profile_fields(storage_db):
    pool, _ = storage_db
    # Even a profile already learned from chat does not mean onboarding was offered.
    await profiles.update(USER, {'industry': 'Мастерская', 'timezone': 'Asia/Vladivostok'})
    first = await storage.open_menu(USER, 'start', NOW)
    assert first.mode == 'offer' and first.onboarding_status == 'offered'
    result = await act(first, 'all')
    assert result.session.onboarding_status == 'skipped'
    assert await profiles.get(USER) == UserProfile(USER, industry='Мастерская', timezone='Asia/Vladivostok')
    repeated = await storage.open_menu(USER, 'start', NOW)
    assert repeated.mode == 'home' and repeated.onboarding_status == 'skipped'
    assert (await act(first, 'begin')).status == 'stale'
    assert await pool.fetchval('SELECT count(*) FROM reminders') == 0


async def test_skip_all_can_leave_no_profile_row(storage_db):
    pool, _ = storage_db
    first = await storage.open_menu(USER, 'start', NOW)
    await act(first, 'all')
    assert await pool.fetchval('SELECT count(*) FROM profiles') == 0
    assert await profiles.get(USER) == UserProfile(USER)
    assert (await storage.open_menu(USER, 'start', NOW)).mode == 'home'


async def test_all_three_steps_partial_fill_and_skip_preserve_existing_fields(storage_db):
    await profiles.update(USER, {'legal_form': 'ooo', 'industry': 'Мастерская'})
    s = await storage.open_menu(USER, 'start', NOW)
    s = (await act(s, 'begin')).session
    s = (await act(s, 'save', field='business_stage', value='idea')).session
    assert s.field == 'legal_form' and s.revision == 2
    s = (await act(s, 'skip')).session
    assert s.field == 'timezone'
    s = (await act(s, 'save', field='timezone', value='Asia/Vladivostok')).session
    assert s.mode == 'idle' and s.onboarding_status == 'completed'
    assert await profiles.get(USER) == UserProfile(
        USER, business_stage='idea', industry='Мастерская', legal_form='ooo', timezone='Asia/Vladivostok')


async def test_each_skip_does_not_create_profile_and_repeat_does_not_reoffer(storage_db):
    pool, _ = storage_db
    s = (await act(await storage.open_menu(USER, 'start', NOW), 'begin')).session
    for field in storage.QUESTIONS:
        assert s.field == field
        s = (await act(s, 'skip')).session
    assert s.onboarding_status == 'completed'
    assert await pool.fetchval('SELECT count(*) FROM profiles') == 0
    assert (await storage.open_menu(USER, 'start', NOW)).mode == 'home'


@pytest.mark.parametrize('field,value', [
    ('business_stage', 'growing'), ('legal_form', 'self_employed'), ('tax_regime', 'usn_income_expense'),
    ('experience', 'experienced'), ('industry', '<Мастерская>'), ('current_goal', 'План & развитие'),
    ('timezone', 'Europe/Berlin'),
])
async def test_edit_and_clear_each_type_uses_partial_update(storage_db, field, value):
    original = {'business_stage': 'idea', 'industry': 'Синтетическая отрасль', 'legal_form': 'ip',
                'tax_regime': 'npd', 'experience': 'beginner', 'current_goal': 'Синтетический план',
                'timezone': 'Europe/Moscow'}
    await profiles.update(USER, original)
    s = await editing(field)
    if field == 'timezone':
        s = (await act(s, 'other')).session
    result = await act(s, 'save', field=field, value=value, text_input=s.expects_text)
    assert result.status == 'ok' and result.fields == (field,)
    assert vars(await profiles.get(USER)) == {'user_id': USER, **original, field: value}
    if field != 'timezone':
        s = await editing(field)
        await act(s, 'save', field=field, value=None, text_input=s.expects_text)
        assert vars(await profiles.get(USER)) == {'user_id': USER, **original, field: None}


@pytest.mark.parametrize('field,value', [
    ('business_stage', 'unknown'), ('legal_form', 'other'), ('experience', 'expert'),
    ('tax_regime', 'usn'), ('timezone', 'Mars/Olympus'), ('timezone', None),
    ('industry', 'x' * 301), ('current_goal', '\x00'), ('industry', ''),
])
async def test_storage_validation_does_not_advance_or_write(storage_db, field, value):
    pool, _ = storage_db
    s = await editing(field)
    with pytest.raises(ValueError):
        await act(s, 'save', field=field, value=value, text_input=s.expects_text)
    assert await storage.current(USER) == s
    assert await pool.fetchval('SELECT count(*) FROM profiles') == 0


async def test_continue_text_and_old_buttons_after_new_pool(storage_db):
    pool, open_pool = storage_db
    s = await editing('current_goal')
    await pool.close()
    await open_pool()
    assert await storage.current(USER) == s
    result = await act(s, 'save', field='current_goal', value='Сохранить план', text_input=True)
    assert result.status == 'ok'
    assert (await profiles.get(USER)).current_goal == 'Сохранить план'
    assert (await act(s, 'save', field='current_goal', value='Старый план', text_input=True)).status == 'stale'


async def test_double_click_and_concurrent_different_choices_only_advance_once(storage_db):
    s = (await act(await storage.open_menu(USER, 'start', NOW), 'begin')).session
    results = await asyncio.gather(*[
        act(s, 'save', field='business_stage', value=value) for value in ('idea', 'growing')])
    assert sorted(r.status for r in results) == ['ok', 'stale']
    saved = next(r for r in results if r.status == 'ok')
    assert await storage.current(USER) == saved.session
    assert saved.session.field == 'legal_form' and saved.session.revision == s.revision + 1
    assert (await profiles.get(USER)).business_stage in {'idea', 'growing'}


async def test_concurrent_independent_profile_updates_survive(storage_db):
    s = await editing('legal_form')
    await asyncio.gather(
        act(s, 'save', field='legal_form', value='ip'),
        profiles.update(USER, {'industry': 'Мастерская', 'current_goal': 'Найти клиентов'}))
    assert await profiles.get(USER) == UserProfile(USER, legal_form='ip', industry='Мастерская', current_goal='Найти клиентов')


async def test_foreign_stale_expired_cancelled_and_new_session_do_not_write(storage_db):
    pool, _ = storage_db
    s = await editing('experience')
    assert (await storage.transition(202, s.token, s.revision, 'save', NOW,
                                     field='experience', value='beginner')).status == 'forbidden'
    assert (await storage.transition(USER, s.token, s.revision, 'save', s.expires_at,
                                     field='experience', value='beginner')).status == 'expired'
    assert (await act(s, 'save', field='industry', value='wrong')).status == 'invalid'
    assert await storage.current(USER) == s
    await storage.cancel(USER)
    assert (await act(s, 'save', field='experience', value='beginner')).status == 'stale'
    new = await storage.open_menu(USER, 'start', NOW)
    assert new.token != s.token
    assert (await act(s, 'close')).status == 'stale'
    assert await pool.fetchval('SELECT count(*) FROM profiles') == 0


async def test_profile_and_step_rollback_together_if_state_write_fails(storage_db):
    pool, _ = storage_db
    s = (await act(await storage.open_menu(USER, 'start', NOW), 'begin')).session
    await pool.execute("""
        CREATE FUNCTION reject_ui_update() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'synthetic transition failure'; END $$;
        CREATE TRIGGER reject_ui_update BEFORE UPDATE ON profile_ui_sessions
        FOR EACH ROW EXECUTE FUNCTION reject_ui_update();
    """)
    with pytest.raises(asyncpg.RaiseError, match='synthetic transition failure'):
        await act(s, 'save', field='business_stage', value='starting')
    assert await storage.current(USER) == s
    assert await pool.fetchval('SELECT count(*) FROM profiles') == 0
    await pool.execute('DROP TRIGGER reject_ui_update ON profile_ui_sessions')
    result = await act(s, 'save', field='business_stage', value='starting')
    assert result.status == 'ok' and result.session.field == 'legal_form'
    assert (await profiles.get(USER)).business_stage == 'starting'


async def seed_history():
    await profiles.update(USER, {'legal_form': 'ip', 'timezone': 'Europe/Moscow'})
    run_at = NOW + timedelta(days=2)
    rule = canonicalize('FREQ=DAILY', run_at, 'Europe/Moscow')
    item = await reminders.insert(Reminder(0, USER, 'custom', 'Синтетический план', run_at, rule))
    draft_id = await reminder_drafts.create(USER, 'Синтетический черновик', run_at, 'Europe/Moscow', NOW, rule)
    await events.log_event(USER, events.BOT_STARTED)
    await shown_materials.mark_shown(USER, 'fictional:article:one')
    return item, draft_id


async def snapshot(pool):
    return {table: await pool.fetch(f'SELECT * FROM {table}')
            for table in ('users', 'reminders', 'reminder_drafts', 'shown_materials', 'events')}


async def test_timezone_change_keeps_utc_recurrence_and_shown_draft_unchanged(storage_db):
    pool, _ = storage_db
    await seed_history()
    before = await snapshot(pool)
    s = await editing('timezone')
    result = await act(s, 'save', field='timezone', value='Asia/Vladivostok')
    assert result.fields == ('timezone',)
    assert await snapshot(pool) == before
    assert (await profiles.get(USER)).timezone == 'Asia/Vladivostok'
    # Existing Moscow draft is still confirmed with the originally shown UTC/rule.
    draft = before['reminder_drafts'][0]
    assert await reminder_drafts.decide(draft['id'], USER, True, NOW) == 'confirmed'
    saved = (await reminders.list_for(USER))[-1]
    assert saved.next_run_at == draft['run_at'] and saved.rrule == draft['rrule']


async def test_delete_cancel_confirm_double_click_and_old_confirmation_after_refill(storage_db):
    pool, _ = storage_db
    await seed_history()
    before = await snapshot(pool)
    menu = await storage.open_menu(USER, 'profile', NOW)
    confirm = (await act(menu, 'delete')).session
    assert (await profiles.get(USER)).legal_form == 'ip'
    cancelled = await act(confirm, 'back')
    assert not cancelled.reset and await snapshot(pool) == before
    assert (await act(confirm, 'yes')).status == 'stale'
    confirm = (await act(cancelled.session, 'delete')).session
    deleted = await act(confirm, 'yes')
    assert deleted.reset and deleted.session.token != confirm.token
    assert await profiles.get(USER) == UserProfile(USER)
    assert await snapshot(pool) == before
    assert (await act(confirm, 'yes')).status == 'stale'
    await profiles.update(USER, {'legal_form': 'ooo', 'industry': 'Новое дело'})
    assert (await act(confirm, 'yes')).status == 'stale'
    assert (await profiles.get(USER)).legal_form == 'ooo'
    s = (await act(deleted.session, 'begin')).session
    assert s.mode == 'onboarding'


async def test_reset_also_rolls_back_if_transition_fails(storage_db):
    pool, _ = storage_db
    await profiles.update(USER, {'industry': 'Мастерская'})
    confirm = (await act(await storage.open_menu(USER, 'profile', NOW), 'delete')).session
    await pool.execute("""
        CREATE FUNCTION reject_reset() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'synthetic reset failure'; END $$;
        CREATE TRIGGER reject_reset BEFORE UPDATE ON profile_ui_sessions
        FOR EACH ROW EXECUTE FUNCTION reject_reset();
    """)
    with pytest.raises(asyncpg.RaiseError):
        await act(confirm, 'yes')
    assert (await profiles.get(USER)).industry == 'Мастерская'
    assert await storage.current(USER) == confirm


async def test_reapplication_preserves_session_and_profile_without_ui_columns(storage_db):
    pool, _ = storage_db
    await profiles.update(USER, {'industry': 'Мастерская'})
    s = await editing('industry')
    before = await profiles.get(USER)
    await pool.execute(SCHEMA_PATH.read_text())
    assert await storage.current(USER) == s
    assert await profiles.get(USER) == before
    assert set(vars(before)) == {'user_id', *storage.FIELDS}


@pytest.fixture
async def routed(storage_db, monkeypatch):
    session = FakeSession()
    bot = Bot('123456:fake-test-token', session=session)
    dispatcher = Dispatcher()
    dispatcher.message.handlers = handlers.router.message.handlers.copy()
    dispatcher.callback_query.handlers = handlers.router.callback_query.handlers.copy()
    reply = AsyncMock(return_value=Answer('no_answer', 'Ответ'))
    monkeypatch.setattr(handlers.core, 'reply', reply)
    monkeypatch.setattr(ui, 'utcnow', lambda: NOW)
    monkeypatch.setattr(reminder_ui, 'utcnow', lambda: NOW)

    async def feed(text=None, data=None):
        start = len(session.calls)
        message = {'message_id': 1, 'date': 1, 'chat': {'id': USER, 'type': 'private'}}
        user = {'id': USER, 'is_bot': False, 'first_name': 'Синтетический'}
        if data is None:
            update = {'update_id': 1, 'message': {**message, 'from': user, 'text': text}}
        else:
            update = {'update_id': 1, 'callback_query': {
                'id': 'synthetic', 'from': user, 'chat_instance': 'test', 'message': message, 'data': data}}
        await dispatcher.feed_update(bot, Update.model_validate(update))
        return session.calls[start:]

    yield feed, reply
    await session.close()


async def click(feed, label):
    s = await storage.current(USER)
    await feed(data=callback(s, label))


async def test_routed_whole_onboarding_profile_delete_events_and_remind(routed, storage_db):
    feed, reply = routed
    pool, _ = storage_db
    await feed('/start')
    await click(feed, 'begin')
    await click(feed, 's2')
    await click(feed, 's0')
    await click(feed, 's3')
    assert await profiles.get(USER) == UserProfile(USER, business_stage='running', legal_form='ip', timezone='Asia/Vladivostok')
    await feed('/remind 2026-11-14 10:00 Проверить план')
    draft = await pool.fetchrow('SELECT * FROM reminder_drafts')
    assert draft['run_at'] == NOW.replace(day=14, hour=0) and draft['timezone'] == 'Asia/Vladivostok'
    await feed('/profile')
    await click(feed, 'f1')
    await feed('<Мастерская>')
    assert (await profiles.get(USER)).industry == '<Мастерская>'
    await click(feed, 'delete')
    await click(feed, 'yes')
    assert await profiles.get(USER) == UserProfile(USER)
    await feed('/remind 2026-11-14 10:00 Новый план')
    drafts = await pool.fetch('SELECT * FROM reminder_drafts ORDER BY run_at')
    assert drafts[0] == draft
    assert drafts[1]['run_at'] == NOW.replace(day=14, hour=7)
    rows = await pool.fetch('SELECT name,payload FROM events ORDER BY id')
    assert [(r['name'], json.loads(r['payload'])) for r in rows] == [
        (events.BOT_STARTED, {}),
        *[(events.PROFILE_UPDATED, {'fields': [f]}) for f in ('business_stage', 'legal_form', 'timezone', 'industry')],
        (events.PROFILE_RESET, {}),
    ]
    reply.assert_not_awaited()


async def test_routed_text_resume_after_pool_restart_cancel_and_normal_dialog(routed, storage_db):
    feed, reply = routed
    pool, open_pool = storage_db
    await feed('/profile')
    assert await pool.fetchval('SELECT count(*) FROM profiles') == 0
    await click(feed, 'f5')
    await pool.close()
    await open_pool()
    await feed('Проверить спрос')
    assert (await profiles.get(USER)).current_goal == 'Проверить спрос'
    await click(feed, 'f1')
    old = await storage.current(USER)
    await feed('/cancel')
    await feed('Как зарегистрировать бизнес?')
    reply.assert_awaited_once_with(USER, 'Как зарегистрировать бизнес?')
    calls = await feed(data=callback(old, 'clear'))
    assert isinstance(calls[0], AnswerCallbackQuery) and 'уже использована' in calls[0].text
    assert (await profiles.get(USER)).current_goal == 'Проверить спрос'


async def test_expiration_cannot_cancel_a_concurrently_opened_session(storage_db):
    old = await editing('industry')
    new = await editing('current_goal')
    await storage.cancel(USER, token=old.token, revision=old.revision)
    assert await storage.current(USER) == new
    await storage.cancel(USER, token=new.token, revision=new.revision)
    assert (await storage.current(USER)).mode == 'idle'


async def test_routed_concurrent_buttons_advance_and_log_only_once(routed, storage_db):
    feed, reply = routed
    pool, _ = storage_db
    await feed('/start')
    await click(feed, 'begin')
    s = await storage.current(USER)
    await asyncio.gather(feed(data=callback(s, 's0')), feed(data=callback(s, 's2')))
    assert (await storage.current(USER)).field == 'legal_form'
    assert await pool.fetchval('SELECT count(*) FROM events WHERE name=$1', events.PROFILE_UPDATED) == 1
    reply.assert_not_awaited()
