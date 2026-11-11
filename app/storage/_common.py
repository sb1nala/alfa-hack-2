"""Small shared storage primitives; no connection lifecycle of their own."""

import asyncpg


def validate_user_id(user_id: int) -> None:
    if type(user_id) is not int:
        raise TypeError("user_id must be an integer")
    if not -(2**63) <= user_id < 2**63:
        raise ValueError("user_id must fit PostgreSQL BIGINT")


def require_text(value: str, field: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    if not value.strip():
        raise ValueError(f"{field} must not be blank")


async def ensure_user(connection: asyncpg.Connection, user_id: int) -> None:
    await connection.execute(
        "INSERT INTO users (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING",
        user_id,
    )
