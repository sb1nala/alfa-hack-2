"""Журнал событий и UTM-метки: что пишем, что не пишем, какие ссылки помечаем."""

import logging
from urllib.parse import parse_qs, urlsplit

import pytest

from app import events

ARTICLE = "https://kurs.alfabank.ru/articles/primer/"


@pytest.fixture
def journal(monkeypatch):
    written = []

    async def append(user_id, name, payload=None):
        written.append((user_id, name, payload))
        return len(written)

    monkeypatch.setattr(events.storage, "append", append)
    return written


async def test_event_is_written_with_payload(journal):
    await events.log_event(7, events.MATERIAL_SHOWN, {"material_id": "a1", "place": "answer"})
    assert journal == [(7, "material_shown", {"material_id": "a1", "place": "answer"})]


async def test_event_without_payload_gets_empty_object(journal):
    await events.log_event(7, events.BOT_STARTED)
    assert journal == [(7, "bot_started", {})]


async def test_message_text_and_personal_data_never_reach_the_journal(journal):
    await events.log_event(
        7, events.QUESTION_ASKED,
        {"kind": "answer", "text": "как платить налоги", "username": "someone", "first_name": "Имя"},
    )
    assert journal == [(7, "question_asked", {"kind": "answer"})]


async def test_caller_payload_is_not_modified(journal):
    payload = {"kind": "answer", "text": "вопрос"}
    await events.log_event(7, events.QUESTION_ASKED, payload)
    assert payload == {"kind": "answer", "text": "вопрос"}


async def test_storage_failure_does_not_break_the_bot(monkeypatch, caplog):
    async def broken(*args, **kwargs):
        raise RuntimeError("база недоступна")

    monkeypatch.setattr(events.storage, "append", broken)
    with caplog.at_level(logging.ERROR):
        await events.log_event(7, events.BOT_STARTED)
    assert "bot_started" in caplog.text


async def test_unknown_event_is_written_but_reported(journal, caplog):
    with caplog.at_level(logging.WARNING):
        await events.log_event(7, "something_new")
    assert journal == [(7, "something_new", {})]
    assert "something_new" in caplog.text


def test_event_names_are_unique_snake_case():
    assert all(name == name.lower() and " " not in name for name in events.EVENT_NAMES)
    assert len(events.EVENT_NAMES) == 10


def _query(url):
    return {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}


def test_platform_link_gets_utm_tags():
    url = events.tracked_url(ARTICLE, "recommendation", "article")
    assert url.startswith(ARTICLE + "?")
    assert _query(url) == {
        "utm_source": "alfa_kurs_bot", "utm_medium": "telegram",
        "utm_campaign": "recommendation", "utm_content": "article",
    }


def test_content_is_optional():
    assert "utm_content" not in _query(events.tracked_url(ARTICLE, "answer"))


def test_existing_query_and_fragment_are_kept():
    url = events.tracked_url("https://kurs.alfabank.ru/courses/x/?tab=program#lessons", "answer")
    assert _query(url)["tab"] == "program"
    assert url.endswith("#lessons")


def test_old_utm_tags_are_replaced_not_duplicated():
    once = events.tracked_url(ARTICLE, "answer", "article")
    twice = events.tracked_url(once, "reminder_tax", "article")
    assert twice.count("utm_campaign=") == 1
    assert _query(twice)["utm_campaign"] == "reminder_tax"


@pytest.mark.parametrize("url", [
    "https://example.com/page",
    "https://kurs.alfabank.ru.evil.example/articles/x/",
    "tg://resolve?domain=bot",
    "",
])
def test_foreign_links_are_left_untouched(url):
    assert events.tracked_url(url, "answer") == url


def test_unknown_campaign_is_rejected():
    with pytest.raises(ValueError):
        events.tracked_url(ARTICLE, "spam")


@pytest.mark.parametrize("content", ["Иван Иванов", "user name", "a&b=c", ""])
def test_free_text_cannot_be_put_into_utm(content):
    with pytest.raises(ValueError):
        events.tracked_url(ARTICLE, "answer", content)
