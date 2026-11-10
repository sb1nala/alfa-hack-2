"""Рекомендации материалов. Роль C."""

from app.models import Recommendation, UserProfile


def recommend(
    profile: UserProfile, query: str, exclude_ids: set[str], k: int = 3
) -> list[Recommendation]:
    """До k материалов под вопрос и профиль; для платных — только польза и ссылка."""
    raise NotImplementedError
