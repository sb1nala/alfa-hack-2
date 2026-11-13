"""Знакомство при /start, стартовая подборка и /profile."""

from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import Update

from app import events
from app.bot import handlers
from app.bot import onboarding as ui
from app.models import Answer, Material, UserProfile
from app.recs import service as recs
from app.storage import profiles
from tests.test_reminder_commands import FakeSession

USER = 8_300_000_001


def material(id_, title, access='open', popularity=0.5):
    return Material(id=id_, kind='article', access=access, title=title, popularity=popularity,
                    url=f'https://kurs.alfabank.ru/articles/{id_}/', summary='')


@pytest.fixture
def catalog():
    recs.set_catalog([
        material('a', 'С чего начать <бизнес>', popularity=0.9),
        material('b', 'Как выбрать режим', popularity=0.8),
        material('c', 'Первые клиенты', popularity=0.7),
        material('d', 'Редкая статья', popularity=0.1),
        material('p', 'Платный курс', access='paid', popularity=1.0),
    ])
    yield
    recs.set_catalog([])


@pytest.fixture
async def chat(monkeypatch, catalog):
    session = FakeSession()
    bot = Bot('123456:fake-test-token', session=session)
    dispatcher = Dispatcher()
    dispatcher.message.handlers = handlers.router.message.handlers.copy()
    dispatcher.callback_query.handlers = handlers.router.callback_query.handlers.copy()
    state = {'profile': UserProfile(101), 'events': []}

    async def get(user_id):
        return state['profile']

    async def update(user_id, changes):
        state['profile'] = UserProfile(**{**state['profile'].__dict__, **changes})
        return state['profile']

    async def reset(user_id):
        state['profile'] = UserProfile(101)

    async def log_event(user_id, name, payload=None):
        state['events'].append((name, payload))

    reply = AsyncMock(return_value=Answer('no_answer', 'Ответ'))
    monkeypatch.setattr(handlers.core, 'reply', reply)
    monkeypatch.setattr(profiles, 'get', get)
    monkeypatch.setattr(profiles, 'update', update)
    monkeypatch.setattr(profiles, 'reset', reset)
    monkeypatch.setattr(ui.events, 'log_event', log_event)

    async def feed(text=None, *, data=None, group=False):
        message = {'message_id': 1, 'date': 1,
                   'chat': {'id': 101, 'type': 'group' if group else 'private'}}
        user = {'id': 101, 'is_bot': False, 'first_name': 'Тест'}
        if data is None:
            update_ = {'update_id': 1, 'message': {**message, 'from': user, 'text': text}}
        else:
            update_ = {'update_id': 1, 'callback_query': {
                'id': 'cb', 'from': user, 'chat_instance': 'test', 'data': data,
                'message': {**message, 'from': {'id': 1, 'is_bot': True, 'first_name': 'Бот'},
                            'text': 'старый текст'}}}
        session.calls.clear()
        await dispatcher.feed_update(bot, Update.model_validate(update_))
        return list(session.calls)

    yield feed, state, reply
    await bot.session.close()


def buttons(method):
    return [button.text for row in method.reply_markup.inline_keyboard for button in row]


def edited(calls):
    return next(call for call in calls if isinstance(call, EditMessageText))


async def test_new_user_is_greeted_and_asked_the_first_question(chat):
    feed, state, reply = chat
    calls = await feed('/start')
    sent = [call for call in calls if isinstance(call, SendMessage)]
    assert ui.GREETING in sent[0].text and 'Любой можно пропустить' in sent[0].text
    assert sent[1].text == 'На каком этапе ваш бизнес?'
    assert buttons(sent[1]) == ['Только планирую', 'Недавно начал', 'Уже работает', 'Расширяюсь',
                                'Пропустить', 'Пропустить знакомство']
    assert state['events'] == [(events.BOT_STARTED, None)]
    reply.assert_not_awaited()


async def test_three_answers_fill_the_profile_and_end_with_a_starter_pack(chat):
    feed, state, _ = chat
    form = edited(await feed(data='ob:s:running'))
    assert form.text == 'Как оформлен бизнес?'
    assert buttons(form) == ['ИП', 'ООО', 'Самозанятый', 'Пропустить']
    regime = edited(await feed(data='ob:f:ip'))
    assert regime.text == 'Какой у вас налоговый режим?' and 'УСН «доходы»' in buttons(regime)
    done = edited(await feed(data='ob:r:usn_income'))
    assert state['profile'] == UserProfile(
        101, business_stage='running', legal_form='ip', tax_regime='usn_income')
    assert 'Спасибо! Запомнил: ИП; УСН «доходы»; бизнес работает.' in done.text
    assert 'С чего можно начать:' in done.text and 'напомни завтра в 10' in done.text
    assert done.reply_markup is None
    assert [name for name, _ in state['events']] == [events.PROFILE_UPDATED] * 3
    assert state['events'][0][1] == {'fields': ['business_stage']}


async def test_planning_stage_ends_early_and_self_employed_needs_no_regime(chat):
    feed, state, _ = chat
    done = edited(await feed(data='ob:s:idea'))
    assert 'Запомнил: только планирует бизнес.' in done.text
    assert state['profile'].legal_form is None

    state['profile'] = UserProfile(101)
    done = edited(await feed(data='ob:f:self_employed'))
    assert state['profile'].tax_regime == 'npd' and 'самозанятый' in done.text
    assert 'Какой у вас налоговый режим?' not in done.text


async def test_every_question_can_be_skipped_and_nothing_is_saved(chat):
    feed, state, _ = chat
    assert edited(await feed(data='ob:skip:stage')).text == 'Как оформлен бизнес?'
    assert edited(await feed(data='ob:skip:form')).text == 'Какой у вас налоговый режим?'
    done = edited(await feed(data='ob:skip:regime'))
    assert 'Хорошо, обойдёмся без анкеты.' in done.text and 'С чего можно начать:' in done.text
    assert state['profile'] == UserProfile(101) and state['events'] == []


async def test_whole_onboarding_can_be_skipped_at_once(chat):
    feed, state, _ = chat
    done = edited(await feed(data='ob:end'))
    assert 'обойдёмся без анкеты' in done.text and state['profile'] == UserProfile(101)


def test_starter_pack_is_popular_open_materials_with_marked_links(catalog):
    pack = ui.starter_pack(UserProfile(1))
    assert pack.startswith('С чего можно начать:')
    assert pack.count('•') == ui.STARTER_COUNT
    assert 'С чего начать &lt;бизнес&gt;' in pack and 'Редкая статья' not in pack
    assert 'Платный курс' not in pack  # новичку платное не навязываем
    assert 'utm_campaign=starter' in pack and 'utm_source=alfa_kurs_bot' in pack


def test_starter_pack_is_empty_without_catalog_and_survives_errors(monkeypatch):
    recs.set_catalog([])
    assert ui.starter_pack(UserProfile(1)) == ''
    monkeypatch.setattr(ui.recs, 'recommend', lambda *a, **k: 1 / 0)
    assert ui.starter_pack(UserProfile(1)) == ''
    assert 'обойдёмся без анкеты' in ui.finish_text(UserProfile(1))


async def test_known_user_is_not_questioned_again(chat):
    feed, state, _ = chat
    state['profile'] = UserProfile(101, legal_form='ooo', tax_regime='osno')
    calls = await feed('/start')
    assert len([call for call in calls if isinstance(call, SendMessage)]) == 1
    assert 'Я помню ваш бизнес: ООО; общая система налогообложения.' in calls[-1].text
    assert buttons(calls[-1]) == ['Обновить', 'Забыть']
    assert edited(await feed(data='ob:restart')).text == 'На каком этапе ваш бизнес?'


async def test_profile_command_shows_updates_and_forgets(chat):
    feed, state, _ = chat
    calls = await feed('/profile')
    assert calls[-1].text == ui.EMPTY_PROFILE_TEXT
    assert buttons(calls[-1]) == ['Ответить на три вопроса']

    state['profile'] = UserProfile(101, legal_form='ip', industry='Общепит <кафе>')
    calls = await feed('/profile')
    assert 'ИП; сфера: Общепит &lt;кафе&gt;.' in calls[-1].text
    assert buttons(calls[-1]) == ['Обновить', 'Забыть']

    forgotten = edited(await feed(data='ob:reset'))
    assert forgotten.text == ui.FORGOTTEN_TEXT and state['profile'] == UserProfile(101)
    assert state['events'][-1] == (events.PROFILE_RESET, None)


@pytest.mark.parametrize('data', ['ob:', 'ob:s:rich', 'ob:f:zao', 'ob:r:npd', 'ob:skip:all', 'ob:x'])
async def test_unknown_buttons_are_answered_and_change_nothing(chat, data):
    feed, state, _ = chat
    calls = await feed(data=data)
    assert isinstance(calls[-1], AnswerCallbackQuery) and 'устарела' in calls[-1].text
    assert state['profile'] == UserProfile(101)


async def test_storage_failure_is_reported_without_details(chat, monkeypatch):
    feed, *_ = chat
    monkeypatch.setattr(profiles, 'update', AsyncMock(side_effect=RuntimeError('secret dsn')))
    calls = await feed(data='ob:s:running')
    assert isinstance(calls[-1], AnswerCallbackQuery)
    assert calls[-1].text == ui.FAILED_TEXT and 'secret' not in calls[-1].text


async def test_start_still_greets_when_profile_or_events_are_unavailable(chat, monkeypatch):
    feed, *_ = chat
    monkeypatch.setattr(profiles, 'get', AsyncMock(side_effect=RuntimeError('down')))
    monkeypatch.setattr(ui.events, 'log_event', AsyncMock(side_effect=RuntimeError('down')))
    calls = await feed('/start')
    assert ui.GREETING in calls[0].text and calls[-1].text == 'На каком этапе ваш бизнес?'


async def test_group_chat_gets_a_greeting_without_questions_or_profile(chat):
    feed, state, _ = chat
    calls = await feed('/start', group=True)
    assert [call.text for call in calls if isinstance(call, SendMessage)] == [ui.GREETING]
    assert 'личном чате' in (await feed('/profile', group=True))[-1].text
    assert state['events'] == []


@pytest.mark.parametrize(('done', 'profile', 'expected'), [
    ('stage', UserProfile(1, business_stage='idea'), None),
    ('stage', UserProfile(1, business_stage='running'), 'form'),
    ('form', UserProfile(1, legal_form='self_employed'), None),
    ('form', UserProfile(1, legal_form='ooo'), 'regime'),
    ('regime', UserProfile(1), None),
])
def test_next_step(done, profile, expected):
    assert ui.next_step(done, profile) == expected


async def test_answers_are_saved_in_the_real_database(storage_db):
    await profiles.update(USER, {'business_stage': 'starting'})
    await profiles.update(USER, {'legal_form': 'self_employed', 'tax_regime': 'npd'})
    saved = await profiles.get(USER)
    assert (saved.business_stage, saved.legal_form, saved.tax_regime) == (
        'starting', 'self_employed', 'npd')
    await profiles.reset(USER)
    assert await profiles.get(USER) == UserProfile(USER)
