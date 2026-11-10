"""Чтение выгрузки кейса и сборка материалов и фрагментов. Роль A.

Устройство выгрузки — docs/PLAN.md, раздел 10.
"""

from pathlib import Path

from app.models import Chunk, Material


def load_materials(data_dir: Path) -> list[Material]:
    """Прочитать .backup и вернуть опубликованные материалы."""
    raise NotImplementedError


def build_chunks(data_dir: Path) -> list[Chunk]:
    """Собрать текст из блоков и нарезать на фрагменты. Платные уроки не включать."""
    raise NotImplementedError
