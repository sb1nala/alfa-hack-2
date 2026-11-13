"""Налоговые напоминания: подбор сроков по режиму, момент напоминания, диалог и запись в базу."""

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import Update

from app.bot import handlers
from app.bot import tax as ui
from app.kb import search as kb
from app.kb.search import SearchIndex
from app.models import Answer, Chunk, Material, Reminder, UserProfile
from app.reminders import service, tax
from app.storage import profiles
from tests.test_reminder_commands import FakeSession

MSK = ZoneInfo('Europe/Moscow')
# Пятница, 13 ноября 2026, 12:00 по Москве.
NOW = datetime(2026, 11, 13, 9, 0, tzinfo=timezone.utc)
TODAY = date(2026, 11, 13)
USER = 8_100_000_001

IP_USN = UserProfile(USER, legal_form='ip', tax_regime='usn_income')
OOO_USN = UserProfile(USER, legal_form='ooo', tax_regime='usn_income_expense')
SELF_EMPLOYED = UserProfile(USER, tax_regime='npd')
PATENT = UserProfile(USER, tax_regime='psn')


def ids(profile):
    return {due.event_id for due in tax.upcoming(profile, TODAY)}


# --- подбор сроков ---


def test_ip_on_usn_gets_usn_and_contribution_deadlines_but_not_ooo_ones():
    found = ids(IP_USN)
    assert {'usn_advance', 'usn_declaration_ip', 'usn_tax_ip', 'ip_contributions_fixed'} <= found
    assert not {'usn_declaration_ooo', 'usn_tax_ooo', 'npd_tax', 'osno_vat_payment'} & found


def test_ooo_on_usn_gets_no_personal_contributions():
    found = ids(OOO_USN)
    assert {'usn_advance', 'usn_declaration_ooo', 'usn_tax_ooo'} <= found
    assert not any(event.startswith('ip_contributions') or event.endswith('_ip') for event in found)


def test_self_employed_gets_only_monthly_tax():
    assert ids(SELF_EMPLOYED) == {'npd_tax'}
    dues = tax.upcoming(SELF_EMPLOYED, TODAY)
    assert dues[0].due == date(2026, 11, 30) and dues[0].period == 'октябрь 2026'
    assert [d.due for d in dues] == sorted(d.due for d in dues)


def test_only_deadlines_from_today_within_the_horizon():
    dues = tax.upcoming(IP_USN, date(2027, 4, 28), horizon_days=30)
    assert dues and all(date(2027, 4, 28) <= d.due <= date(2027, 5, 28) for d in dues)
    assert tax.upcoming(IP_USN, date(2029, 1, 1)) == []


def test_form_is_implied_for_self_employed_and_patent_but_asked_otherwise():
    assert tax.missing(UserProfile(USER)) == 'regime'
    assert tax.missing(UserProfile(USER, tax_regime='usn_income')) == 'legal_form'
    assert tax.missing(UserProfile(USER, tax_regime='osno')) == 'legal_form'
    assert tax.missing(IP_USN) is None
    assert tax.missing(SELF_EMPLOYED) is None and tax.effective_form(SELF_EMPLOYED) == 'self_employed'
    assert tax.missing(PATENT) is None and tax.effective_form(PATENT) == 'ip'


def test_patent_has_personal_deadlines_and_still_gets_contributions():
    assert tax.has_relative_events(PATENT) and not tax.has_relative_events(IP_USN)
    assert ids(PATENT) == {'ip_contributions_fixed', 'ip_contributions_1pct'}


# --- момент напоминания ---


@pytest.mark.parametrize(('due', 'expected'), [
    (date(2026, 11, 30), '2026-11-27 10:00'),   # за три дня до срока
    (date(2026, 11, 16), '2026-11-14 10:00'),   # три дня назад уже прошло — завтра утром
    (date(2026, 11, 14), '2026-11-14 10:00'),   # срок завтра — напомним в день срока
    (date(2026, 11, 13), None),                 # срок сегодня, а 10:00 уже прошло
    (date(2026, 11, 10), None),                 # срок в прошлом
])
def test_reminder_moment(due, expected):
    moment = tax.remind_at(due, NOW, 'Europe/Moscow')
    assert (moment and moment.astimezone(MSK).strftime('%Y-%m-%d %H:%M')) == expected


def test_reminder_moment_is_ten_in_the_morning_local_time():
    early = datetime(2026, 11, 13, 5, 0, tzinfo=timezone.utc)  # 08:00 в Москве
    assert tax.remind_at(date(2026, 11, 13), early, 'Europe/Moscow') == datetime(
        2026, 11, 13, 7, 0, tzinfo=timezone.utc)
    far_east = tax.remind_at(date(2026, 11, 30), NOW, 'Asia/Vladivostok')
    assert far_east == datetime(2026, 11, 27, 0, 0, tzinfo=timezone.utc)
    assert tax.remind_at(date(2026, 11, 30), NOW, 'Mars/Olympus') == tax.remind_at(
        date(2026, 11, 30), NOW, 'Europe/Moscow')


def test_plan_skips_deadlines_that_can_no_longer_be_announced():
    planned = tax.plan(SELF_EMPLOYED, NOW)
    assert planned[0][0].due == date(2026, 11, 30)
    assert all(moment > NOW and moment.astimezone(MSK).date() <= due.due for due, moment in planned)


# --- текст напоминания ---


@pytest.fixture
def no_kb():
    kb.set_index(None)
    yield
    kb.set_index(None)


def test_text_has_deadline_period_and_official_source(no_kb):
    due = tax.upcoming(SELF_EMPLOYED, TODAY)[0]
    text = tax.reminder_text(due)
    assert 'Срок — 30.11.2026 (октябрь 2026).' in text
    assert 'https://npd.nalog.ru/' in text and 'Как это сделать' not in text


def test_text_links_a_platform_material_with_utm(no_kb):
    material = Material(
        id='m1', kind='article', access='open', title='Как самозанятому оплатить налог',
        url='https://kurs.alfabank.ru/articles/kak-oplatit/', summary='')
    kb.set_index(SearchIndex([Chunk(material, 'Как самозанятому оплатить налог в приложении.', 0.0)]))
    text = tax.reminder_text(tax.upcoming(SELF_EMPLOYED, TODAY)[0])
    assert 'Как это сделать: Как самозанятому оплатить налог' in text
    assert 'utm_source=alfa_kurs_bot' in text and 'utm_campaign=reminder_tax' in text


# --- распознавание просьбы ---


@pytest.mark.parametrize(('text', 'expected'), [
    ('напоминай о налогах', True),
    ('Напомни про взносы и декларацию', True),
    ('поставь напоминание об уплате УСН', True),
    ('напомни 28 ноября в 10 утра оплатить налог', False),   # свой срок — обычное напоминание
    ('напомни завтра сверить кассу', False),
    ('когда платить налог по УСН', False),                    # вопрос к базе знаний
])
def test_tax_request_detection(text, expected):
    assert ui.is_tax_request(text) is expected


# --- диалог ---


@pytest.fixture
async def chat(monkeypatch, no_kb, no_profile_input):
    session = FakeSession()
    bot = Bot('123456:fake-test-token', session=session)
    dispatcher = Dispatcher()
    dispatcher.message.handlers = handlers.router.message.handlers.copy()
    dispatcher.callback_query.handlers = handlers.router.callback_query.handlers.copy()
    state = {'profile': UserProfile(101)}

    async def get(user_id):
        return state['profile']

    async def update(user_id, changes):
        state['profile'] = UserProfile(**{**state['profile'].__dict__, **changes})
        return state['profile']

    reply = AsyncMock(return_value=Answer('no_answer', 'Ответ'))
    monkeypatch.setattr(handlers.core, 'reply', reply)
    monkeypatch.setattr(ui, 'utcnow', lambda: NOW)
    monkeypatch.setattr(profiles, 'get', get)
    monkeypatch.setattr(profiles, 'update', update)
    subscribe = AsyncMock(return_value=14)
    unsubscribe = AsyncMock(return_value=14)
    monkeypatch.setattr(tax, 'subscribe', subscribe)
    monkeypatch.setattr(tax, 'unsubscribe', unsubscribe)

    async def feed(text=None, *, data=None, group=False):
        message = {'message_id': 1, 'date': 1,
                   'chat': {'id': 101, 'type': 'group' if group else 'private'}}
        user = {'id': 101, 'is_bot': False, 'first_name': 'Тест'}
        if data is None:
            update_ = {'update_id': 1, 'message': {**message, 'from': user, 'text': text}}
        else:
            update_ = {'update_id': 1, 'callback_query': {
                'id': 'cb', 'from': user, 'chat_instance': 'test',
                'message': {**message, 'from': {'id': 1, 'is_bot': True, 'first_name': 'Бот'},
                            'text': 'старый текст'},
                'data': data}}
        session.calls.clear()
        await dispatcher.feed_update(bot, Update.model_validate(update_))
        return list(session.calls)

    yield feed, state, reply, subscribe, unsubscribe
    await bot.session.close()


def buttons(method):
    return [button.text for row in method.reply_markup.inline_keyboard for button in row]


def edited(calls):
    return next(call for call in calls if isinstance(call, EditMessageText))


async def test_full_dialogue_from_request_to_confirmation(chat):
    feed, state, reply, subscribe, _ = chat
    calls = await feed('напоминай о налогах')
    reply.assert_not_awaited()
    assert isinstance(calls[-1], SendMessage) and calls[-1].text == ui.ASK_REGIME
    assert 'УСН «доходы»' in buttons(calls[-1]) and 'Самозанятый (НПД)' in buttons(calls[-1])

    calls = await feed(data='tx:r:usn_income')
    assert state['profile'].tax_regime == 'usn_income'
    assert edited(calls).text == ui.ASK_FORM and buttons(edited(calls)) == ['ИП', 'ООО']

    calls = await feed(data='tx:f:ip')
    assert state['profile'].legal_form == 'ip'
    plan = edited(calls)
    assert 'Ваш режим: ИП, УСН «доходы».' in plan.text
    assert 'Ближайшие сроки:' in plan.text and '28.12.2026' in plan.text
    assert 'проверьте дату' in plan.text
    assert buttons(plan) == ['Подтвердить', 'Другой режим', 'Отмена']
    subscribe.assert_not_awaited()

    calls = await feed(data='tx:ok')
    subscribe.assert_awaited_once()
    assert subscribe.await_args.args[0].tax_regime == 'usn_income'
    done = edited(calls)
    assert 'поставил напоминаний: 14' in done.text
    assert buttons(done) == ['Отключить налоговые напоминания']
    assert any(isinstance(call, AnswerCallbackQuery) for call in calls)


async def test_known_profile_goes_straight_to_the_plan(chat):
    feed, state, *_ = chat
    state['profile'] = UserProfile(101, tax_regime='npd')
    calls = await feed('/taxes')
    assert 'Ваш режим: самозанятость (НПД).' in calls[-1].text
    assert '30.11.2026' in calls[-1].text and buttons(calls[-1])[0] == 'Подтвердить'


async def test_self_employed_and_patent_are_not_asked_for_the_form(chat):
    feed, state, *_ = chat
    calls = await feed(data='tx:r:npd')
    assert state['profile'].legal_form == 'self_employed' and 'Ваш режим' in edited(calls).text
    calls = await feed(data='tx:r:psn')
    assert state['profile'].legal_form == 'ip'
    assert 'Срок оплаты патента зависит от дат вашего патента' in edited(calls).text


async def test_changing_regime_from_self_employed_asks_the_form_again(chat):
    feed, state, *_ = chat
    state['profile'] = UserProfile(101, tax_regime='npd', legal_form='self_employed')
    calls = await feed(data='tx:r:usn_income')
    assert state['profile'].legal_form is None and edited(calls).text == ui.ASK_FORM


async def test_cancel_change_and_disable(chat):
    feed, state, _, subscribe, unsubscribe = chat
    assert 'не ставлю' in edited(await feed(data='tx:no')).text
    assert edited(await feed(data='tx:chg')).text == ui.ASK_REGIME
    subscribe.assert_not_awaited()
    assert 'Отключил налоговых напоминаний: 14' in edited(await feed(data='tx:off')).text
    unsubscribe.assert_awaited_once_with(101)


async def test_repeated_confirmation_says_nothing_new_was_added(chat):
    feed, state, _, subscribe, _ = chat
    state['profile'] = UserProfile(101, tax_regime='npd')
    subscribe.return_value = 0
    assert 'уже стоят' in edited(await feed(data='tx:ok')).text


async def test_confirmation_without_profile_asks_again_instead_of_failing(chat):
    feed, _, _, subscribe, _ = chat
    assert edited(await feed(data='tx:ok')).text == ui.ASK_REGIME
    subscribe.assert_not_awaited()


@pytest.mark.parametrize('data', ['tx:r:выдумка', 'tx:f:self_employed', 'tx:', 'tx:zzz'])
async def test_unknown_buttons_are_answered_and_change_nothing(chat, data):
    feed, state, _, subscribe, _ = chat
    calls = await feed(data=data)
    assert isinstance(calls[-1], AnswerCallbackQuery) and 'устарела' in calls[-1].text
    assert state['profile'] == UserProfile(101)
    subscribe.assert_not_awaited()


async def test_storage_failure_is_reported_without_details(chat, monkeypatch):
    feed, *_ = chat
    monkeypatch.setattr(profiles, 'update', AsyncMock(side_effect=RuntimeError('secret dsn')))
    calls = await feed(data='tx:r:npd')
    assert isinstance(calls[-1], AnswerCallbackQuery)
    assert calls[-1].text == ui.FAILED_TEXT and 'secret' not in calls[-1].text


async def test_group_chat_and_dated_request_are_not_tax_setup(chat):
    feed, _, reply, *_ = chat
    assert (await feed('/taxes', group=True))[-1].text == ui.PRIVATE_ONLY_TEXT
    await feed('напоминай о налогах', group=True)
    reply.assert_awaited_once()  # в группе это обычный вопрос


# --- запись в базу ---


async def test_subscribe_creates_tax_reminders_once_and_unsubscribe_disables_them(storage_db, no_kb):
    profile = UserProfile(USER, tax_regime='npd')
    created = await tax.subscribe(profile, NOW)
    assert created == len(tax.plan(profile, NOW)) > 10
    saved = await service.list_for(USER)
    assert all(r.kind == 'tax' and r.active and r.rrule is None for r in saved)
    assert saved[0].next_run_at == datetime(2026, 11, 27, 7, 0, tzinfo=timezone.utc)
    assert 'Срок — 30.11.2026' in saved[0].text
    assert await tax.subscribe(profile, NOW) == 0  # повторное подтверждение ничего не дублирует
    assert len(await service.list_for(USER)) == created

    await service.create(Reminder(0, USER, 'custom', 'Своё вымышленное дело', saved[0].next_run_at, None))
    assert await tax.unsubscribe(USER) == created
    left = await service.list_for(USER)
    assert [r.kind for r in left if r.active] == ['custom']
    assert await tax.unsubscribe(USER) == 0
    assert await tax.subscribe(profile, NOW) == created  # после отключения можно поставить заново
