import pytest

from app.storage import db
from app.storage.db import DatabaseError


def test_schema_file_exists_and_is_not_empty():
    assert db.SCHEMA_PATH.read_text(encoding="utf-8").strip()


def test_pool_before_connect_is_an_error():
    with pytest.raises(DatabaseError, match="не подключена"):
        db.pool()


async def test_unreachable_database_gives_up_with_a_clear_error():
    with pytest.raises(DatabaseError, match="недоступна после 2 попыток"):
        await db.connect("postgresql://u:p@127.0.0.1:9/none", attempts=2, delay=0.0)
    with pytest.raises(DatabaseError):
        db.pool()


async def test_close_without_connection_is_harmless():
    await db.close()
