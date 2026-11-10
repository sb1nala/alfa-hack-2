"""Проверки на настоящей выгрузке. Пропускаются, если в data/ нет файла .backup."""

from pathlib import Path

import pytest

from app.kb.ingest import DataError, find_backup, load_corpus
from app.kb.search import SearchIndex

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

try:
    find_backup(DATA_DIR)
except DataError:
    pytestmark = pytest.mark.skip(reason="нет выгрузки в data/")


@pytest.fixture(scope="module")
def corpus():
    return load_corpus(DATA_DIR)


@pytest.fixture(scope="module")
def index(corpus):
    return SearchIndex(corpus.chunks)


def test_all_kinds_of_materials_are_loaded(corpus):
    kinds = {material.kind for material in corpus.materials}
    assert kinds == {
        "article",
        "course",
        "lesson",
        "document",
        "glossary",
        "webinar",
        "podcast",
        "calculator",
    }
    assert len(corpus.materials) > 1500
    assert len(corpus.chunks) > 5000


def test_no_lesson_belongs_to_a_paid_course(corpus):
    paid_course_urls = {m.url for m in corpus.materials if m.kind == "course" and m.is_paid}
    assert paid_course_urls
    lessons = [m for m in corpus.materials if m.kind == "lesson"]
    assert lessons
    assert all(not m.is_paid and m.url not in paid_course_urls for m in lessons)


def test_every_material_has_title_and_platform_url(corpus):
    for material in corpus.materials:
        assert material.title
        assert material.url.startswith("https://kurs.alfabank.ru/")
        assert material.url.endswith("/") and "None" not in material.url


def test_chunks_fit_the_limit(corpus):
    assert max(len(chunk.text) for chunk in corpus.chunks) <= 1000


@pytest.mark.parametrize(
    "question",
    [
        "как выбрать налоговый режим для ИП",
        "что такое франшиза",
        "как зарегистрировать ООО",
        "шаблон трудового договора",
        "как продавать на маркетплейсах",
    ],
)
def test_business_questions_find_materials(index, question):
    assert index.search(question)


@pytest.mark.parametrize(
    "question",
    ["рецепт борща со свёклой", "расписание электричек до Твери", "как вылечить насморк"],
)
def test_off_topic_questions_find_nothing(index, question):
    # Поиск по словам отсекает только вопросы, слов которых в базе нет. Вопрос не по теме
    # со случайно совпавшими словами должна отсеивать языковая модель на шаге ответа.
    assert index.search(question) == []
