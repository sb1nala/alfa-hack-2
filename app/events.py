"""Журнал событий для метрик и UTM-метки в ссылках на платформу. Роль 3.

События отвечают на вопрос кейса «как бот возвращает на платформу и как это проверить»:
что бот показал, что напомнил и что пользователь отключил. Переход по ссылке бот не видит —
его считает платформа по UTM-меткам. Описание метрик — docs/product/metrics.md.
"""

import logging
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.storage import events as storage

log = logging.getLogger(__name__)

# Названия событий. Новое событие добавляем сюда и в docs/product/metrics.md.
BOT_STARTED = "bot_started"  # пользователь нажал /start
QUESTION_ASKED = "question_asked"  # payload: kind (answer | clarify | no_answer), sources
MATERIAL_SHOWN = "material_shown"  # payload: material_id, kind, access, place
REMINDER_CREATED = "reminder_created"  # payload: kind, recurring
REMINDER_SENT = "reminder_sent"  # payload: reminder_id, kind
REMINDER_CHANGED = "reminder_changed"  # payload: reminder_id, action (less_often | pause | resume)
REMINDER_DISABLED = "reminder_disabled"  # payload: reminder_id, kind
PROFILE_UPDATED = "profile_updated"  # payload: fields — только названия полей, без значений
PROFILE_RESET = "profile_reset"
BOT_BLOCKED = "bot_blocked"  # пользователь заблокировал бота: сигнал раздражения

EVENT_NAMES = frozenset({
    BOT_STARTED, QUESTION_ASKED, MATERIAL_SHOWN, REMINDER_CREATED, REMINDER_SENT,
    REMINDER_CHANGED, REMINDER_DISABLED, PROFILE_UPDATED, PROFILE_RESET, BOT_BLOCKED,
})

# В журнал не попадают тексты сообщений и сведения, по которым можно узнать человека.
PRIVATE_KEYS = frozenset({
    "text", "message", "query", "question", "username", "first_name", "last_name", "full_name",
    "name", "phone", "email",
})

UTM_SOURCE = "alfa_kurs_bot"
UTM_MEDIUM = "telegram"
PLATFORM_HOSTS = frozenset({"kurs.alfabank.ru"})
# Откуда ссылка: из ответа, из рекомендаций, из напоминания или из стартовой подборки.
CAMPAIGNS = frozenset({"answer", "recommendation", "reminder_learning", "reminder_tax", "starter"})
_UTM_VALUE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


async def log_event(user_id: int, name: str, payload: dict | None = None) -> None:
    """Записать событие. Сбой записи не ломает ответ пользователю: ошибка уходит в лог."""
    if name not in EVENT_NAMES:
        log.warning("Неизвестное событие %r: добавьте его в app/events.py", name)
    clean = {key: value for key, value in (payload or {}).items() if key not in PRIVATE_KEYS}
    try:
        await storage.append(user_id, name, clean)
    except Exception:  # метрики не должны ронять бота
        log.exception("Не удалось записать событие %s", name)


def tracked_url(url: str, campaign: str, content: str | None = None) -> str:
    """Добавить UTM-метки к ссылке на платформу.

    campaign — откуда ссылка (одно из CAMPAIGNS); content — вид или id материала.
    Чужие ссылки возвращаются как есть. В метки нельзя передавать ничего о пользователе.
    """
    if campaign not in CAMPAIGNS:
        raise ValueError(f"Неизвестная кампания {campaign!r}; допустимые: {sorted(CAMPAIGNS)}")
    if content is not None and not _UTM_VALUE.fullmatch(content):
        raise ValueError("utm_content: только латинские строчные буквы, цифры, «-» и «_»")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or parts.hostname not in PLATFORM_HOSTS:
        return url
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
    ]
    query += [("utm_source", UTM_SOURCE), ("utm_medium", UTM_MEDIUM), ("utm_campaign", campaign)]
    if content is not None:
        query.append(("utm_content", content))
    return urlunsplit(parts._replace(query=urlencode(query)))
