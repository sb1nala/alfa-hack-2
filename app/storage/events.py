"""Persist events, without product analytics or global JSON codecs."""

import json

from app.storage import db
from app.storage._common import ensure_user, require_text, validate_user_id


def _serialize_payload(payload: dict | None) -> str:
    if payload is not None and not isinstance(payload, dict):
        raise TypeError("payload must be a dict or None")
    try:
        # Reject NaN/Infinity, unsupported values and cycles before any DB writes.
        return json.dumps({} if payload is None else payload, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError) as error:
        raise ValueError("payload must be a JSON-serializable object") from error


async def append(user_id: int, name: str, payload: dict | None = None) -> int:
    validate_user_id(user_id)
    require_text(name, "name")
    serialized = _serialize_payload(payload)
    async with db.pool().acquire() as connection:
        async with connection.transaction():
            await ensure_user(connection, user_id)
            return await connection.fetchval(
                "INSERT INTO events (user_id, name, payload) VALUES ($1, $2, $3::jsonb) RETURNING id",
                user_id, name, serialized,
            )
