"""Real PostgreSQL tests; only UUID-named test schemas are created or dropped.

Run with DATABASE_URL pointing to a database that permits CREATE SCHEMA.
Each test uses its own connection/search_path, never the application's global
pool or public schema. An invalid configured URL is deliberately an error.
"""

import json
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from app.models import Reminder, UserProfile
from app.storage.db import SCHEMA_PATH

USER_ID = 5_000_000_001
OTHER_USER_ID = 5_000_000_002
TABLES = {"users", "profiles", "reminders", "events", "shown_materials"}
WHEN = datetime(2026, 11, 12, 10, 15, tzinfo=timezone(timedelta(hours=5, minutes=30)))


async def add_user(connection, user_id=USER_ID):
    await connection.execute("INSERT INTO users (user_id) VALUES ($1)", user_id)


async def seed_all(connection, user_id=USER_ID):
    await add_user(connection, user_id)
    await connection.execute("INSERT INTO profiles (user_id) VALUES ($1)", user_id)
    await connection.execute(
        "INSERT INTO reminders (user_id, kind, text, next_run_at) VALUES ($1, $2, $3, $4)",
        user_id, "custom", "Проверить вымышленный план", WHEN,
    )
    await connection.execute(
        "INSERT INTO events (user_id, name) VALUES ($1, $2)", user_id, "synthetic_event",
    )
    await connection.execute(
        "INSERT INTO shown_materials (user_id, material_id) VALUES ($1, $2)",
        user_id, "fictional:article:one",
    )


async def test_tables_and_reapplication_preserve_every_row(schema_db):
    connection = schema_db
    tables = await connection.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
    )
    assert {row["tablename"] for row in tables} == TABLES
    await seed_all(connection)
    before = {table: await connection.fetch(f'SELECT * FROM "{table}"') for table in TABLES}
    await connection.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    after = {table: await connection.fetch(f'SELECT * FROM "{table}"') for table in TABLES}
    assert after == before
    # Identity sequences still produce fresh IDs after another application.
    event_id = await connection.fetchval(
        "INSERT INTO events (user_id, name) VALUES ($1, $2) RETURNING id", USER_ID, "second"
    )
    assert event_id > before["events"][0]["id"]


async def test_bigint_empty_and_partial_profiles(schema_db):
    await add_user(schema_db)
    await add_user(schema_db, OTHER_USER_ID)
    await schema_db.execute("INSERT INTO profiles (user_id) VALUES ($1)", USER_ID)
    row = await schema_db.fetchrow("SELECT * FROM profiles WHERE user_id = $1", USER_ID)
    assert UserProfile(**dict(row)) == UserProfile(user_id=USER_ID)
    await schema_db.execute(
        "INSERT INTO profiles (user_id, industry, current_goal, timezone) VALUES ($1, $2, $3, $4)",
        OTHER_USER_ID, "Вымышленная мастерская", "Проверить идею", "Asia/Yekaterinburg",
    )
    partial = await schema_db.fetchrow("SELECT * FROM profiles WHERE user_id = $1", OTHER_USER_ID)
    assert UserProfile(**dict(partial)) == UserProfile(
        user_id=OTHER_USER_ID, industry="Вымышленная мастерская",
        current_goal="Проверить идею", timezone="Asia/Yekaterinburg",
    )
    with pytest.raises(asyncpg.UniqueViolationError):
        await schema_db.execute("INSERT INTO profiles (user_id) VALUES ($1)", USER_ID)
    with pytest.raises(asyncpg.UniqueViolationError):
        await add_user(schema_db)


@pytest.mark.parametrize("kind,rrule,active", [
    ("custom", None, True),
    ("learning", "FREQ=WEEKLY;BYDAY=TU", True),
    ("tax", "FREQ=YEARLY;BYMONTH=4;BYMONTHDAY=1", False),
])
async def test_reminders_round_trip(schema_db, kind, rrule, active):
    await add_user(schema_db)
    row = await schema_db.fetchrow(
        "INSERT INTO reminders (user_id, kind, text, next_run_at, rrule, active) "
        "VALUES ($1, $2, $3, $4, $5, $6) RETURNING *",
        USER_ID, kind, "Вымышленное напоминание", WHEN, rrule, active,
    )
    reminder = Reminder(**dict(row))
    assert reminder == Reminder(
        id=row["id"], user_id=USER_ID, kind=kind, text="Вымышленное напоминание",
        next_run_at=WHEN.astimezone(timezone.utc), rrule=rrule, active=active,
    )
    assert reminder.id > 0
    assert reminder.next_run_at.utcoffset() == timedelta(0)


async def test_defaults_and_jsonb(schema_db):
    before = await schema_db.fetchval("SELECT clock_timestamp()")
    await seed_all(schema_db)
    after = await schema_db.fetchval("SELECT clock_timestamp()")
    for table, column in [("users", "created_at"), ("events", "occurred_at"),
                          ("shown_materials", "shown_at")]:
        stamp = await schema_db.fetchval(f'SELECT {column} FROM {table}')
        assert before <= stamp <= after
        assert stamp.utcoffset() == timedelta(0)
    reminder = await schema_db.fetchrow("SELECT * FROM reminders")
    assert reminder["active"] is True
    assert reminder["rrule"] is None
    assert json.loads(await schema_db.fetchval("SELECT payload FROM events")) == {}
    payload = {"source": "synthetic", "nested": {"ids": [1, 2], "ok": True}, "empty": None}
    row = await schema_db.fetchrow(
        "INSERT INTO events (user_id, name, payload) VALUES ($1, $2, $3::jsonb) "
        "RETURNING payload, pg_typeof(payload)::text AS type",
        USER_ID, "fictional_click", json.dumps(payload),
    )
    assert row["type"] == "jsonb"
    assert json.loads(row["payload"]) == payload


async def test_invalid_kind(schema_db):
    await add_user(schema_db)
    with pytest.raises(asyncpg.CheckViolationError):
        await schema_db.execute(
            "INSERT INTO reminders (user_id, kind, text, next_run_at) VALUES ($1, $2, $3, $4)",
            USER_ID, "broadcast", "Вымышленный текст", WHEN,
        )


@pytest.mark.parametrize("statement", [
    "INSERT INTO profiles (user_id) VALUES ($1)",
    "INSERT INTO reminders (user_id, kind, text, next_run_at) "
    "VALUES ($1, 'custom', 'Synthetic', now())",
    "INSERT INTO events (user_id, name) VALUES ($1, 'synthetic')",
    "INSERT INTO shown_materials (user_id, material_id) VALUES ($1, 'synthetic')",
])
async def test_foreign_keys_reject_missing_user(schema_db, statement):
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await schema_db.execute(statement, USER_ID)


async def test_user_delete_cascades_only_own_data(schema_db):
    await seed_all(schema_db)
    await seed_all(schema_db, OTHER_USER_ID)
    await schema_db.execute("DELETE FROM users WHERE user_id = $1", USER_ID)
    for table in TABLES:
        rows = await schema_db.fetch(f'SELECT user_id FROM "{table}"')
        assert [row["user_id"] for row in rows] == [OTHER_USER_ID]


async def test_profile_reset_preserves_user_history(schema_db):
    await seed_all(schema_db)
    await schema_db.execute("DELETE FROM profiles WHERE user_id = $1", USER_ID)
    for table in TABLES - {"profiles"}:
        assert await schema_db.fetchval(f'SELECT count(*) FROM "{table}"') == 1


async def test_shown_material_pair_is_unique(schema_db):
    await seed_all(schema_db)
    with pytest.raises(asyncpg.UniqueViolationError):
        await schema_db.execute(
            "INSERT INTO shown_materials (user_id, material_id) VALUES ($1, $2)",
            USER_ID, "fictional:article:one",
        )
    await schema_db.execute(
        "INSERT INTO shown_materials (user_id, material_id) VALUES ($1, $2)",
        USER_ID, "fictional:course:two",
    )
    await add_user(schema_db, OTHER_USER_ID)
    await schema_db.execute(
        "INSERT INTO shown_materials (user_id, material_id) VALUES ($1, $2)",
        OTHER_USER_ID, "fictional:article:one",
    )
    assert await schema_db.fetchval("SELECT count(*) FROM shown_materials") == 3


@pytest.mark.parametrize("table,column", [
    ("users", "created_at"), ("reminders", "next_run_at"),
    ("events", "occurred_at"), ("shown_materials", "shown_at"),
])
async def test_timestamptz_preserves_instant_in_utc(schema_db, table, column):
    await seed_all(schema_db)
    await schema_db.execute(f'UPDATE "{table}" SET "{column}" = $1 WHERE user_id = $2', WHEN, USER_ID)
    row = await schema_db.fetchrow(
        f'SELECT "{column}" AS stamp, pg_typeof("{column}")::text AS type FROM "{table}"'
    )
    assert row["type"] == "timestamp with time zone"
    assert row["stamp"] == WHEN.astimezone(timezone.utc)
    assert row["stamp"].utcoffset() == timedelta(0)


async def test_query_indexes_exist_in_test_schema(schema_db):
    indexes = {row["indexname"]: row["indexdef"] for row in await schema_db.fetch(
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = current_schema()"
    )}
    assert "(next_run_at) WHERE active" in indexes["reminders_due_idx"]
    assert "(user_id)" in indexes["reminders_user_idx"]
    assert "(user_id, occurred_at DESC)" in indexes["events_user_history_idx"]
    assert "(user_id, material_id)" in indexes["shown_materials_pkey"]


@pytest.mark.parametrize("table,column", [
    ("users", "created_at"), ("profiles", "timezone"),
    ("reminders", "user_id"), ("reminders", "kind"), ("reminders", "text"),
    ("reminders", "next_run_at"), ("reminders", "active"),
    ("events", "user_id"), ("events", "name"), ("events", "payload"),
    ("events", "occurred_at"), ("shown_materials", "user_id"),
    ("shown_materials", "material_id"), ("shown_materials", "shown_at"),
])
async def test_required_fields_reject_null(schema_db, table, column):
    await seed_all(schema_db)
    with pytest.raises(asyncpg.NotNullViolationError):
        await schema_db.execute(
            f'UPDATE "{table}" SET "{column}" = NULL WHERE user_id = $1', USER_ID,
        )
