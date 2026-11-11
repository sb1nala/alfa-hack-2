"""Isolated real PostgreSQL pool for storage API tests; never uses public."""

import os
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from app.storage import db
from app.storage.db import SCHEMA_PATH


@pytest_asyncio.fixture
async def schema_db():
    database_url = os.environ.get("DATABASE_URL")
    if database_url is None:
        pytest.skip("DATABASE_URL не задан: нужен настоящий PostgreSQL для тестов схемы")
    # Do not load .env or log the URL (it may contain credentials).
    connection = await asyncpg.connect(
        database_url, timeout=10,
        server_settings={"search_path": "pg_catalog", "timezone": "UTC"},
    )
    schema = "test_storage_" + uuid4().hex
    created = False
    try:
        # Only a generated identifier is interpolated; all data uses parameters.
        await connection.execute(f'CREATE SCHEMA "{schema}"')
        created = True
        await connection.execute(f'SET search_path TO "{schema}"')
        assert await connection.fetchval("SELECT current_schema()") == schema
        await connection.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
        yield connection
    finally:
        try:
            if created:
                await connection.execute('SET search_path TO pg_catalog')
                await connection.execute(f'DROP SCHEMA "{schema}" CASCADE')
        finally:
            await connection.close()


@pytest_asyncio.fixture
async def storage_db(schema_db, monkeypatch):
    schema = await schema_db.fetchval("SELECT current_schema()")
    pools = []

    async def open_pool():
        pool = await asyncpg.create_pool(
            os.environ["DATABASE_URL"], min_size=1, max_size=5, timeout=10,
            server_settings={"search_path": schema, "timezone": "UTC"},
        )
        pools.append(pool)
        assert await pool.fetchval("SELECT current_schema()") == schema
        monkeypatch.setattr(db, "pool", lambda: pool)
        return pool

    pool = await open_pool()
    try:
        yield pool, open_pool
    finally:
        for pool in pools:
            await pool.close()
