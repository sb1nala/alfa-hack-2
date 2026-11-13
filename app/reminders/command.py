"""Strict one-off command parsing; no natural-language or recurrence UI."""

import re
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil import tz

from app.reminders.content import checked_text
from app.reminders.schedule import aware_utc

USAGE = ('Используйте /remind ГГГГ-ММ-ДД ЧЧ:ММ Текст напоминания.\n'
         'Например: /remind 2027-01-15 10:00 Проверить план.\n'
         'Дата и время — по вашему местному времени. Если я не знаю ваш город, считаю по московскому.')


def parse_command(text: str, zone: str, now: datetime) -> tuple[str, datetime]:
    match = re.fullmatch(
        r'/remind(?:@[A-Za-z0-9_]+)?\s+([0-9]{4}-[0-9]{2}-[0-9]{2}) '
        r'([0-9]{2}:[0-9]{2})\s+(.+)', text, re.DOTALL,
    )
    if match is None:
        raise ValueError(USAGE)
    try:
        body = checked_text(match[3])
    except ValueError as error:
        raise ValueError(f'{error}\n{USAGE}') from error
    try:
        local = datetime.strptime(f'{match[1]} {match[2]}', '%Y-%m-%d %H:%M').replace(
            tzinfo=ZoneInfo(zone))
    except (ValueError, ZoneInfoNotFoundError) as error:
        raise ValueError('Проверьте дату, время и часовой пояс.\n' + USAGE) from error
    if not tz.datetime_exists(local) or tz.datetime_ambiguous(local):
        raise ValueError('Это время неоднозначно или не существует из-за перевода часов. '
                         'Выберите другое время.\n' + USAGE)
    instant = aware_utc(local)
    if instant <= aware_utc(now):
        raise ValueError('Укажите дату и время в будущем.\n' + USAGE)
    return body, instant
