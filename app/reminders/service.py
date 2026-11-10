"""Хранение и расписание напоминаний. Роль B."""

from datetime import datetime

from app.models import Reminder


def create(reminder: Reminder) -> Reminder:
    raise NotImplementedError


def list_for(user_id: int) -> list[Reminder]:
    raise NotImplementedError


def set_schedule(reminder_id: int, rrule: str | None, next_run_at: datetime) -> Reminder:
    raise NotImplementedError


def pause(reminder_id: int, active: bool) -> None:
    raise NotImplementedError


def delete(reminder_id: int) -> None:
    raise NotImplementedError


def due(now: datetime) -> list[Reminder]:
    """Активные напоминания, срок которых наступил. now — в UTC."""
    raise NotImplementedError
