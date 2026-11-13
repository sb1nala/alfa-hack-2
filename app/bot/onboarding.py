"""Стартовая подборка и безопасный переход со старых кнопок.

Новый диалог и хранение состояния — в bot.profile/storage.profile_ui.
Старые ob:* не несут владельца/версии, поэтому менять профиль через них нельзя.
"""

import logging
from html import escape

from aiogram.types import CallbackQuery, Message

from app import events
from app.models import UserProfile
from app.recs import service as recs

log = logging.getLogger(__name__)

STARTER_COUNT = 3

STAGE_QUERIES = {
    "idea": "как открыть свой бизнес",
    "starting": "первые клиенты для бизнеса",
    "running": "как увеличить продажи",
    "growing": "как масштабировать бизнес",
}


def starter_pack(profile: UserProfile) -> str:
    """Стартовая подборка для нового пользователя: популярное, с учётом того, что уже известно."""
    # Если стадия известна, подбираем под неё; когда не вышло или ничего не нашлось — популярное.
    items = []
    query = STAGE_QUERIES.get(profile.business_stage or "", "")
    if query:
        try:
            items = recs.recommend(profile, query, set(), STARTER_COUNT)
        except Exception as error:
            log.warning("stage starter pack failed: %s", type(error).__name__)
    try:
        if not items:
            items = recs.recommend(profile, "", set(), STARTER_COUNT)
    except Exception as error:  # подборка — дополнение к знакомству
        log.warning("starter pack failed: %s", type(error).__name__)
        return ""
    if not items:
        return ""
    lines = []
    used = 0
    for item in items[:STARTER_COUNT]:
        url = events.tracked_url(item.material.url, "starter", item.material.kind)
        mark = " (платный)" if item.material.is_paid else ""
        title = item.material.title[:100]
        line = f'• <a href="{escape(url, quote=True)}">{escape(title)}</a>{mark}'
        size = len(line.encode('utf-16-le')) // 2
        if used + size <= 1800:
            lines.append(line)
            used += size
    return "С чего можно начать:\n" + "\n".join(lines) if lines else ""


async def start(message: Message) -> None:
    from app.bot.profile import open_screen

    await open_screen(message, 'start')


async def show_profile(message: Message) -> None:
    from app.bot.profile import open_screen

    await open_screen(message, 'profile')


async def on_callback(callback: CallbackQuery) -> None:
    await callback.answer(
        'Эта кнопка устарела. Откройте /start или /profile для новой настройки.', show_alert=True)
