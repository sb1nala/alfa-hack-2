"""Профиль бизнеса пользователя. Роль 3."""

from app.models import UserProfile


async def get(user_id: int) -> UserProfile:
    raise NotImplementedError


async def update_from_message(user_id: int, text: str) -> UserProfile:
    """Достать из сообщения сведения о бизнесе и сохранить в профиль."""
    raise NotImplementedError


async def reset(user_id: int) -> None:
    raise NotImplementedError
