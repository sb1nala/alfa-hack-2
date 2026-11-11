"""Хранение и расписание напоминаний. Роль 2."""

from datetime import datetime

from app.models import Reminder


async def create(reminder: Reminder) -> Reminder:
    raise NotImplementedError


async def list_for(user_id: int) -> list[Reminder]:
    raise NotImplementedError


async def set_schedule(reminder_id: int, rrule: str | None, next_run_at: datetime) -> Reminder:
    raise NotImplementedError


async def pause(reminder_id: int, active: bool) -> None:
    raise NotImplementedError


async def delete(reminder_id: int) -> None:
    raise NotImplementedError


async def due(now: datetime) -> list[Reminder]:
    """Активные напоминания, срок которых наступил. now — в UTC."""
    raise NotImplementedError
