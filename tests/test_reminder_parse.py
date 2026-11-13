"""Разбор «напомни…»: расчёт даты без модели и работа с ответом модели."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.models import Reminder, UserProfile
from app.reminders import parse
from app.reminders.content import confirmation, repeat_label
from app.reminders.parse import (
    ASK_AGAIN, ASK_PAST, ASK_TEXT, ASK_TIME, NotReminder, looks_like_reminder, parse_request,
    resolve,
)
from app.reminders.schedule import canonicalize

MSK = ZoneInfo('Europe/Moscow')
# Пятница, 13 ноября 2026, 12:00 по Москве.
NOW = datetime(2026, 11, 13, 9, 0, tzinfo=timezone.utc)
PROFILE = UserProfile(user_id=101)


def parts(**over):
    base = {
        'is_reminder': True, 'text': 'Сверить кассу', 'date': '', 'day_offset': -1,
        'day_of_month': -1, 'weekday': 'none', 'time': '', 'minutes_from_now': -1, 'repeat': 'none',
    }
    return base | over


def local(reminder: Reminder) -> str:
    return reminder.next_run_at.astimezone(MSK).strftime('%Y-%m-%d %H:%M')


@pytest.mark.parametrize(('given', 'expected'), [
    ({'day_offset': 1, 'time': '10:00'}, '2026-11-14 10:00'),          # завтра в 10
    ({'day_offset': 0, 'time': '18:30'}, '2026-11-13 18:30'),          # сегодня вечером
    ({'time': '15:00'}, '2026-11-13 15:00'),                           # в 15:00 — ещё сегодня
    ({'time': '09:00'}, '2026-11-14 09:00'),                           # в 9 — уже завтра
    ({'weekday': 'mo', 'time': '09:30'}, '2026-11-16 09:30'),          # в понедельник
    ({'weekday': 'fr', 'time': '18:00'}, '2026-11-13 18:00'),          # в пятницу — сегодня, время впереди
    ({'weekday': 'fr', 'time': '10:00'}, '2026-11-20 10:00'),          # в пятницу — время прошло, через неделю
    ({'date': '2027-01-15', 'time': '10:00'}, '2027-01-15 10:00'),     # названо число
    ({'date': '2027-01-15', 'day_offset': 3, 'time': '8.05'}, '2027-01-15 08:05'),  # число важнее
    ({'minutes_from_now': 120}, '2026-11-13 14:00'),                   # через два часа
    ({'minutes_from_now': 20}, '2026-11-13 12:20'),
    ({'day_offset': 7, 'time': '12:00'}, '2026-11-20 12:00'),          # через неделю
    ({'day_offset': 7, 'weekday': 'tu', 'time': '14:00'}, '2026-11-17 14:00'),  # «следующий вторник»
    ({'day_of_month': 25, 'time': '11:00'}, '2026-11-25 11:00'),       # 25 числа — в этом месяце
    ({'day_of_month': 5, 'time': '11:00'}, '2026-12-05 11:00'),        # 5-е прошло — в следующем
    ({'day_of_month': 13, 'time': '15:00'}, '2026-11-13 15:00'),       # сегодня, время впереди
    ({'day_of_month': 13, 'time': '10:00'}, '2026-12-13 10:00'),       # сегодня, время прошло
    ({'day_of_month': 31, 'time': '10:00'}, '2026-12-31 10:00'),       # в ноябре нет 31-го
])
def test_date_is_computed_from_parts(given, expected):
    reminder = resolve(parts(**given), PROFILE, NOW)
    assert isinstance(reminder, Reminder)
    assert local(reminder) == expected
    assert reminder.rrule is None and reminder.kind == 'custom' and reminder.id == 0
    assert reminder.user_id == 101 and reminder.text == 'Сверить кассу'


def test_time_is_local_to_the_profile_zone():
    far_east = UserProfile(user_id=101, timezone='Asia/Vladivostok')  # там уже 19:00
    reminder = resolve(parts(time='20:00'), far_east, NOW)
    assert reminder.next_run_at == datetime(2026, 11, 13, 10, 0, tzinfo=timezone.utc)
    assert local(resolve(parts(time='20:00'), PROFILE, NOW)) == '2026-11-13 20:00'


def test_unknown_zone_falls_back_to_moscow():
    odd = UserProfile(user_id=101, timezone='Mars/Olympus')
    assert local(resolve(parts(time='15:00'), odd, NOW)) == '2026-11-13 15:00'


@pytest.mark.parametrize(('given', 'rule', 'first'), [
    ({'repeat': 'daily', 'time': '09:00'}, 'FREQ=DAILY', '2026-11-14 09:00'),
    ({'repeat': 'weekly', 'weekday': 'mo', 'time': '09:30'}, 'FREQ=WEEKLY;BYDAY=MO', '2026-11-16 09:30'),
    ({'repeat': 'weekly', 'time': '15:00'}, 'FREQ=WEEKLY;BYDAY=FR', '2026-11-13 15:00'),
    ({'repeat': 'monthly', 'date': '2026-11-28', 'time': '10:00'}, 'FREQ=MONTHLY;BYMONTHDAY=28', '2026-11-28 10:00'),
    ({'repeat': 'yearly', 'date': '2027-04-28', 'time': '10:00'}, 'FREQ=YEARLY', '2027-04-28 10:00'),
])
def test_repeat_becomes_a_rule_that_matches_the_first_date(given, rule, first):
    reminder = resolve(parts(**given), PROFILE, NOW)
    assert reminder.rrule == rule and local(reminder) == first
    # Правило принимается расписанием: первая дата — действительно его событие.
    assert canonicalize(rule, reminder.next_run_at, 'Europe/Moscow').startswith('DTSTART;TZID=')


@pytest.mark.parametrize(('given', 'question'), [
    ({}, ASK_TIME),                                         # «напомни сверить кассу»
    ({'day_offset': 1}, ASK_TIME),                          # «завтра» без времени
    ({'time': '25:00'}, ASK_TIME),                          # невозможное время
    ({'time': 'утром'}, ASK_TIME),
    ({'day_offset': 0, 'time': '09:00'}, ASK_PAST),         # сегодня в 9, а уже 12
    ({'date': '2026-01-10', 'time': '10:00'}, ASK_PAST),    # число в прошлом
    ({'date': '2031-01-10', 'time': '10:00'}, ASK_AGAIN),   # слишком далеко
    ({'time': '15:00', 'text': '  '}, ASK_TEXT),            # непонятно, о чём
    ({'minutes_from_now': 30, 'text': ''}, ASK_TEXT),
])
def test_missing_or_impossible_parts_become_a_question(given, question):
    assert resolve(parts(**given), PROFILE, NOW) == question


@pytest.mark.parametrize('garbage', [
    {'time': None, 'date': 5, 'day_offset': 'завтра', 'weekday': 7, 'minutes_from_now': True},
    {'date': '2026-13-45', 'time': '15:00', 'day_offset': -5, 'minutes_from_now': 10**9},
    {'repeat': 'иногда', 'time': '15:00', 'weekday': 'someday'},
    {'day_of_month': 99, 'time': '15:00'},
    {'day_of_month': 'пятое', 'time': '15:00'},
])
def test_malformed_model_output_never_crashes(garbage):
    result = resolve(parts(**garbage), PROFILE, NOW)
    assert isinstance(result, (Reminder, str))


def test_text_starts_with_a_capital_letter():
    assert resolve(parts(text='сверить  кассу', time='15:00'), PROFILE, NOW).text == 'Сверить кассу'


@pytest.mark.parametrize(('source', 'rule'), [
    ('напомни в пятницу в 18:00 позвонить поставщику', None),
    ('поставь напоминание на следующий вторник в 14', None),
    ('напоминай каждую пятницу в 18:00 звонить поставщику', 'FREQ=WEEKLY;BYDAY=FR'),
    ('напоминай по пятницам в 18:00 звонить поставщику', 'FREQ=WEEKLY;BYDAY=FR'),
    ('', 'FREQ=WEEKLY;BYDAY=FR'),  # исходный текст не передан — верим разбору
])
def test_repeat_needs_explicit_words_in_the_message(source, rule):
    data = parts(weekday='fr', time='18:00', repeat='weekly')
    assert resolve(data, PROFILE, NOW, source).rrule == rule


def test_monthly_repeat_uses_the_named_day():
    data = parts(day_of_month=25, time='11:00', repeat='monthly')
    reminder = resolve(data, PROFILE, NOW, 'каждый месяц 25 числа в 11 напоминай платить аренду')
    assert local(reminder) == '2026-11-25 11:00' and reminder.rrule == 'FREQ=MONTHLY;BYMONTHDAY=25'


def test_not_a_reminder():
    for data in (parts(is_reminder=False), parts(is_reminder='да'), {}):
        with pytest.raises(NotReminder):
            resolve(data, PROFILE, NOW)


def test_clock_change_is_refused_politely():
    berlin = UserProfile(user_id=101, timezone='Europe/Berlin')
    answer = resolve(parts(date='2027-03-28', time='02:30'), berlin, NOW)  # этого часа нет
    assert isinstance(answer, str) and 'переводят часы' in answer


@pytest.mark.parametrize(('text', 'expected'), [
    ('Напомни завтра в 10 сверить кассу', True),
    ('напоминай каждый понедельник проверять заказы', True),
    ('поставь напоминание на пятницу', True),
    ('как выбрать налоговый режим', False),
    ('что такое франшиза', False),
])
def test_cheap_check_before_calling_the_model(text, expected):
    assert looks_like_reminder(text) is expected


async def test_model_gets_message_and_local_time_but_nothing_about_the_user(monkeypatch):
    calls = []

    async def fake_complete(messages, json_schema=None):
        calls.append((messages, json_schema))
        return parts(day_offset=1, time='10:00')

    monkeypatch.setattr(parse.llm, 'complete', fake_complete)
    profile = UserProfile(user_id=987654321, industry='Общепит', tax_regime='usn_income')
    reminder = await parse_request('напомни завтра в 10 сверить кассу', profile, NOW)
    assert local(reminder) == '2026-11-14 10:00' and reminder.user_id == 987654321
    (messages, schema), = calls
    assert schema == parse.SCHEMA
    sent = '\n'.join(message['content'] for message in messages)
    assert 'напомни завтра в 10 сверить кассу' in sent
    assert '2026-11-13 12:00, пятница' in sent
    for private in ('987654321', 'Общепит', 'usn_income'):
        assert private not in sent


async def test_model_answer_that_is_not_an_object_means_not_a_reminder(monkeypatch):
    async def fake_complete(messages, json_schema=None):
        return 'просто текст'

    monkeypatch.setattr(parse.llm, 'complete', fake_complete)
    with pytest.raises(NotReminder):
        await parse_request('напомни', PROFILE, NOW)


def test_confirmation_names_weekday_and_repeat():
    run_at = datetime(2026, 11, 16, 6, 30, tzinfo=timezone.utc)
    once = confirmation('Проверить заказы', run_at, 'Europe/Moscow')
    assert '16.11.2026 09:30, понедельник, московское время' in once and 'Повтор' not in once
    weekly = confirmation('Проверить заказы', run_at, 'Europe/Moscow', 'FREQ=WEEKLY;BYDAY=MO')
    assert 'Первый раз: 16.11.2026 09:30, понедельник' in weekly
    assert 'Повтор: каждую неделю' in weekly


def test_repeat_label_reads_short_and_canonical_rules():
    canonical = canonicalize('FREQ=DAILY', NOW, 'Europe/Moscow')
    assert repeat_label(canonical) == 'каждый день'
    assert repeat_label('FREQ=MONTHLY;BYMONTHDAY=28') == 'каждый месяц'
    assert repeat_label('RRULE:FREQ=YEARLY') == 'каждый год'
    assert repeat_label(None) is None and repeat_label('') is None
