"""Чтение выгрузки кейса и сборка материалов и фрагментов. Роль A.

Устройство выгрузки — docs/data.md.
"""

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from app.kb.dump import Row, read_tables
from app.kb.text import block_text, html_to_text, split_chunks
from app.models import Access, Chunk, Material, MaterialKind

BASE_URL = "https://kurs.alfabank.ru"
PUBLISHED = "Опубликовано"
ACCESS_BY_TYPE: dict[str, Access] = {
    "Открытый": "open",
    "По подписке": "subscription",
    "Платный": "paid",
}
COURSE_INFO_TYPES = {"Кому подойдет", "Что вы получите от обучения", "Как проходит обучение"}
SUMMARY_CHARS = 300

TABLES = {
    "Statuses",
    "CourseTypes",
    "CourseInfoTypes",
    "Articles",
    "ArticleEmbedItems",
    "Glossary",
    "GlossaryEmbedItems",
    "Documents",
    "DocumentEmbedItems",
    "Courses",
    "CourseInfoBindings",
    "CourseQuestions",
    "CourseLessonBindings",
    "CourseUserBindings",
    "Lessons",
    "LessonEmbedItems",
    "Webinars",
    "Podcasts",
    "PodcastDetails",
    "Calculators",
    "CalculatorPageEmbedItems",
    "ContentSettings",
    "ContentSettingsTopics",
    "ContentSettingsTags",
    "ContentSettingsIndustries",
    "Topics",
    "Tags",
    "Industries",
}


class DataError(RuntimeError):
    """Выгрузка не найдена или не читается."""


@dataclass
class Corpus:
    materials: list[Material]
    chunks: list[Chunk]


def find_backup(data_dir: Path) -> Path:
    backups = sorted(data_dir.glob("*.backup")) if data_dir.is_dir() else []
    if not backups:
        raise DataError(
            f"В папке {data_dir} нет файла выгрузки .backup. Положите "
            "alfa_course_hackathon.backup в папку data/ рядом с compose.yaml."
        )
    return backups[0]


def load_corpus(data_dir: Path) -> Corpus:
    return build_corpus(read_tables(find_backup(data_dir), TABLES))


def load_materials(data_dir: Path) -> list[Material]:
    """Прочитать .backup и вернуть опубликованные материалы."""
    return load_corpus(data_dir).materials


def build_chunks(data_dir: Path) -> list[Chunk]:
    """Собрать текст из блоков и нарезать на фрагменты. Платные уроки не включаются."""
    return load_corpus(data_dir).chunks


def _true(value: str | None) -> bool:
    return value == "t"


def _clean(value: str | None) -> str:
    return " ".join((value or "").split())


def _join(*texts: str) -> str:
    return "\n".join(text for text in texts if text)


def _summary(*texts: str | None) -> str:
    for text in texts:
        plain = html_to_text(text).replace("\n", " ")
        if plain:
            if len(plain) <= SUMMARY_CHARS:
                return plain
            return plain[: SUMMARY_CHARS - 1].rstrip() + "…"
    return ""


def _ordered(parts: list[tuple[int, str]]) -> list[str]:
    return [text for _, text in sorted(parts, key=lambda part: part[0])]


def _blocks_by_owner(rows: list[Row], owner_column: str) -> dict[str, str]:
    """Склеить текст блоков каждого материала по порядку."""
    grouped: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for row in rows:
        text = block_text(int(row["Type"] or 0), row["HtmlContent"], row["Params"])
        if text:
            grouped[row[owner_column] or ""].append((int(row["Order"] or 0), text))
    return {owner: "\n".join(_ordered(parts)) for owner, parts in grouped.items()}


def _labels(tables: dict[str, list[Row]]) -> dict[str, dict[str, list[str]]]:
    """Темы, теги и отрасли материалов: {id материала: {"topics": [...], ...}}."""
    entity_by_settings = {row["Id"]: row["EntityId"] for row in tables["ContentSettings"]}
    result: dict[str, dict[str, list[str]]] = defaultdict(
        lambda: {"topics": [], "tags": [], "industries": []}
    )
    for key, link_table, id_column, name_table in (
        ("topics", "ContentSettingsTopics", "TopicId", "Topics"),
        ("tags", "ContentSettingsTags", "TagId", "Tags"),
        ("industries", "ContentSettingsIndustries", "IndustryId", "Industries"),
    ):
        names = {row["Id"]: _clean(row["Title"]) for row in tables[name_table]}
        for link in tables[link_table]:
            entity = entity_by_settings.get(link["ContentSettingsId"])
            name = names.get(link[id_column])
            if entity and name and name not in result[entity][key]:
                result[entity][key].append(name)
    return result


def build_corpus(tables: dict[str, list[Row]]) -> Corpus:
    """Собрать материалы и фрагменты из строк таблиц. Без ввода-вывода — удобно тестировать."""
    published = {row["Id"] for row in tables["Statuses"] if row["Title"] == PUBLISHED}
    access_by_type = {
        row["Id"]: ACCESS_BY_TYPE[row["Title"]]
        for row in tables["CourseTypes"]
        if row["Title"] in ACCESS_BY_TYPE
    }
    labels = _labels(tables)
    materials: list[Material] = []
    chunks: list[Chunk] = []

    def add(
        row: Row,
        kind: MaterialKind,
        section: str,
        slug: str | None,
        text: str,
        *,
        access: Access = "open",
        title: str | None = None,
        summary: str = "",
        popularity: float = 0.0,
    ) -> None:
        if not slug:
            return
        own = labels.get(row["Id"] or "", {})
        material = Material(
            id=row["Id"] or "",
            kind=kind,
            access=access,
            title=_clean(title or row["Title"]),
            url=f"{BASE_URL}/{section}/{slug}/",
            summary=summary,
            topics=list(own.get("topics", [])),
            tags=list(own.get("tags", [])),
            industries=list(own.get("industries", [])),
            popularity=popularity,
        )
        materials.append(material)
        for piece in split_chunks(text or material.title):
            chunks.append(Chunk(material=material, text=piece, score=0.0))

    def is_published(row: Row) -> bool:
        return row["StatusId"] in published and not _true(row["IsDeleted"])

    article_text = _blocks_by_owner(tables["ArticleEmbedItems"], "ArticleId")
    for row in tables["Articles"]:
        access = access_by_type.get(row["TypeId"])
        if is_published(row) and access and _true(row["IsPublic"]) and not _true(row["IsClosed"]):
            add(
                row,
                "article",
                "articles",
                row["Url"],
                article_text.get(row["Id"] or "", ""),
                access=access,
                summary=_summary(row["Description"]),
            )

    for kind, section, table, blocks, owner in (
        ("glossary", "vocabulary", "Glossary", "GlossaryEmbedItems", "GlossaryId"),
        ("document", "documents", "Documents", "DocumentEmbedItems", "DocumentId"),
    ):
        texts = _blocks_by_owner(tables[blocks], owner)
        for row in tables[table]:
            if is_published(row):
                body = texts.get(row["Id"] or "", "") or html_to_text(row["Description"])
                add(row, kind, section, row["Url"], body, summary=_summary(row["Description"], body))

    # Курсы: в индекс идёт только карточка — описание, «кому подойдёт», вопросы и ответы.
    info_types = {
        row["Id"] for row in tables["CourseInfoTypes"] if row["Title"] in COURSE_INFO_TYPES
    }
    course_parts: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for row in tables["CourseInfoBindings"]:
        if row["CourseInfoTypeId"] in info_types:
            text = _join(html_to_text(row["Title"]), html_to_text(row["Description"]))
            course_parts[row["CourseId"] or ""].append((int(row["Order"] or 0), text))
    for row in tables["CourseQuestions"]:
        if not _true(row["IsDeleted"]):
            text = _join(html_to_text(row["Question"]), html_to_text(row["Answer"]))
            course_parts[row["CourseId"] or ""].append((1000 + int(row["Order"] or 0), text))
    enrollments: dict[str, int] = defaultdict(int)
    for row in tables["CourseUserBindings"]:
        enrollments[row["CourseId"] or ""] += 1
    top_enrollments = max(enrollments.values(), default=0) or 1

    courses: dict[str, tuple[Row, Access]] = {}
    for row in tables["Courses"]:
        access = access_by_type.get(row["TypeId"])
        if not (is_published(row) and access and not _true(row["IsClosed"]) and row["Url"]):
            continue
        course_id = row["Id"] or ""
        courses[course_id] = (row, access)
        card = _join(
            html_to_text(row["Description"]),
            html_to_text(row["DescriptionAdditional"]),
            html_to_text(row["Target"]),
            *_ordered(course_parts.get(course_id, [])),
        )
        add(
            row,
            "course",
            "courses",
            row["Url"],
            card,
            access=access,
            summary=_summary(row["Description"]),
            popularity=enrollments[course_id] / top_enrollments,
        )

    # Уроки: только те, все курсы которых открыты и опубликованы.
    # Тексты уроков платных курсов и курсов по подписке в индекс не попадают.
    courses_of_lesson: dict[str, set[str]] = defaultdict(set)
    for row in tables["CourseLessonBindings"]:
        if not _true(row["IsDeleted"]):
            courses_of_lesson[row["LessonId"] or ""].add(row["CourseId"] or "")
    lesson_text = _blocks_by_owner(tables["LessonEmbedItems"], "LessonId")
    for row in tables["Lessons"]:
        owners = courses_of_lesson.get(row["Id"] or "", set())
        if _true(row["IsDeleted"]) or _true(row["IsGroup"]) or not owners:
            continue
        if any(owner not in courses or courses[owner][1] != "open" for owner in owners):
            continue
        body = lesson_text.get(row["Id"] or "", "")
        if not body:
            continue
        course_row = courses[sorted(owners)[0]][0]
        add(
            row,
            "lesson",
            "courses",
            course_row["Url"],
            body,
            title=f"{_clean(row['Title'])} — урок курса «{_clean(course_row['Title'])}»",
            summary=_summary(row["Description"], body),
        )

    for row in tables["Webinars"]:
        access = access_by_type.get(row["TypeId"])
        if is_published(row) and access and not _true(row["IsClosed"]):
            body = _join(html_to_text(row["Description"]), html_to_text(row["DescriptionExt"]))
            add(
                row,
                "webinar",
                "webinars",
                row["Url"],
                body,
                access=access,
                summary=_summary(row["Description"]),
            )

    podcast_details: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for row in tables["PodcastDetails"]:
        text = _join(html_to_text(row["Title"]), html_to_text(row["Description"]))
        podcast_details[row["PodcastId"] or ""].append((int(row["Order"] or 0), text))
    for row in tables["Podcasts"]:
        access = access_by_type.get(row["TypeId"])
        if is_published(row) and access and not _true(row["IsClosed"]):
            body = _join(
                html_to_text(row["Description"]),
                html_to_text(row["DescriptionExt"]),
                *_ordered(podcast_details.get(row["Id"] or "", [])),
            )
            add(
                row,
                "podcast",
                "podcasts",
                row["Url"],
                body,
                access=access,
                summary=_summary(row["Description"]),
            )

    calculator_text = _blocks_by_owner(tables["CalculatorPageEmbedItems"], "CalculatorPageId")
    for row in tables["Calculators"]:
        if not _true(row["IsDeleted"]):
            body = _join(html_to_text(row["Description"]), calculator_text.get(row["Id"] or "", ""))
            add(
                row,
                "calculator",
                "calculators",
                row["Url"],
                body,
                summary=_summary(row["Description"]),
            )

    return Corpus(materials=materials, chunks=chunks)
