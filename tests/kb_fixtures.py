"""Придуманные строки таблиц в том же виде, в каком их отдаёт дамп. Данных кейса здесь нет."""

from app.kb.ingest import TABLES

PUBLISHED, DRAFT = "st-pub", "st-draft"
OPEN, SUBSCRIPTION, PAID, CLOSED = "ty-open", "ty-sub", "ty-paid", "ty-closed"


def empty_tables() -> dict[str, list[dict]]:
    tables: dict[str, list[dict]] = {name: [] for name in TABLES}
    tables["Statuses"] = [
        {"Id": PUBLISHED, "Title": "Опубликовано"},
        {"Id": DRAFT, "Title": "Черновик"},
    ]
    tables["CourseTypes"] = [
        {"Id": OPEN, "Title": "Открытый"},
        {"Id": SUBSCRIPTION, "Title": "По подписке"},
        {"Id": PAID, "Title": "Платный"},
        {"Id": CLOSED, "Title": "Закрытый"},
    ]
    tables["CourseInfoTypes"] = [
        {"Id": "it-who", "Title": "Кому подойдет"},
        {"Id": "it-banner", "Title": "Платёжный баннер"},
    ]
    return tables


def article(id_, title="Статья", **over):
    row = {
        "Id": id_,
        "Title": title,
        "Url": f"slug-{id_}",
        "Description": "Короткое описание",
        "StatusId": PUBLISHED,
        "TypeId": OPEN,
        "IsDeleted": "f",
        "IsPublic": "t",
        "IsClosed": "f",
    }
    return row | over


def block(owner_column, owner, text, order=0, type_="0", params=None):
    return {
        owner_column: owner,
        "Type": type_,
        "HtmlContent": text,
        "Params": params,
        "Order": str(order),
    }


def course(id_, title="Курс", type_id=OPEN, **over):
    row = {
        "Id": id_,
        "Title": title,
        "Url": f"course-{id_}",
        "Description": "О курсе",
        "DescriptionAdditional": None,
        "Target": None,
        "StatusId": PUBLISHED,
        "TypeId": type_id,
        "IsDeleted": "f",
        "IsClosed": "f",
    }
    return row | over


def lesson(id_, title="Урок", **over):
    row = {"Id": id_, "Title": title, "Description": None, "IsDeleted": "f", "IsGroup": "f"}
    return row | over


def lesson_in_course(lesson_id, course_id):
    return {"LessonId": lesson_id, "CourseId": course_id, "IsDeleted": "f"}
