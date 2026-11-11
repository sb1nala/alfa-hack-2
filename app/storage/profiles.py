"""Partial profile updates without overwriting concurrent independent changes."""

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.models import UserProfile
from app.storage import db
from app.storage._common import ensure_user, validate_user_id

# SQL identifiers come exclusively from this fixed mapping, never from input.
_COLUMNS = {name: name for name in (
    "business_stage", "industry", "legal_form", "tax_regime", "experience",
    "current_goal", "timezone",
)}
_CHOICES = {
    "business_stage": {"idea", "starting", "running", "growing"},
    "legal_form": {"ip", "ooo", "self_employed"},
    "tax_regime": {"usn_income", "usn_income_expense", "ausn", "psn", "npd", "osno"},
    "experience": {"beginner", "experienced"},
}


def _validate_changes(changes: dict[str, str | None]) -> dict[str, str | None]:
    if not isinstance(changes, dict):
        raise TypeError("changes must be a dict")
    changes = changes.copy()
    for name, value in changes.items():
        if name not in _COLUMNS:
            raise ValueError(f"Unknown or immutable profile field: {name}")
        if value is not None and not isinstance(value, str):
            raise TypeError(f"{name} must be a string or None")
        if name == "timezone":
            if value is None:
                raise ValueError("timezone cannot be None")
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError) as error:
                raise ValueError("timezone must be an existing IANA zone") from error
        elif value is not None and name in _CHOICES and value not in _CHOICES[name]:
            raise ValueError(f"Invalid {name}; expected one of {sorted(_CHOICES[name])}")
    return changes


async def get(user_id: int) -> UserProfile:
    """Read without registering a user or persisting a default profile."""
    validate_user_id(user_id)
    row = await db.pool().fetchrow("SELECT * FROM profiles WHERE user_id = $1", user_id)
    return UserProfile(**dict(row)) if row is not None else UserProfile(user_id=user_id)


async def update(user_id: int, changes: dict[str, str | None]) -> UserProfile:
    """Validate before writing; None clears nullable fields, omission preserves them."""
    validate_user_id(user_id)
    changes = _validate_changes(changes)
    if not changes:
        return await get(user_id)
    columns = [_COLUMNS[name] for name in changes]
    placeholders = ", ".join(f"${i}" for i in range(1, len(columns) + 2))
    assignments = ", ".join(f"{column} = EXCLUDED.{column}" for column in columns)
    query = (
        f"INSERT INTO profiles (user_id, {', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT (user_id) DO UPDATE SET {assignments} RETURNING *"
    )
    async with db.pool().acquire() as connection:
        async with connection.transaction():
            await ensure_user(connection, user_id)
            row = await connection.fetchrow(query, user_id, *changes.values())
    return UserProfile(**dict(row))


async def reset(user_id: int) -> None:
    """Delete only the profile; retain the user, reminders and history."""
    validate_user_id(user_id)
    await db.pool().execute("DELETE FROM profiles WHERE user_id = $1", user_id)
