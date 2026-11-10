import pytest

from app.kb.ingest import DataError, build_corpus, find_backup
from tests.kb_fixtures import (
    CLOSED,
    DRAFT,
    OPEN,
    PAID,
    SUBSCRIPTION,
    article,
    block,
    course,
    empty_tables,
    lesson,
    lesson_in_course,
)


def titles(corpus, kind=None):
    return {m.title for m in corpus.materials if kind is None or m.kind == kind}


def all_text(corpus):
    return "\n".join(chunk.text for chunk in corpus.chunks)


def test_published_article_becomes_material_with_full_url_and_text():
    tables = empty_tables()
    tables["Articles"] = [article("a1", "Как открыть ИП")]
    tables["ArticleEmbedItems"] = [
        block("ArticleId", "a1", "<p>Второй блок</p>", order=2),
        block("ArticleId", "a1", "<p>Первый блок</p>", order=1),
    ]
    corpus = build_corpus(tables)
    [material] = corpus.materials
    assert material.kind == "article"
    assert material.access == "open"
    assert material.url == "https://kurs.alfabank.ru/articles/slug-a1/"
    assert material.summary == "Короткое описание"
    assert [chunk.text for chunk in corpus.chunks] == ["Первый блок\nВторой блок"]
    assert corpus.chunks[0].material is material


@pytest.mark.parametrize(
    "change",
    [
        {"StatusId": DRAFT},
        {"IsDeleted": "t"},
        {"IsPublic": "f"},
        {"IsClosed": "t"},
        {"TypeId": CLOSED},
        {"Url": None},
    ],
)
def test_hidden_articles_are_skipped(change):
    tables = empty_tables()
    tables["Articles"] = [article("a1", **change)]
    tables["ArticleEmbedItems"] = [block("ArticleId", "a1", "<p>Текст</p>")]
    corpus = build_corpus(tables)
    assert corpus.materials == []
    assert corpus.chunks == []


def test_long_summary_is_cut():
    tables = empty_tables()
    tables["Articles"] = [article("a1", Description="слово " * 200)]
    [material] = build_corpus(tables).materials
    assert len(material.summary) <= 300
    assert material.summary.endswith("…")


def test_topics_tags_and_industries_are_attached_without_duplicates():
    tables = empty_tables()
    tables["Articles"] = [article("a1")]
    tables["ContentSettings"] = [{"Id": "cs1", "EntityId": "a1"}]
    tables["Topics"] = [{"Id": "t1", "Title": "Налоги"}, {"Id": "t2", "Title": " Налоги\t"}]
    tables["Tags"] = [{"Id": "g1", "Title": "Выбор налогового режима"}]
    tables["Industries"] = [{"Id": "i1", "Title": "Розница"}]
    tables["ContentSettingsTopics"] = [
        {"ContentSettingsId": "cs1", "TopicId": "t1"},
        {"ContentSettingsId": "cs1", "TopicId": "t2"},
    ]
    tables["ContentSettingsTags"] = [{"ContentSettingsId": "cs1", "TagId": "g1"}]
    tables["ContentSettingsIndustries"] = [{"ContentSettingsId": "cs1", "IndustryId": "i1"}]
    [material] = build_corpus(tables).materials
    assert material.topics == ["Налоги"]
    assert material.tags == ["Выбор налогового режима"]
    assert material.industries == ["Розница"]


def test_glossary_and_documents_use_their_sections():
    tables = empty_tables()
    base = {"StatusId": "st-pub", "IsDeleted": "f", "Description": "Описание"}
    tables["Glossary"] = [{"Id": "g1", "Title": "Смета", "Url": "smeta"} | base]
    tables["Documents"] = [{"Id": "d1", "Title": "Акт", "Url": "akt"} | base]
    tables["DocumentEmbedItems"] = [block("DocumentId", "d1", "<p>Текст акта</p>")]
    corpus = build_corpus(tables)
    urls = {m.kind: m.url for m in corpus.materials}
    assert urls == {
        "glossary": "https://kurs.alfabank.ru/vocabulary/smeta/",
        "document": "https://kurs.alfabank.ru/documents/akt/",
    }
    assert "Текст акта" in all_text(corpus)
    assert "Описание" in all_text(corpus)  # у термина нет блоков — берём описание


def test_course_card_has_description_audience_and_questions_but_no_banner():
    tables = empty_tables()
    tables["Courses"] = [course("c1", "Финансы", type_id=PAID, Target="<p>Цель курса</p>")]
    tables["CourseInfoBindings"] = [
        {
            "CourseId": "c1",
            "CourseInfoTypeId": "it-who",
            "Title": "Новичкам",
            "Description": "Без опыта",
            "Order": "1",
        },
        {
            "CourseId": "c1",
            "CourseInfoTypeId": "it-banner",
            "Title": "Купите сейчас",
            "Description": None,
            "Order": "2",
        },
    ]
    tables["CourseQuestions"] = [
        {
            "CourseId": "c1",
            "Question": "Будет сертификат?",
            "Answer": "Да",
            "Order": "1",
            "IsDeleted": "f",
        }
    ]
    corpus = build_corpus(tables)
    [material] = corpus.materials
    assert material.access == "paid"
    assert material.is_paid
    assert material.url == "https://kurs.alfabank.ru/courses/course-c1/"
    text = all_text(corpus)
    for expected in ("О курсе", "Цель курса", "Новичкам", "Без опыта", "Будет сертификат?", "Да"):
        assert expected in text
    assert "Купите сейчас" not in text


def test_lessons_of_open_course_are_indexed_with_course_url():
    tables = empty_tables()
    tables["Courses"] = [course("c1", "Старт")]
    tables["Lessons"] = [lesson("l1", "Первый шаг"), lesson("l2", "Раздел", IsGroup="t")]
    tables["CourseLessonBindings"] = [lesson_in_course("l1", "c1"), lesson_in_course("l2", "c1")]
    tables["LessonEmbedItems"] = [
        block("LessonId", "l1", "<p>Открытый текст урока</p>"),
        block("LessonId", "l2", "<p>Текст раздела</p>"),
    ]
    corpus = build_corpus(tables)
    assert titles(corpus, "lesson") == {"Первый шаг — урок курса «Старт»"}
    [lesson_material] = [m for m in corpus.materials if m.kind == "lesson"]
    assert lesson_material.url == "https://kurs.alfabank.ru/courses/course-c1/"
    assert "Открытый текст урока" in all_text(corpus)
    assert "Текст раздела" not in all_text(corpus)


@pytest.mark.parametrize("type_id", [SUBSCRIPTION, PAID])
def test_lesson_text_of_paid_course_never_reaches_the_index(type_id):
    tables = empty_tables()
    tables["Courses"] = [course("c1", "Платный курс", type_id=type_id)]
    tables["Lessons"] = [lesson("l1")]
    tables["CourseLessonBindings"] = [lesson_in_course("l1", "c1")]
    tables["LessonEmbedItems"] = [block("LessonId", "l1", "<p>СЕКРЕТНЫЙ платный текст</p>")]
    corpus = build_corpus(tables)
    assert titles(corpus, "lesson") == set()
    assert titles(corpus, "course") == {"Платный курс"}
    assert "СЕКРЕТНЫЙ" not in all_text(corpus)


def test_lesson_shared_by_open_and_paid_course_is_treated_as_paid():
    tables = empty_tables()
    tables["Courses"] = [course("c1"), course("c2", type_id=SUBSCRIPTION)]
    tables["Lessons"] = [lesson("l1")]
    tables["CourseLessonBindings"] = [lesson_in_course("l1", "c1"), lesson_in_course("l1", "c2")]
    tables["LessonEmbedItems"] = [block("LessonId", "l1", "<p>СЕКРЕТНЫЙ текст</p>")]
    assert "СЕКРЕТНЫЙ" not in all_text(build_corpus(tables))


@pytest.mark.parametrize("change", [{"StatusId": DRAFT}, {"IsClosed": "t"}, {"TypeId": CLOSED}])
def test_lessons_of_hidden_course_are_skipped_with_the_course(change):
    tables = empty_tables()
    tables["Courses"] = [course("c1", **change)]
    tables["Lessons"] = [lesson("l1")]
    tables["CourseLessonBindings"] = [lesson_in_course("l1", "c1")]
    tables["LessonEmbedItems"] = [block("LessonId", "l1", "<p>Текст</p>")]
    assert build_corpus(tables).materials == []


def test_lesson_without_course_is_skipped():
    tables = empty_tables()
    tables["Lessons"] = [lesson("l1")]
    tables["LessonEmbedItems"] = [block("LessonId", "l1", "<p>Текст</p>")]
    assert build_corpus(tables).materials == []


def test_course_popularity_is_share_of_top_enrollments():
    tables = empty_tables()
    tables["Courses"] = [course("c1", "Популярный"), course("c2", "Редкий"), course("c3", "Новый")]
    tables["CourseUserBindings"] = [{"CourseId": "c1"}] * 4 + [{"CourseId": "c2"}]
    popularity = {m.title: m.popularity for m in build_corpus(tables).materials}
    assert popularity == {"Популярный": 1.0, "Редкий": 0.25, "Новый": 0.0}


def test_material_without_text_is_still_findable_by_title():
    tables = empty_tables()
    tables["Articles"] = [article("a1", "Только заголовок", Description=None)]
    corpus = build_corpus(tables)
    assert [chunk.text for chunk in corpus.chunks] == ["Только заголовок"]


def test_missing_backup_explains_where_to_put_it(tmp_path):
    with pytest.raises(DataError, match="alfa_course_hackathon.backup"):
        find_backup(tmp_path)
    with pytest.raises(DataError):
        find_backup(tmp_path / "нет-такой-папки")


def test_backup_is_found_by_extension(tmp_path):
    (tmp_path / "x.backup").write_bytes(b"")
    assert find_backup(tmp_path).name == "x.backup"
