"""Per-user material exposure history."""

from app.storage import db
from app.storage._common import ensure_user, require_text, validate_user_id


async def mark_shown(user_id: int, material_id: str) -> None:
    validate_user_id(user_id)
    require_text(material_id, "material_id")
    async with db.pool().acquire() as connection:
        async with connection.transaction():
            await ensure_user(connection, user_id)
            await connection.execute(
                "INSERT INTO shown_materials (user_id, material_id) VALUES ($1, $2) "
                "ON CONFLICT (user_id, material_id) DO UPDATE SET shown_at = clock_timestamp()",
                user_id, material_id,
            )


async def list_ids(user_id: int) -> set[str]:
    validate_user_id(user_id)
    rows = await db.pool().fetch(
        "SELECT material_id FROM shown_materials WHERE user_id = $1", user_id,
    )
    return {row["material_id"] for row in rows}
