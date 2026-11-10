import pytest

from app.assistant.core import MAX_SOURCES, reply
from app.kb import search as kb
from app.kb.search import SearchIndex
from app.models import Chunk, Material


def chunk(material_id, text, url=None):
    material = Material(
        id=material_id,
        kind="article",
        access="open",
        title=f"Материал {material_id}",
        url=url or f"https://example.test/{material_id}/",
        summary="",
    )
    return Chunk(material=material, text=text, score=0.0)


@pytest.fixture(autouse=True)
def loaded_index():
    kb.set_index(
        SearchIndex(
            [
                *(chunk(f"p{n}", f"Патент для ИП: вариант {n}.") for n in range(5)),
                chunk("l1", "Касса и чеки: урок один.", url="https://example.test/course/"),
                chunk("l2", "Касса и чеки: урок два.", url="https://example.test/course/"),
            ]
        )
    )
    yield
    kb.set_index(None)


async def test_known_topic_gives_answer_with_limited_sources():
    answer = await reply(1, "патент для ИП")
    assert answer.kind == "answer"
    assert len(answer.sources) == MAX_SOURCES
    assert len({source.id for source in answer.sources}) == MAX_SOURCES


async def test_sources_with_the_same_link_are_shown_once():
    answer = await reply(1, "касса и чеки")
    assert [source.url for source in answer.sources] == ["https://example.test/course/"]


async def test_unknown_topic_is_admitted_honestly():
    answer = await reply(1, "рецепт борща")
    assert answer.kind == "no_answer"
    assert answer.sources == []
    assert "не нашёл" in answer.text
