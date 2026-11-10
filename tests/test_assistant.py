import pytest

from app.assistant import core
from app.assistant.core import MATERIALS_ONLY_TEXT, MAX_SOURCES, reply
from app.kb import search as kb
from app.kb.search import SearchIndex
from app.llm import client as llm
from app.llm.client import LLMError
from app.models import Chunk, Material


def chunk(material_id, text, url=None, access="open"):
    material = Material(
        id=material_id,
        kind="article",
        access=access,
        title=f"Материал {material_id}",
        url=url or f"https://example.test/{material_id}/",
        summary="",
    )
    return Chunk(material=material, text=text, score=0.0)


class FakeModel:
    """Вместо LLM: возвращает заготовленный ответ и запоминает, что ему прислали."""

    def __init__(self):
        self.result = {"kind": "answer", "text": "Ответ", "used": [1], "options": []}
        self.calls = []

    def is_configured(self):
        return True

    async def complete(self, messages, json_schema=None):
        self.calls.append(messages)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    @property
    def last_prompt(self):
        return self.calls[-1][-1]["content"]


@pytest.fixture(autouse=True)
def loaded_index():
    kb.set_index(
        SearchIndex(
            [
                *(chunk(f"p{n}", f"Патент для ИП: вариант {n}.") for n in range(5)),
                chunk("l1", "Касса и чеки: урок один.", url="https://example.test/course/"),
                chunk("l2", "Касса и чеки: урок два.", url="https://example.test/course/"),
                chunk("paid", "Курс про налоги для ООО: кому подойдёт.", access="subscription"),
            ]
        )
    )
    core.reset_history()
    llm.configure(None)
    yield
    kb.set_index(None)
    core.reset_history()


@pytest.fixture
def model(monkeypatch):
    fake = FakeModel()
    monkeypatch.setattr(core.llm, "is_configured", fake.is_configured)
    monkeypatch.setattr(core.llm, "complete", fake.complete)
    return fake


# --- без LLM ---


async def test_without_llm_known_topic_gives_materials():
    answer = await reply(1, "патент для ИП")
    assert answer.kind == "answer"
    assert answer.text == MATERIALS_ONLY_TEXT
    assert len(answer.sources) == MAX_SOURCES
    assert len({source.id for source in answer.sources}) == MAX_SOURCES


async def test_sources_with_the_same_link_are_shown_once():
    answer = await reply(1, "касса и чеки")
    assert [source.url for source in answer.sources] == ["https://example.test/course/"]


async def test_unknown_topic_is_admitted_honestly_without_calling_the_model(model):
    answer = await reply(1, "рецепт борща")
    assert answer.kind == "no_answer"
    assert answer.sources == []
    assert "не нашёл" in answer.text
    assert model.calls == []


# --- с LLM ---


async def test_answer_uses_model_text_and_only_cited_sources(model):
    model.result = {"kind": "answer", "text": " Патент покупают на срок. ", "used": [2], "options": []}
    answer = await reply(1, "патент для ИП")
    assert answer.kind == "answer"
    assert answer.text == "Патент покупают на срок."
    assert len(answer.sources) == 1


async def test_prompt_contains_numbered_fragments_and_question_but_no_user_id(model):
    await reply(987654321, "патент для ИП")
    assert "[1] Материал p" in model.last_prompt
    assert "Вопрос: патент для ИП" in model.last_prompt
    assert all("987654321" not in message["content"] for message in model.calls[-1])


async def test_paid_fragment_is_marked_for_the_model(model):
    await reply(1, "курс про налоги для ООО")
    assert "(платный материал)" in model.last_prompt


async def test_answer_without_citations_still_gets_the_closest_source(model):
    model.result = {"kind": "answer", "text": "Ответ", "used": [], "options": []}
    assert len((await reply(1, "патент для ИП")).sources) == 1


async def test_citations_out_of_range_are_ignored(model):
    model.result = {"kind": "answer", "text": "Ответ", "used": [0, 99, "x", 1], "options": []}
    assert len((await reply(1, "патент для ИП")).sources) == 1


async def test_clarify_returns_question_and_limited_options_without_sources(model):
    model.result = {
        "kind": "clarify",
        "text": "Какая у вас форма бизнеса?",
        "used": [1],
        "options": ["ИП", " ООО ", "", "Самозанятый", "Ещё думаю", "Лишний"],
    }
    answer = await reply(1, "патент для ИП")
    assert answer.kind == "clarify"
    assert answer.options == ["ИП", "ООО", "Самозанятый", "Ещё думаю"]
    assert answer.sources == []


async def test_model_can_say_the_fragments_do_not_answer(model):
    model.result = {"kind": "no_answer", "text": "В базе этого нет.", "used": [1], "options": []}
    answer = await reply(1, "патент для ИП")
    assert answer.kind == "no_answer"
    assert answer.sources == []


async def test_reply_to_clarification_is_searched_with_the_original_question(model):
    model.result = {"kind": "clarify", "text": "Для кого?", "used": [], "options": ["ИП"]}
    await reply(1, "патент")
    model.result = {"kind": "answer", "text": "Ответ", "used": [1], "options": []}
    await reply(1, "ИП")
    assert "Патент для ИП" in model.last_prompt  # фрагменты найдены по «патент ИП»
    assert "Вопрос: ИП" in model.last_prompt
    history = [message["content"] for message in model.calls[-1][1:-1]]
    assert history == ["патент", "Для кого?"]


async def test_new_question_instead_of_clarification_is_searched_on_its_own(model):
    model.result = {"kind": "clarify", "text": "Для кого?", "used": [], "options": ["ИП"]}
    await reply(1, "патент")
    model.result = {"kind": "answer", "text": "Ответ", "used": [1], "options": []}
    answer = await reply(1, "касса чеки")
    assert answer.kind == "answer"
    assert "Вопрос: касса чеки" in model.last_prompt


async def test_history_is_separate_for_each_user(model):
    await reply(1, "патент для ИП")
    await reply(2, "касса и чеки")
    assert len(model.calls[-1]) == 2  # системное сообщение и вопрос, без чужой истории


@pytest.mark.parametrize(
    "bad_result",
    [
        LLMError("недоступен"),
        {"kind": "что-то", "text": "Ответ", "used": [], "options": []},
        {"kind": "answer", "text": "   ", "used": [], "options": []},
        {"kind": "answer", "used": [1]},
        "строка вместо объекта",
    ],
)
async def test_model_failure_falls_back_to_materials(model, bad_result):
    model.result = bad_result
    answer = await reply(1, "патент для ИП")
    assert answer.kind == "answer"
    assert answer.text == MATERIALS_ONLY_TEXT
    assert len(answer.sources) == MAX_SOURCES


async def test_very_long_model_text_is_cut(model):
    model.result = {"kind": "answer", "text": "а" * 10000, "used": [1], "options": []}
    assert len((await reply(1, "патент для ИП")).text) == core.MAX_TEXT_CHARS
