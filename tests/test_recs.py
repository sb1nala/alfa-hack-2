"""Рекомендации: отбор по вопросу и задаче, порядок по профилю, строка «почему вам». Материалы придуманы."""

import pytest

from app.kb import search as kb
from app.kb.search import SearchIndex
from app.models import Chunk, Material, UserProfile
from app.recs import service as recs

PAID_LESSON_TEXT = "Секретная методика из платного урока: шаг первый, шаг второй."


def material(id_, title, kind="article", access="open", summary="", **labels):
    return Material(
        id=id_,
        kind=kind,
        access=access,
        title=title,
        url=f"https://example.test/{id_}/",
        summary=summary,
        **labels,
    )


def chunk(mat, text):
    return Chunk(material=mat, text=text, score=0.0)


SALES = material("sales", "Как увеличить продажи", tags=["Увеличение продаж"], popularity=0.2)
SALES_CAFE = material(
    "sales-cafe", "Продажи в кафе", industries=["Общепит", "Популярное"], popularity=0.1
)
SALES_PRO = material("sales-pro", "Продажи для опытных", industries=["С опытом"], popularity=0.9)
SALES_START = material("sales-start", "Первые продажи", industries=["Без опыта"], popularity=0.3)
SALES_COURSE = material(
    "sales-course",
    "Курс «Система продаж»",
    kind="course",
    access="paid",
    summary="Научитесь строить отдел продаж с нуля. Программа рассчитана на шесть недель.",
    popularity=0.5,
)
HIRING = material("hiring", "Как нанять первого сотрудника", tags=["Поиск сотрудников"])
CONTRACT = material("contract", "Шаблон трудового договора", kind="document")
TAXES = material("taxes", "Налоги для начинающих", popularity=1.0)

ALL = [SALES, SALES_CAFE, SALES_PRO, SALES_START, SALES_COURSE, HIRING, CONTRACT, TAXES]


@pytest.fixture(autouse=True)
def knowledge_base():
    kb.set_index(
        SearchIndex(
            [
                chunk(SALES, "Чтобы увеличить продажи, посчитайте воронку продаж."),
                chunk(SALES_CAFE, "Продажи в кафе растут за счёт среднего чека."),
                chunk(SALES_PRO, "Продажи растут, когда отдел продаж работает по плану."),
                chunk(SALES_START, "Первые продажи проще сделать знакомым."),
                chunk(SALES_COURSE, "Платный материал. Курс про продажи и отдел продаж."),
                chunk(HIRING, "Поиск сотрудников начинается с описания вакансии."),
                chunk(CONTRACT, "Трудовой договор с сотрудником заключают письменно."),
                chunk(TAXES, "Налоги платят по выбранному режиму."),
            ]
        )
    )
    recs.set_catalog(ALL)
    yield
    kb.set_index(None)
    recs.set_catalog([])


def ids(result):
    return [rec.material.id for rec in result]


def test_returns_up_to_k_materials_about_the_question():
    result = recs.recommend(UserProfile(user_id=1), "продажи", set())
    assert len(result) == 3
    assert set(ids(result)) <= {"sales", "sales-cafe", "sales-pro", "sales-start", "sales-course"}
    assert len(recs.recommend(UserProfile(user_id=1), "продажи", set(), k=2)) == 2


def test_already_shown_materials_are_not_repeated():
    first = recs.recommend(UserProfile(user_id=1), "продажи", set())
    second = recs.recommend(UserProfile(user_id=1), "продажи", set(ids(first)))
    assert second and not set(ids(first)) & set(ids(second))


def test_each_material_appears_once():
    kb.set_index(
        SearchIndex([chunk(SALES, "Продажи и воронка."), chunk(SALES, "Продажи и скрипты.")])
    )
    assert ids(recs.recommend(UserProfile(user_id=1), "продажи", set())) == ["sales"]


def test_empty_profile_keeps_search_order():
    found = [c.material.id for c in kb.search("продажи", k=12)]
    assert ids(recs.recommend(UserProfile(user_id=1), "продажи", set())) == found[:3]


def test_matching_industry_goes_first_and_is_named_in_reason():
    profile = UserProfile(user_id=1, industry="общепит")
    first = recs.recommend(profile, "продажи", set())[0]
    assert first.material.id == "sales-cafe"
    assert "Общепит" in first.reason


def test_beginner_gets_beginner_material_before_advanced():
    result = ids(recs.recommend(UserProfile(user_id=1, experience="beginner"), "продажи", set(), k=5))
    assert result[0] == "sales-start"
    assert result[-1] == "sales-pro"


def test_experienced_gets_advanced_material_first():
    result = ids(recs.recommend(UserProfile(user_id=1, experience="experienced"), "продажи", set()))
    assert result[0] == "sales-pro"


def test_early_business_stage_counts_as_beginner_when_experience_unknown():
    result = ids(recs.recommend(UserProfile(user_id=1, business_stage="idea"), "продажи", set()))
    assert result[0] == "sales-start"


def test_current_goal_adds_materials_with_matching_tag():
    profile = UserProfile(user_id=1, current_goal="Поиск сотрудников")
    result = recs.recommend(profile, "налоги", set())
    by_id = {rec.material.id: rec for rec in result}
    assert "hiring" in by_id and "taxes" in by_id
    assert "Поиск сотрудников" in by_id["hiring"].reason


def test_goal_tag_match_ranks_above_plain_search_hit():
    profile = UserProfile(user_id=1, current_goal="увеличение продаж")
    assert ids(recs.recommend(profile, "продажи", set()))[0] == "sales"


def test_paid_material_reason_uses_only_summary():
    result = recs.recommend(UserProfile(user_id=1), "курс система продаж", set(), k=5)
    paid = next(rec for rec in result if rec.material.id == "sales-course")
    assert paid.reason == "Научитесь строить отдел продаж с нуля."


def test_paid_material_without_summary_gets_neutral_reason():
    bare = material("bare", "Курс «Финансы»", kind="course", access="subscription")
    kb.set_index(SearchIndex([chunk(bare, PAID_LESSON_TEXT)]))
    [rec] = recs.recommend(UserProfile(user_id=1), "секретная методика", set())
    assert rec.reason and "методика" not in rec.reason.lower()


def test_reason_is_one_line_and_never_quotes_chunk_text():
    profile = UserProfile(user_id=1, industry="Общепит", experience="beginner", current_goal="Продажи")
    for rec in recs.recommend(profile, "продажи", set(), k=5):
        assert rec.reason.strip() and "\n" not in rec.reason
        assert len(rec.reason) <= recs.MAX_REASON_CHARS
        assert "воронк" not in rec.reason and "средн" not in rec.reason


def test_document_reason_says_it_is_a_template():
    [rec] = recs.recommend(UserProfile(user_id=1), "трудовой договор", set(), k=1)
    assert rec.material.id == "contract"
    assert "шаблон" in rec.reason.lower()


def test_off_topic_question_with_empty_profile_gives_nothing():
    assert recs.recommend(UserProfile(user_id=1), "рецепт борща со свёклой", set()) == []


def test_empty_question_gives_starter_pack_of_popular_open_materials():
    result = recs.recommend(UserProfile(user_id=1), "", set())
    assert ids(result) == ["taxes", "sales-pro", "sales-start"]
    assert all(not rec.material.is_paid for rec in result)


def test_starter_pack_for_beginner_skips_advanced_materials():
    result = ids(recs.recommend(UserProfile(user_id=1, experience="beginner"), "", set()))
    assert result[0] == "sales-start" and "sales-pro" not in result


def test_empty_question_without_catalog_gives_nothing():
    recs.set_catalog([])
    assert recs.recommend(UserProfile(user_id=1), "", set()) == []


def test_k_zero_gives_nothing():
    assert recs.recommend(UserProfile(user_id=1), "продажи", set(), k=0) == []
