"""PostgreSQL: подключение и создание таблиц при старте. Роль 2."""

import asyncio
import logging
from pathlib import Path

import asyncpg

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
_pool: asyncpg.Pool | None = None


class DatabaseError(RuntimeError):
    """База данных недоступна или не приняла подключение."""


async def connect(database_url: str, attempts: int = 10, delay: float = 2.0) -> asyncpg.Pool:
    """Подключиться к базе и применить schema.sql. Ждёт, пока база поднимется."""
    global _pool
    last_error = "нет ответа"
    for attempt in range(attempts):
        if attempt:
            await asyncio.sleep(delay)
        try:
            pool = await asyncpg.create_pool(database_url, min_size=1, max_size=5, timeout=10)
        except (asyncpg.InvalidPasswordError, asyncpg.InvalidCatalogNameError) as error:
            raise DatabaseError(
                f"База данных не приняла подключение: {error}. Проверьте POSTGRES_PASSWORD "
                "в .env; если пароль меняли после первого запуска — удалите том базы: "
                "docker compose down -v."
            ) from error
        except (OSError, asyncio.TimeoutError, asyncpg.PostgresError) as error:
            last_error = f"{type(error).__name__}: {error}"
            log.info("База данных ещё не готова (%s), попытка %d из %d", last_error, attempt + 1, attempts)
            continue
        async with pool.acquire() as connection:
            await connection.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
        _pool = pool
        return pool
    raise DatabaseError(f"База данных недоступна после {attempts} попыток: {last_error}")


def pool() -> asyncpg.Pool:
    """Общий пул подключений. Доступен после connect()."""
    if _pool is None:
        raise DatabaseError("База данных не подключена: вызовите app.storage.db.connect().")
    return _pool


async def close() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
