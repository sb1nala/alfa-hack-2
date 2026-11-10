import pytest

from app.kb import search as kb
from app.kb.search import SearchIndex, tokenize
from app.models import Chunk, Material


def chunk(material_id, title, text):
    material = Material(
        id=material_id,
        kind="article",
        access="open",
        title=title,
        url=f"https://example.test/{material_id}/",
        summary="",
    )
    return Chunk(material=material, text=text, score=0.0)


@pytest.fixture
def index():
    return SearchIndex(
        [
            chunk("usn", "Упрощённая система", "Налог по упрощёнке платят раз в квартал."),
            chunk("ip", "Регистрация ИП", "Чтобы открыть ИП, подайте заявление в налоговую."),
            chunk("hr", "Наём сотрудников", "Трудовой договор заключают в письменной форме."),
            chunk("mk", "Продвижение", "Реклама в соцсетях помогает найти клиентов."),
        ]
    )


def test_tokenize_lowercases_drops_stop_words_and_stems():
    assert tokenize("Как платить НАЛОГИ?") == tokenize("платит налог")
    assert tokenize("и в на что") == []
    assert tokenize("Ёлка") == tokenize("елка")


def test_finds_chunk_by_another_word_form(index):
    [found] = index.search("трудовые договоры с сотрудниками")
    assert found.material.id == "hr"
    assert found.score > 0


def test_title_words_are_searchable(index):
    assert index.search("продвижение")[0].material.id == "mk"


def test_best_match_comes_first(index):
    extra = chunk("usn2", "Налоги", "Налог налог налог: всё про налог на упрощёнке.")
    ranked = SearchIndex([*index._chunks, extra]).search("налог на упрощёнке", k=3)
    assert ranked[0].material.id in {"usn", "usn2"}
    assert [c.score for c in ranked] == sorted((c.score for c in ranked), reverse=True)


def test_off_topic_question_finds_nothing(index):
    assert index.search("рецепт борща со свёклой") == []


def test_question_with_one_known_common_word_and_rare_unknown_words_finds_nothing(index):
    assert index.search("налог на добычу марсианского реголита") == []


def test_empty_and_stop_word_only_queries_find_nothing(index):
    assert index.search("") == []
    assert index.search("а что и как?") == []
    assert SearchIndex([]).search("налог") == []


def test_k_limits_results_and_original_chunks_are_not_modified(index):
    assert len(index.search("подать заявление в налоговую", k=1)) == 1
    index.search("налог")
    assert all(c.score == 0.0 for c in index._chunks)


def test_at_most_two_chunks_per_material():
    chunks = [chunk("a", "Статья", f"Патент для ИП, часть {n}.") for n in range(5)]
    chunks.append(chunk("b", "Другая", "Патент можно купить на месяц."))
    found = SearchIndex(chunks).search("патент", k=6)
    assert sum(1 for c in found if c.material.id == "a") == 2
    assert any(c.material.id == "b" for c in found)


def test_module_search_requires_loaded_index(index):
    kb.set_index(None)
    with pytest.raises(RuntimeError, match="не загружен"):
        kb.search("налог")
    kb.set_index(index)
    try:
        assert kb.search("открыть ИП")[0].material.id == "ip"
    finally:
        kb.set_index(None)
