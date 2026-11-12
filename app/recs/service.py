"""Рекомендации материалов. Роль 3.

Отбор: материалы, найденные поиском по вопросу, плюс материалы под текущую задачу из профиля.
Порядок: совпадение с профилем (отрасль, опыт, задача), затем близость к вопросу, затем популярность.
Строка «почему вам» собирается из профиля и вида материала и никогда не берётся из текста
материала; для платных — только первое предложение описания.
"""

import re
from dataclasses import dataclass

from app.kb import search as kb
from app.models import Material, Recommendation, UserProfile

SEARCH_K = 12
GOAL_SEARCH_K = 6
MAX_REASON_CHARS = 160

BEGINNER_LABEL = "без опыта"
EXPERIENCED_LABEL = "с опытом"
SERVICE_LABELS = frozenset({BEGINNER_LABEL, EXPERIENCED_LABEL, "new", "популярное"})
EARLY_STAGES = frozenset({"idea", "starting"})

KIND_REASONS = {
    "article": "Статья по теме вашего вопроса.",
    "course": "Курс по теме вашего вопроса.",
    "lesson": "Урок по теме вашего вопроса.",
    "document": "Готовый шаблон по теме вашего вопроса.",
    "glossary": "Объяснение термина простыми словами.",
    "webinar": "Запись вебинара по теме вашего вопроса.",
    "podcast": "Подкаст по теме вашего вопроса.",
    "calculator": "Калькулятор: можно посчитать на своих цифрах.",
}
STARTER_REASON = "Популярный материал у предпринимателей на «Альфа-Курсе»."
PAID_REASON = "Платный материал по этой теме, подробности — по ссылке."
BEGINNER_REASON = "Подходит тем, кто только начинает."
EXPERIENCED_REASON = "Для предпринимателей с опытом."

_SENTENCE_END = re.compile(r"(?<=[.!?…])\s")

_catalog: list[Material] = []


def set_catalog(materials: list[Material]) -> None:
    """Передать все материалы базы знаний (app.kb.ingest.Corpus.materials).

    Нужен для стартовой подборки и для отбора по тегу-задаче. Без него рекомендации строятся
    только из результатов поиска.
    """
    global _catalog
    _catalog = list(materials)


@dataclass
class _Candidate:
    material: Material
    search_rank: int | None = None  # место в поиске по вопросу, None — найден не по вопросу
    goal_tag: str | None = None  # тег материала, совпавший с задачей из профиля


def _norm(text: str) -> str:
    return " ".join(text.lower().replace("ё", "е").split())


def _stems(text: str) -> frozenset[str]:
    return frozenset(kb.tokenize(text))


def _similar(left: str, right: str) -> bool:
    """Одно и то же понятие в разных формах: «общепит» — «Общепит», «продажи» — «Увеличение продаж»."""
    a, b = _stems(left), _stems(right)
    if not a or not b:
        return False
    common = len(a & b)
    return common == min(len(a), len(b)) or common * 2 >= max(len(a), len(b))


def _experience(profile: UserProfile) -> str | None:
    if profile.experience:
        return profile.experience
    return "beginner" if profile.business_stage in EARLY_STAGES else None


def _labels(material: Material) -> set[str]:
    return {_norm(label) for label in material.industries}


def _matched_industry(material: Material, profile: UserProfile) -> str | None:
    if not profile.industry:
        return None
    for label in material.industries:
        if _norm(label) not in SERVICE_LABELS and _similar(label, profile.industry):
            return label
    return None


def _experience_points(material: Material, experience: str | None) -> int:
    """+1 — материал для такого же опыта, −1 — для противоположного, 0 — метки нет."""
    if experience is None:
        return 0
    labels = _labels(material)
    own, other = (
        (BEGINNER_LABEL, EXPERIENCED_LABEL)
        if experience == "beginner"
        else (EXPERIENCED_LABEL, BEGINNER_LABEL)
    )
    if own in labels:
        return 1
    return -1 if other in labels else 0


def _goal_tag(material: Material, goal: str | None) -> str | None:
    if not goal:
        return None
    return next((tag for tag in material.tags if _similar(tag, goal)), None)


def _first_sentence(text: str) -> str:
    sentence = _SENTENCE_END.split(" ".join(text.split()), maxsplit=1)[0]
    if len(sentence) <= MAX_REASON_CHARS:
        return sentence
    return sentence[: MAX_REASON_CHARS - 1].rsplit(" ", 1)[0] + "…"


def _reason(candidate: _Candidate, profile: UserProfile) -> str:
    material = candidate.material
    if material.is_paid:
        return _first_sentence(material.summary) or PAID_REASON
    industry = _matched_industry(material, profile)
    if industry:
        return f"Подходит для вашей сферы: {industry}."
    if candidate.goal_tag:
        return f"Помогает с вашей задачей: {candidate.goal_tag}."
    if _experience_points(material, _experience(profile)) > 0:
        return BEGINNER_REASON if _experience(profile) == "beginner" else EXPERIENCED_REASON
    if candidate.search_rank is None:
        return STARTER_REASON
    return KIND_REASONS[material.kind]


def _collect(profile: UserProfile, query: str) -> list[_Candidate]:
    """Кандидаты без повторов: сначала найденные по вопросу, затем под задачу из профиля."""
    found: dict[str, _Candidate] = {}

    def add(material: Material, search_rank: int | None = None) -> None:
        if material.id not in found:
            found[material.id] = _Candidate(material, search_rank)

    if query.strip():
        for chunk in kb.search(query, k=SEARCH_K):
            add(chunk.material, search_rank=len(found))
    goal = profile.current_goal
    if goal and goal.strip():
        for material in _catalog:
            if _goal_tag(material, goal):
                add(material)
        for chunk in kb.search(goal, k=GOAL_SEARCH_K):
            if _goal_tag(chunk.material, goal):
                add(chunk.material)
    for candidate in found.values():
        candidate.goal_tag = _goal_tag(candidate.material, goal)
    return list(found.values())


def _starter_pack(profile: UserProfile) -> list[_Candidate]:
    """Общая подборка, когда нет ни вопроса, ни задачи: популярные открытые материалы."""
    experience = _experience(profile)
    return [
        _Candidate(material)
        for material in _catalog
        if not material.is_paid and _experience_points(material, experience) >= 0
    ]


def recommend(
    profile: UserProfile, query: str, exclude_ids: set[str], k: int = 3
) -> list[Recommendation]:
    """До k материалов под вопрос и профиль; для платных — только польза и ссылка."""
    if k <= 0:
        return []
    candidates = _collect(profile, query)
    if not candidates and not query.strip():
        candidates = _starter_pack(profile)
    experience = _experience(profile)

    def order(candidate: _Candidate) -> tuple:
        material = candidate.material
        profile_points = (
            (1 if _matched_industry(material, profile) else 0)
            + _experience_points(material, experience)
            + (1 if candidate.goal_tag else 0)
        )
        rank = candidate.search_rank if candidate.search_rank is not None else SEARCH_K
        return (-profile_points, rank, -material.popularity, material.id)

    result: list[Recommendation] = []
    seen_urls: set[str] = set()
    for candidate in sorted(candidates, key=order):
        material = candidate.material
        if material.id in exclude_ids or material.url in seen_urls:
            continue
        seen_urls.add(material.url)
        result.append(Recommendation(material=material, reason=_reason(candidate, profile)))
        if len(result) == k:
            break
    return result
