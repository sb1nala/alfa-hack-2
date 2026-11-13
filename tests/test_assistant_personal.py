"""Ответ с учётом профиля: запоминание сведений, рекомендации без повторов, журнал событий."""

from unittest.mock import AsyncMock

import pytest

from app import events
from app.assistant import core
from app.assistant.core import business_summary, reply
from app.bot.render import render_answer
from app.kb import search as kb
from app.kb.search import SearchIndex
from app.models import Answer, Chunk, Material, Recommendation, UserProfile
from app.recs import service as recs

USER = 987654321


def material(id_, title, text, access='open', kind='article', industries=(), tags=()):
    item = Material(
        id=id_, kind=kind, access=access, title=title,
        url=f'https://kurs.alfabank.ru/{kind}s/{id_}/', summary=f'Описание: {title}.',
        industries=list(industries), tags=list(tags),
    )
    return Chunk(material=item, text=text, score=0.0)


CHUNKS = [
    material('usn', 'Как платить налог на УСН', 'Налог на УСН платят авансами раз в квартал.'),
    material('usn-cafe', 'Налоги для кафе', 'Кафе на УСН: налог и касса.', industries=['Общепит']),
    material('usn-course', 'Курс по налогам', 'Курс: налог на УСН для начинающих.',
             access='subscription', kind='course'),
    material('hire', 'Как нанять сотрудника', 'Трудовой договор и приказ о приёме.'),
]


class FakeStorage:
    """Профиль, показы и события в памяти вместо PostgreSQL."""

    def __init__(self):
        self.profile = UserProfile(USER)
        self.shown: set[str] = set()
        self.events: list[tuple[str, dict]] = []

    async def get(self, user_id):
        return self.profile

    async def update_from_message(self, user_id, text):
        if 'кафе' in text:
            self.profile = UserProfile(USER, legal_form='ip', tax_regime='usn_income', industry='Общепит')
        return self.profile

    async def list_ids(self, user_id):
        return set(self.shown)

    async def mark_shown(self, user_id, material_id):
        self.shown.add(material_id)

    async def log_event(self, user_id, name, payload=None):
        self.events.append((name, payload or {}))

    def named(self, name):
        return [payload for event, payload in self.events if event == name]


class FakeModel:
    def __init__(self):
        self.result = {'kind': 'answer', 'text': 'Ответ', 'used': [1], 'options': []}
        self.calls = []

    async def complete(self, messages, json_schema=None):
        self.calls.append(messages)
        if self.result['kind'] != 'answer':
            return self.result
        # Модель «ссылается» на фрагмент статьи про УСН, каким бы по счёту он ни пришёл.
        numbers = [
            line[1:line.index(']')] for line in messages[-1]['content'].splitlines()
            if line.startswith('[') and 'Как платить налог на УСН' in line
        ]
        return {**self.result, 'used': [int(numbers[0])] if numbers else [1]}

    @property
    def prompt(self):
        return self.calls[-1][-1]['content']


@pytest.fixture
def env(monkeypatch):
    kb.set_index(SearchIndex(CHUNKS))
    recs.set_catalog([chunk.material for chunk in CHUNKS])
    core.reset_history()
    storage, model = FakeStorage(), FakeModel()
    monkeypatch.setattr(core.profile_service, 'get', storage.get)
    monkeypatch.setattr(core.profile_service, 'update_from_message', storage.update_from_message)
    monkeypatch.setattr(core.shown_materials, 'list_ids', storage.list_ids)
    monkeypatch.setattr(core.shown_materials, 'mark_shown', storage.mark_shown)
    monkeypatch.setattr(core.events, 'log_event', storage.log_event)
    monkeypatch.setattr(core.llm, 'is_configured', lambda: True)
    monkeypatch.setattr(core.llm, 'complete', model.complete)
    yield storage, model
    kb.set_index(None)
    recs.set_catalog([])
    core.reset_history()


def rec_ids(answer):
    return [item.material.id for item in answer.recommendations]


async def test_answer_gets_recommendations_other_than_its_sources(env):
    answer = await reply(USER, 'налог на усн')
    assert answer.kind == 'answer' and [s.id for s in answer.sources] == ['usn']
    assert rec_ids(answer) and 'usn' not in rec_ids(answer)
    assert len(answer.recommendations) <= core.MAX_RECOMMENDATIONS
    assert all(item.reason for item in answer.recommendations)


async def test_business_facts_from_the_message_shape_prompt_and_recommendations(env):
    storage, model = env
    answer = await reply(USER, 'у меня кафе, как платить налог на усн')
    assert 'О бизнесе пользователя: ИП; УСН «доходы»; сфера: Общепит.' in model.prompt
    assert rec_ids(answer)[0] == 'usn-cafe'
    assert answer.recommendations[0].reason == 'Подходит для вашей сферы: Общепит.'
    assert storage.named(events.PROFILE_UPDATED) == [
        {'fields': ['industry', 'legal_form', 'tax_regime']}]


async def test_empty_profile_adds_nothing_to_the_prompt_and_no_user_id_ever(env):
    _, model = env
    await reply(USER, 'налог на усн')
    assert 'О бизнесе пользователя' not in model.prompt
    await reply(USER, 'у меня кафе, как платить налог на усн')
    assert all(str(USER) not in message['content'] for call in model.calls for message in call)


async def test_shown_materials_are_not_recommended_again(env):
    storage, _ = env
    first = await reply(USER, 'налог на усн')
    assert storage.shown == {'usn', *rec_ids(first)}
    core.reset_history()
    second = await reply(USER, 'налог на усн')
    assert not set(rec_ids(second)) & set(rec_ids(first))


async def test_paid_recommendation_shows_only_its_description(env):
    storage, _ = env
    storage.shown = {'usn-cafe'}
    answer = await reply(USER, 'налог на усн')
    paid = next(item for item in answer.recommendations if item.material.is_paid)
    assert paid.reason == 'Описание: Курс по налогам.'
    assert 'для начинающих' not in paid.reason  # текст материала в рекомендацию не попадает
    assert '(платный)' in render_answer(answer)


async def test_clarify_and_no_answer_get_no_recommendations(env):
    storage, model = env
    model.result = {'kind': 'clarify', 'text': 'ИП или ООО?', 'used': [], 'options': ['ИП', 'ООО']}
    assert (await reply(USER, 'налог на усн')).recommendations == []
    core.reset_history()
    assert (await reply(USER, 'рецепт борща')).recommendations == []
    assert [payload['kind'] for payload in storage.named(events.QUESTION_ASKED)] == [
        'clarify', 'no_answer']
    assert storage.named(events.MATERIAL_SHOWN) == []


async def test_events_record_what_was_shown_without_the_question_text(env):
    storage, _ = env
    answer = await reply(USER, 'налог на усн')
    assert storage.named(events.QUESTION_ASKED) == [{'kind': 'answer', 'sources': 1}]
    shown = storage.named(events.MATERIAL_SHOWN)
    assert shown[0] == {'material_id': 'usn', 'kind': 'article', 'access': 'open', 'place': 'answer'}
    assert [p['place'] for p in shown[1:]] == ['recommendation'] * len(answer.recommendations)
    assert 'налог' not in str(storage.events)


async def test_storage_failures_never_break_the_answer(env, monkeypatch):
    broken = AsyncMock(side_effect=RuntimeError('database is down'))
    for target, name in (
        (core.profile_service, 'get'), (core.profile_service, 'update_from_message'),
        (core.shown_materials, 'list_ids'), (core.shown_materials, 'mark_shown'),
        (core.events, 'log_event'),
    ):
        monkeypatch.setattr(target, name, broken)
    answer = await reply(USER, 'налог на усн')
    assert answer.kind == 'answer' and answer.text == 'Ответ' and answer.sources


async def test_recommendation_failure_keeps_the_answer(env, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError('boom')

    monkeypatch.setattr(core.recs, 'recommend', broken)
    answer = await reply(USER, 'налог на усн')
    assert answer.kind == 'answer' and answer.recommendations == []


IP_USN = UserProfile(1, legal_form='ip', tax_regime='usn_income')


@pytest.mark.parametrize(('query', 'profile', 'expected'), [
    ('как платить налоги', IP_USN, 'как платить налоги ИП УСН'),
    ('когда сдавать декларацию', UserProfile(1, legal_form='ooo', tax_regime='osno'),
     'когда сдавать декларацию ООО ОСНО'),
    ('как платить налог на УСН', IP_USN, 'как платить налог на УСН ИП'),   # режим уже назван
    ('налоги самозанятого', UserProfile(1, legal_form='self_employed', tax_regime='npd'),
     'налоги самозанятого'),
    ('как нанять сотрудника', IP_USN, 'как нанять сотрудника'),           # не про налоги
    ('как платить налоги', UserProfile(1), 'как платить налоги'),          # профиль пуст
])
def test_tax_questions_are_searched_with_the_user_regime(query, profile, expected):
    assert core.personal_query(query, profile) == expected


@pytest.mark.parametrize(('title', 'profile', 'skipped'), [
    ('ИП на ЕСХН: взносы и налоги', IP_USN, True),
    ('Налог на прибыль для ООО', IP_USN, True),
    ('ИП на ОСН: как рассчитать налог', IP_USN, True),
    ('УСН для ИП: налоги и отчётность', IP_USN, False),
    ('Авансовые платежи на упрощёнке', IP_USN, False),
    ('Переходим с УСН на ОСН и обратно', IP_USN, False),     # упомянут и свой режим
    ('Основы финансового учёта', IP_USN, False),              # «основы» — не «ОСН»
    ('Как нанять первого сотрудника', IP_USN, False),
    ('Налог на прибыль для ООО', UserProfile(1), False),      # о пользователе ничего не знаем
    ('Как самозанятому оплатить налог', UserProfile(1, tax_regime='npd'), False),
    ('Патент для ИП', UserProfile(1, tax_regime='npd'), True),
])
def test_materials_about_another_regime_or_form_are_not_recommended(title, profile, skipped):
    item = Material(id='x', kind='article', access='open', title=title, url='https://x.test/', summary='')
    assert core.is_for_someone_else(item, profile) is skipped


@pytest.fixture
def courses(monkeypatch):
    def course(id_, title, access='open', popularity=0.0, summary=''):
        return Material(id=id_, kind='course', access=access, title=title, popularity=popularity,
                        url=f'https://kurs.alfabank.ru/courses/{id_}/', summary=summary)

    catalog = [
        course('team', 'Как управлять командой', 'subscription', 0.4,
               'Научитесь ставить задачи и мотивировать. Внутри восемь уроков'),
        course('fin', 'Азы финансовой грамотности', popularity=0.9),
        course('sales', 'Подбор команды продаж', popularity=0.1),
        Material(id='art', kind='article', access='open', title='Статья про команду',
                 url='https://kurs.alfabank.ru/articles/art/', summary=''),
    ]
    monkeypatch.setattr(core.kb, 'materials', lambda: catalog)


def test_direct_question_about_a_course_gets_the_course_itself(courses):
    found = core.course_for('есть ли курс про управление командой', set())
    assert found.material.id == 'team'
    assert found.reason == 'Научитесь ставить задачи и мотивировать.'  # платный: только описание
    free = core.course_for('хочу курс по финансовой грамотности', set())
    assert free.material.id == 'fin' and free.reason == recs.KIND_REASONS['course']


def test_course_is_not_forced_when_nobody_asked_or_nothing_fits(courses):
    assert core.course_for('как управлять командой', set()) is None        # про курс не спрашивали
    assert core.course_for('есть ли курс про выпечку хлеба', set()) is None
    assert core.course_for('есть ли курс про управление командой', {'team'}).material.id == 'sales'


@pytest.mark.parametrize(('profile', 'expected'), [
    (UserProfile(1), ''),
    (UserProfile(1, legal_form='ooo', tax_regime='osno'), 'ООО; общая система налогообложения'),
    (UserProfile(1, business_stage='idea', current_goal='Запуск бизнеса'),
     'только планирует бизнес; текущая задача: Запуск бизнеса'),
    (UserProfile(1, legal_form='выдумка', tax_regime='?', timezone='Asia/Omsk'), ''),
])
def test_business_summary(profile, expected):
    assert business_summary(profile) == expected


def test_links_to_the_platform_carry_utm_marks_and_other_links_do_not():
    own = Material(id='1', kind='course', access='paid', title='Курс',
                   url='https://kurs.alfabank.ru/courses/x/', summary='')
    other = Material(id='2', kind='article', access='open', title='Статья',
                     url='https://example.test/a/', summary='')
    text = render_answer(Answer(
        kind='answer', text='Ответ', sources=[own, other],
        recommendations=[Recommendation(material=own, reason='Курс по теме.')]))
    assert ('utm_source=alfa_kurs_bot&amp;utm_medium=telegram&amp;utm_campaign=answer'
            '&amp;utm_content=course') in text
    assert 'utm_campaign=recommendation' in text
    assert '<a href="https://example.test/a/">Статья</a>' in text
