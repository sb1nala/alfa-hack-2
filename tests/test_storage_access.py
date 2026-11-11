"""Storage contracts through the public async API and real PostgreSQL."""

import asyncio
import json
from dataclasses import fields
from datetime import datetime, timezone

import asyncpg
import pytest

from app.models import UserProfile
from app.storage import db, events, profiles, shown_materials, users
from app.storage.profiles import _CHOICES, _COLUMNS

USER = 5_000_000_001
OTHER = USER + 1
TEXT = "Вымышленный 'план'; DROP TABLE users; --"


@pytest.fixture
def no_database(monkeypatch):
    def fail():
        pytest.fail("Validation must happen before accessing the database")
    monkeypatch.setattr(db, "pool", fail)


def test_profile_field_mapping_matches_model():
    assert set(_COLUMNS) == {field.name for field in fields(UserProfile)} - {"user_id"}


@pytest.mark.parametrize("changes,error", [
    ({"user_id": 10}, ValueError), ({"unknown": "x"}, ValueError),
    ({"timezone": None}, ValueError), ({"timezone": "Mars/Olympus"}, ValueError),
    ({"timezone": "../UTC"}, ValueError), ({"timezone": ""}, ValueError),
    ({"industry": 3}, TypeError), ({"current_goal": False}, TypeError),
    ({"timezone": []}, TypeError), ({"industry": {}}, TypeError),
    ([], TypeError), (None, TypeError),
] + [({name: "invalid"}, ValueError) for name in _CHOICES])
async def test_invalid_profile_before_database(no_database, changes, error):
    with pytest.raises(error):
        await profiles.update(USER, changes)


@pytest.mark.parametrize("payload,error", [
    ([], TypeError), ("text", TypeError), (42, TypeError),
    ({"bad": object()}, ValueError), ({"bad": {1, 2}}, ValueError),
    ({"bad": float("nan")}, ValueError), ({"bad": float("inf")}, ValueError),
])
async def test_invalid_payload_before_database(no_database, payload, error):
    with pytest.raises(error):
        await events.append(USER, "event", payload)


async def test_circular_payload_before_database(no_database):
    payload = {}
    payload["self"] = payload
    with pytest.raises(ValueError):
        await events.append(USER, "event", payload)


@pytest.mark.parametrize("value,error", [("", ValueError), (" \n\t", ValueError),
                                         (123, TypeError), (None, TypeError)])
async def test_invalid_names_before_database(no_database, value, error):
    with pytest.raises(error):
        await events.append(USER, value)
    with pytest.raises(error):
        await shown_materials.mark_shown(USER, value)


@pytest.mark.parametrize("value,error", [(True, TypeError), ("1", TypeError),
                                         (2**63, ValueError)])
async def test_invalid_user_before_database(no_database, value, error):
    for operation in (users.ensure, profiles.get, profiles.reset, shown_materials.list_ids):
        with pytest.raises(error):
            await operation(value)


async def test_reads_and_empty_update_do_not_write(storage_db):
    pool, _ = storage_db
    assert await profiles.get(USER) == UserProfile(USER)
    assert await profiles.update(USER, {}) == UserProfile(USER)
    assert await shown_materials.list_ids(USER) == set()
    await profiles.reset(USER)
    assert await pool.fetchval("SELECT count(*) FROM users") == 0
    assert await pool.fetchval("SELECT count(*) FROM profiles") == 0


async def test_idempotent_and_concurrent_registration(storage_db):
    pool, _ = storage_db
    await asyncio.gather(*(users.ensure(USER) for _ in range(12)))
    before = await pool.fetchval("SELECT created_at FROM users WHERE user_id=$1", USER)
    await users.ensure(USER)
    assert await pool.fetchval("SELECT count(*) FROM users") == 1
    assert await pool.fetchval("SELECT created_at FROM users WHERE user_id=$1", USER) == before


async def test_partial_profile_and_null(storage_db):
    pool, _ = storage_db
    saved = await profiles.update(USER, {"industry": TEXT, "timezone": "Asia/Yekaterinburg"})
    assert saved == UserProfile(USER, industry=TEXT, timezone="Asia/Yekaterinburg")
    saved = await profiles.update(USER, {"current_goal": TEXT, "legal_form": "ip"})
    assert saved.industry == TEXT and saved.current_goal == TEXT
    assert saved.timezone == "Asia/Yekaterinburg" and saved.legal_form == "ip"
    saved = await profiles.update(USER, {"industry": None})
    assert saved.industry is None and saved.current_goal == TEXT
    assert saved == await profiles.get(USER)
    assert await profiles.update(USER, {}) == saved
    assert await pool.fetchval("SELECT count(*) FROM users") == 1


@pytest.mark.parametrize("name,value", [(name, value) for name, values in _CHOICES.items()
                                       for value in sorted(values)])
async def test_enum_values_and_clearing(storage_db, name, value):
    assert getattr(await profiles.update(USER, {name: value}), name) == value
    assert getattr(await profiles.update(USER, {name: None}), name) is None


@pytest.mark.parametrize("existing", [False, True])
async def test_concurrent_independent_updates(storage_db, existing):
    if existing:
        await profiles.update(USER, {"timezone": "UTC"})
    await asyncio.gather(
        profiles.update(USER, {"industry": TEXT}),
        profiles.update(USER, {"current_goal": "Проверить идею"}),
        profiles.update(USER, {"legal_form": "ooo"}),
    )
    saved = await profiles.get(USER)
    assert saved.industry == TEXT and saved.current_goal == "Проверить идею"
    assert saved.legal_form == "ooo"
    assert saved.timezone == ("UTC" if existing else "Europe/Moscow")


async def test_reset_preserves_reminders_and_history(storage_db):
    pool, _ = storage_db
    await profiles.update(USER, {"industry": TEXT, "timezone": "UTC"})
    await profiles.update(OTHER, {"industry": "Другая мастерская"})
    await events.append(USER, "synthetic")
    await shown_materials.mark_shown(USER, "fictional:one")
    await pool.execute(
        "INSERT INTO reminders (user_id, kind, text, next_run_at) VALUES ($1,'custom',$2,$3)",
        USER, TEXT, datetime(2026, 11, 12, tzinfo=timezone.utc),
    )
    before = {table: await pool.fetch(f"SELECT * FROM {table}")
              for table in ("users", "reminders", "events", "shown_materials")}
    await profiles.reset(USER)
    await profiles.reset(USER)
    assert await profiles.get(USER) == UserProfile(USER)
    assert (await profiles.get(OTHER)).industry == "Другая мастерская"
    for table, rows in before.items():
        assert await pool.fetch(f"SELECT * FROM {table}") == rows


async def test_events_json_roundtrip(storage_db):
    pool, _ = storage_db
    payload = {"текст": TEXT, "вложенный": {"список": [1, True, None, {"a": "я"}]}}
    first = await events.append(USER, TEXT)
    second = await events.append(USER, "synthetic", payload)
    assert isinstance(first, int) and second > first
    rows = await pool.fetch("SELECT * FROM events ORDER BY id")
    assert json.loads(rows[0]["payload"]) == {} and rows[0]["name"] == TEXT
    assert json.loads(rows[1]["payload"]) == payload


async def test_invalid_payload_leaves_no_rows(storage_db):
    pool, _ = storage_db
    with pytest.raises(ValueError):
        await events.append(USER, "synthetic", {"bad": object()})
    assert await pool.fetchval("SELECT count(*) FROM users") == 0
    assert await pool.fetchval("SELECT count(*) FROM events") == 0


async def test_shown_upsert_time_and_user_isolation(storage_db):
    pool, _ = storage_db
    await shown_materials.mark_shown(USER, TEXT)
    original = await pool.fetchval("SELECT shown_at FROM shown_materials")
    # A known old timestamp makes the assertion deterministic without sleeps.
    await pool.execute("UPDATE shown_materials SET shown_at = '2000-01-01Z'::timestamptz")
    before = await pool.fetchval("SELECT clock_timestamp()")
    await asyncio.gather(*(shown_materials.mark_shown(USER, TEXT) for _ in range(8)))
    after = await pool.fetchval("SELECT clock_timestamp()")
    stamp = await pool.fetchval("SELECT shown_at FROM shown_materials")
    assert before <= stamp <= after and stamp >= original
    assert await pool.fetchval("SELECT count(*) FROM shown_materials") == 1
    await shown_materials.mark_shown(OTHER, TEXT)
    await shown_materials.mark_shown(OTHER, "fictional:other")
    assert await shown_materials.list_ids(USER) == {TEXT}
    assert await shown_materials.list_ids(OTHER) == {TEXT, "fictional:other"}
    assert await shown_materials.list_ids(OTHER + 1) == set()


@pytest.mark.parametrize("operation,table", [
    (lambda: profiles.update(USER, {"industry": "a\x00b"}), "profiles"),
    (lambda: events.append(USER, "synthetic", {"value": "a\x00b"}), "events"),
    (lambda: shown_materials.mark_shown(USER, "a\x00b"), "shown_materials"),
])
async def test_database_failure_rolls_back_user(storage_db, operation, table):
    pool, _ = storage_db
    with pytest.raises(asyncpg.PostgresError):
        await operation()
    assert await pool.fetchval("SELECT count(*) FROM users") == 0
    assert await pool.fetchval(f"SELECT count(*) FROM {table}") == 0


async def test_persistence_after_pool_reopened(storage_db):
    pool, reopen = storage_db
    saved = await profiles.update(USER, {"industry": TEXT})
    event_id = await events.append(USER, TEXT, {"text": TEXT})
    await shown_materials.mark_shown(USER, TEXT)
    await pool.close()
    new_pool = await reopen()
    assert await profiles.get(USER) == saved
    assert await shown_materials.list_ids(USER) == {TEXT}
    assert await new_pool.fetchval("SELECT name FROM events WHERE id=$1", event_id) == TEXT
