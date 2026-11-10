"""Поиск по базе знаний. Роль A."""

from app.models import Chunk


def search(query: str, k: int = 6) -> list[Chunk]:
    """Вернуть до k самых подходящих фрагментов, лучший первым."""
    raise NotImplementedError
