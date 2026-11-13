"""Стартовая подборка из main и безопасная миграция старого интерфейса.

Полные сценарии новой анкеты — test_profile_ui и test_profile_ui_storage.
"""

import pytest
from aiogram.methods import AnswerCallbackQuery

from app.bot import onboarding as ui
from app.models import Material, UserProfile
from app.recs import service as recs
from app.storage import profiles
from tests.test_profile_ui import chat, choose, state

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


async def test_answers_are_saved_in_the_real_database(storage_db):
    await profiles.update(USER, {'business_stage': 'starting'})
    await profiles.update(USER, {'legal_form': 'self_employed', 'tax_regime': 'npd'})
    saved = await profiles.get(USER)
    assert (saved.business_stage, saved.legal_form, saved.tax_regime) == (
        'starting', 'self_employed', 'npd')
    await profiles.reset(USER)
    assert await profiles.get(USER) == UserProfile(USER)



@pytest.mark.parametrize('data', ['ob:', 'ob:s:running', 'ob:f:ip', 'ob:r:usn_income',
                                'ob:skip:stage', 'ob:restart', 'ob:reset', 'ob:end'])
async def test_legacy_buttons_are_acknowledged_and_cannot_change_or_delete_profile(chat, data):
    feed, mocks = chat
    calls = await feed(data=data)
    assert len(calls) == 1 and isinstance(calls[0], AnswerCallbackQuery)
    assert 'устарела' in calls[0].text and '/start' in calls[0].text
    mocks.transition.assert_not_awaited()
    mocks.reply.assert_not_awaited()
    mocks.get.assert_not_awaited()
    mocks.metric.assert_not_awaited()


@pytest.mark.parametrize('action,field', [('all', None), ('skip', 'timezone'), ('s0', 'timezone')])
async def test_new_flow_keeps_starter_pack_on_finish_and_skip(chat, catalog, action, field):
    feed, mocks = chat
    before = state('onboarding' if field else 'offer', field)
    calls = await choose(feed, mocks, before, action, state('idle'),
                         fields=('timezone',) if action == 's0' else ())
    assert 'С чего можно начать:' in calls[-1].text
    assert 'utm_campaign=starter' in calls[-1].text
    assert 'Платный курс' not in calls[-1].text
    mocks.reply.assert_not_awaited()
    mocks.draft.assert_not_awaited()


async def test_starter_pack_failure_does_not_hide_completion(chat, monkeypatch):
    feed, mocks = chat
    monkeypatch.setattr(ui, 'starter_pack', lambda *a: 1 / 0)
    calls = await choose(feed, mocks, state('offer'), 'all', state('idle'))
    assert 'задайте вопрос о бизнесе' in calls[-1].text


def test_starter_pack_limits_html_and_url_length(catalog, monkeypatch):
    from app.models import Recommendation

    item = material('synthetic', '<&😀' * 10000)
    monkeypatch.setattr(recs, 'recommend', lambda *a: [Recommendation(item, '')] * 3)
    pack = ui.starter_pack(UserProfile(1))
    assert len(pack.encode('utf-16-le')) // 2 <= 1900 and '&lt;' in pack
    item.url = 'https://kurs.alfabank.ru/' + 'x' * 10000
    assert ui.starter_pack(UserProfile(1)) == ''
