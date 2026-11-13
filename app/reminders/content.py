"""Safe Telegram rendering shared by confirmation and delivery."""

from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

MAX_ESCAPED_TEXT = 3400  # Conservative: reserve service lines even before HTML parsing.


def units(text: str) -> int:
    return len(text.encode('utf-16-le')) // 2


def checked_text(text: str) -> str:
    text = text.strip()
    if not text:
        raise ValueError('Добавьте текст напоминания.')
    try:
        length = units(escape(text))
    except UnicodeError as error:
        raise ValueError('В тексте есть недопустимые символы.') from error
    if '\x00' in text:
        raise ValueError('В тексте есть недопустимые символы.')
    if length > MAX_ESCAPED_TEXT:
        raise ValueError('Текст слишком длинный. Сократите его (до 3400 знаков с учётом экранирования).')
    return text


def confirmation(text: str, run_at: datetime, zone: str) -> str:
    local = run_at.astimezone(ZoneInfo(zone))
    return (f'<b>Создать напоминание?</b>\n{escape(checked_text(text))}\n\n'
            f'{local:%Y-%m-%d %H:%M} ({escape(zone)})\n'
            'Подтверждение действует 15 минут.')


def delivery(text: str) -> str:
    return '<b>Напоминание</b>\n' + escape(checked_text(text))
