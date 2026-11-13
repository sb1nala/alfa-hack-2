"""Тестовый набор в eval/ остаётся согласованным: число вопросов, значения полей, ссылки на профили."""

from collections import Counter
from dataclasses import fields
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from app.models import UserProfile

EVAL_DIR = Path(__file__).resolve().parent.parent / "eval"

EXPECT = {"answer", "clarify", "no_answer", "reminder"}
CATEGORY_SIZES = {"kb": 15, "broad": 8, "off_topic": 8, "course": 5, "reminder": 4}
QUESTION_KEYS = {"id", "text", "category", "expect", "profile", "must", "must_not"}
ALLOWED = {
    "business_stage": {"idea", "starting", "running", "growing"},
    "legal_form": {"ip", "ooo", "self_employed"},
    "tax_regime": {"usn_income", "usn_income_expense", "ausn", "psn", "npd", "osno"},
    "experience": {"beginner", "experienced"},
}


def _load(name: str) -> list[dict]:
    data = yaml.safe_load((EVAL_DIR / f"{name}.yaml").read_text(encoding="utf-8"))
    return data[name]


def test_question_set_has_expected_size_and_mix():
    questions = _load("questions")
    assert len(questions) == 40
    assert Counter(q["category"] for q in questions) == CATEGORY_SIZES


def test_questions_are_unique_and_well_formed():
    questions = _load("questions")
    assert len({q["id"] for q in questions}) == len(questions)
    assert len({q["text"] for q in questions}) == len(questions)
    for q in questions:
        assert set(q) <= QUESTION_KEYS, q["id"]
        assert q["text"].strip(), q["id"]
        assert q["expect"] in EXPECT, q["id"]


def test_expected_behaviour_matches_category():
    by_category = {"broad": "clarify", "off_topic": "no_answer", "reminder": "reminder"}
    for q in _load("questions"):
        if q["category"] in by_category:
            assert q["expect"] == by_category[q["category"]], q["id"]


def test_questions_reference_existing_profiles():
    profile_ids = {p["id"] for p in _load("profiles")}
    for q in _load("questions"):
        if "profile" in q:
            assert q["profile"] in profile_ids, q["id"]


def test_profiles_fit_user_profile_type():
    profiles = _load("profiles")
    assert 5 <= len(profiles) <= 6
    assert len({p["id"] for p in profiles}) == len(profiles)
    known_fields = {f.name for f in fields(UserProfile)} - {"user_id"}
    for p in profiles:
        values = p["profile"]
        assert set(values) <= known_fields, p["id"]
        for name, allowed in ALLOWED.items():
            assert values.get(name) in allowed | {None}, (p["id"], name)
        ZoneInfo(values.get("timezone", "Europe/Moscow"))
        UserProfile(user_id=1, **values)


def test_there_is_a_profile_without_any_data():
    assert any(p["profile"] == {} for p in _load("profiles"))
