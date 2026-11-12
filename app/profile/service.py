"""Профиль бизнеса пользователя. Роль 3.

Профиль хранится в PostgreSQL через app.storage.profiles. Сведения из сообщений достаёт
app.profile.extract — по правилам, без обращения к LLM, поэтому текст пользователя никуда не уходит.
"""

from dataclasses import asdict

from app.models import Material, UserProfile
from app.profile.extract import Vocabulary, extract
from app.storage import profiles as storage

_vocabulary = Vocabulary()


def set_vocabulary(materials: list[Material]) -> None:
    """Передать материалы базы знаний: их отрасли и теги-задачи станут словарём профиля.

    Без словаря отрасль и задача записываются своими общими названиями («Общепит», «Поиск клиентов»).
    """
    global _vocabulary
    industries: dict[str, None] = {}
    goals: dict[str, None] = {}
    for material in materials:
        industries.update(dict.fromkeys(material.industries))
        goals.update(dict.fromkeys(material.tags))
    _vocabulary = Vocabulary(industries=tuple(industries), goals=tuple(goals))


async def get(user_id: int) -> UserProfile:
    """Профиль пользователя; если о нём ничего не известно — пустой, это не ошибка."""
    return await storage.get(user_id)


async def update_from_message(user_id: int, text: str) -> UserProfile:
    """Достать из сообщения сведения о бизнесе и сохранить в профиль.

    Записываем только новое и изменившееся; того, чего в сообщении нет, не трогаем.
    """
    profile = await storage.get(user_id)
    current = asdict(profile)
    changes = {
        name: value for name, value in extract(text, _vocabulary).items() if current[name] != value
    }
    if not changes:
        return profile
    return await storage.update(user_id, changes)


async def reset(user_id: int) -> None:
    """Забыть всё о бизнесе пользователя. Напоминания и история остаются."""
    await storage.reset(user_id)
