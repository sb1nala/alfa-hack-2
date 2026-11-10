import json

from app.kb.text import block_text, html_to_text, split_chunks


def test_html_to_text_keeps_paragraphs_and_drops_tags():
    html = "<h2>Заголовок</h2><p>Первый&nbsp;абзац <strong>важно</strong>.</p><p>Второй</p>"
    assert html_to_text(html) == "Заголовок\nПервый абзац важно.\nВторой"


def test_html_to_text_renders_list_items():
    assert html_to_text("<ul><li>раз</li><li>два</li></ul>") == "• раз\n• два"


def test_html_to_text_of_nothing_is_empty():
    assert html_to_text(None) == ""
    assert html_to_text("<p> </p>") == ""


def test_html_block_uses_html_content():
    assert block_text(0, "<p>Текст</p>", '{"anchor": "x"}') == "Текст"


def test_tip_block_reads_title_and_description_from_params():
    params = json.dumps({"title": "Совет", "description": "<p>Ведите учёт</p>"})
    assert block_text(2, None, params) == "Совет\nВедите учёт"


def test_list_block_reads_items_from_params():
    params = json.dumps({"title": "Что нужно", "items": ["Паспорт", "<b>ИНН</b>"]})
    assert block_text(4, None, params) == "Что нужно\n• Паспорт\n• ИНН"


def test_question_block_reads_title_and_text():
    params = json.dumps({"title": "Можно ли?", "text": "<p>Да</p>"})
    assert block_text(13, None, params) == "Можно ли?\nДа"


def test_image_video_and_banner_blocks_give_no_text():
    for block_type in (1, 5, 6, 7, 8, 9, 14):
        assert block_text(block_type, None, json.dumps({"title": "x", "text": "y"})) == ""


def test_broken_params_give_no_text():
    assert block_text(2, None, "{not json") == ""
    assert block_text(4, None, json.dumps({"items": "не список", "title": None})) == ""
    assert block_text(2, None, json.dumps(["список вместо объекта"])) == ""


def test_short_text_is_one_chunk():
    assert split_chunks("Один абзац.\nВторой абзац.") == ["Один абзац.\nВторой абзац."]


def test_chunks_respect_limit_and_keep_all_text():
    paragraphs = [f"Абзац номер {n} с некоторым текстом." for n in range(40)]
    chunks = split_chunks("\n".join(paragraphs), max_chars=200)
    assert len(chunks) > 1
    assert all(len(chunk) <= 200 for chunk in chunks)
    assert "\n".join(chunks).split("\n") == paragraphs


def test_long_paragraph_is_split_by_sentences():
    text = " ".join(f"Предложение {n} достаточно длинное." for n in range(30))
    chunks = split_chunks(text, max_chars=150)
    assert all(len(chunk) <= 150 for chunk in chunks)
    assert all(chunk.rstrip().endswith(".") for chunk in chunks)


def test_text_without_sentence_ends_is_cut_hard():
    chunks = split_chunks("а" * 2500, max_chars=1000)
    assert [len(chunk) for chunk in chunks] == [1000, 1000, 500]


def test_empty_text_gives_no_chunks():
    assert split_chunks("") == []
    assert split_chunks("\n \n") == []
