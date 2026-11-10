"""Разбор просьбы «напомни…» в черновик напоминания. Роль B."""

from datetime import datetime

from app.models import Reminder, UserProfile


async def parse_request(text: str, profile: UserProfile, now: datetime) -> Reminder | str:
    """Вернуть черновик напоминания или строку с вопросом, что нужно уточнить."""
    raise NotImplementedError
