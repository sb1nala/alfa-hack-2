from app.bot.render import render_answer
from app.models import Answer, Material, Recommendation


def material(access="open", title="Как выбрать режим", url="https://example.test/a?x=1&y=2"):
    return Material(id="1", kind="article", access=access, title=title, url=url, summary="")


def test_plain_answer_is_just_escaped_text():
    assert render_answer(Answer(kind="answer", text="2 < 3 & 4")) == "2 &lt; 3 &amp; 4"


def test_sources_are_listed_as_links():
    text = render_answer(Answer(kind="answer", text="Ответ", sources=[material()]))
    assert "Источники:" in text
    assert '<a href="https://example.test/a?x=1&amp;y=2">Как выбрать режим</a>' in text


def test_paid_recommendation_is_marked_and_has_reason():
    rec = Recommendation(material=material(access="subscription", title="Курс <УСН>"), reason="под ваш режим")
    text = render_answer(Answer(kind="answer", text="Ответ", recommendations=[rec]))
    assert "Курс &lt;УСН&gt;</a> (платный) — под ваш режим" in text


def test_open_recommendation_is_not_marked_paid():
    rec = Recommendation(material=material(), reason="для старта")
    assert "(платный)" not in render_answer(Answer(kind="answer", text="Ответ", recommendations=[rec]))
