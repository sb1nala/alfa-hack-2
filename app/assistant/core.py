"""Главная точка входа: сообщение пользователя -> ответ. Роль A."""

from app.models import Answer


async def reply(user_id: int, text: str) -> Answer:
    """Пока заглушка: подтверждает, что каркас работает."""
    return Answer(
        kind="answer",
        text=f"Каркас работает, ответы по базе знаний пока не подключены. Вы написали: {text}",
    )
