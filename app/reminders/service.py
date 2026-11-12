"""Internal reminder API. Callers must check ownership before mutations.

Validation uses TypeError for wrong types and ValueError for invalid values.
No operation implicitly advances a schedule or checks the system clock.
"""

from dataclasses import replace
from datetime import datetime

from app.models import Reminder
from app.reminders.schedule import aware_utc, canonicalize
from app.storage import profiles, reminders
from app.storage._common import require_text, validate_user_id


class ReminderNotFoundError(LookupError):
    """The requested reminder does not exist."""


def _id(reminder_id: int) -> None:
    validate_user_id(reminder_id)
    if reminder_id <= 0:
        raise ValueError("reminder_id must be positive")


def _active(active: bool) -> None:
    if type(active) is not bool:
        raise TypeError("active must be bool")


async def _schedule(value: str | None, instant: datetime, user_id: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("rrule must be a string or None")
    # Canonical input never consults the current profile.
    zone = "Europe/Moscow" if value.startswith("DTSTART") else (await profiles.get(user_id)).timezone
    return canonicalize(value, instant, zone)


async def create(reminder: Reminder) -> Reminder:
    if not isinstance(reminder, Reminder):
        raise TypeError("reminder must be Reminder")
    if type(reminder.id) is not int:
        raise TypeError("draft id must be integer zero")
    if reminder.id != 0:
        raise ValueError("create requires draft id=0")
    validate_user_id(reminder.user_id)
    require_text(reminder.kind, "kind")
    if reminder.kind not in {"custom", "learning", "tax"}:
        raise ValueError("kind must be custom, learning or tax")
    require_text(reminder.text, "text")
    _active(reminder.active)
    instant = aware_utc(reminder.next_run_at)
    rule = await _schedule(reminder.rrule, instant, reminder.user_id)
    return await reminders.insert(replace(reminder, next_run_at=instant, rrule=rule))


async def list_for(user_id: int) -> list[Reminder]:
    validate_user_id(user_id)
    return await reminders.list_for(user_id)


async def set_schedule(reminder_id: int, rrule: str | None, next_run_at: datetime) -> Reminder:
    _id(reminder_id)
    instant = aware_utc(next_run_at)
    current = await reminders.get(reminder_id)
    if current is None:
        raise ReminderNotFoundError(f"Reminder {reminder_id} not found")
    rule = await _schedule(rrule, instant, current.user_id)
    saved = await reminders.set_schedule(reminder_id, rule, instant)
    if saved is None:
        raise ReminderNotFoundError(f"Reminder {reminder_id} not found")
    return saved


async def pause(reminder_id: int, active: bool) -> None:
    _id(reminder_id)
    _active(active)
    if not await reminders.pause(reminder_id, active):
        raise ReminderNotFoundError(f"Reminder {reminder_id} not found")


async def delete(reminder_id: int) -> None:
    _id(reminder_id)
    await reminders.delete(reminder_id)


async def due(now: datetime) -> list[Reminder]:
    return await reminders.due(aware_utc(now))
