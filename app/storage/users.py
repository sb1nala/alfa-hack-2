"""Idempotent Telegram user registration."""

from app.storage import db
from app.storage._common import ensure_user, validate_user_id


async def ensure(user_id: int) -> None:
    validate_user_id(user_id)
    async with db.pool().acquire() as connection:
        await ensure_user(connection, user_id)
