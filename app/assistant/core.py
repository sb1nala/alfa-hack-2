"""Главная точка входа: сообщение пользователя -> ответ. Роль A."""

from app.kb import search as kb
from app.models import Answer, Material

MAX_SOURCES = 3
FOUND_TEXT = (
    "Нашёл в базе знаний «Альфа-Курса» материалы по вашему вопросу — они ниже. "
    "Развёрнутые ответы появятся, когда подключим языковую модель."
)
NOT_FOUND_TEXT = (
    "В базе знаний «Альфа-Курса» я не нашёл материалов по этому вопросу. "
    "Попробуйте сформулировать иначе или спросить о бизнесе: "
    "налоги, регистрация, продажи, сотрудники."
)


async def reply(user_id: int, text: str) -> Answer:
    """Пока без языковой модели: находит материалы по вопросу или честно говорит, что их нет."""
    sources: list[Material] = []
    for chunk in kb.search(text):
        if all(chunk.material.url != known.url for known in sources):
            sources.append(chunk.material)
        if len(sources) == MAX_SOURCES:
            break
    if not sources:
        return Answer(kind="no_answer", text=NOT_FOUND_TEXT)
    return Answer(kind="answer", text=FOUND_TEXT, sources=sources)
