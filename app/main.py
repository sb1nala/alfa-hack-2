"""Запуск бота: python -m app.main"""

import asyncio
import logging
import sys
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError

from app.bot.handlers import router
from app.config import ConfigError, load_settings
from app.kb import search as kb
from app.kb.ingest import DataError
from app.llm import client as llm
from app.llm.client import LLMClient
from app.reminders import worker
from app.storage import db
from app.storage.db import DatabaseError

log = logging.getLogger(__name__)


async def run() -> None:
    settings = load_settings()
    if not settings.database_url:
        raise ConfigError(
            "Не задана переменная DATABASE_URL. При запуске через docker compose она задаётся "
            "автоматически; для запуска без Docker укажите адрес PostgreSQL."
        )
    log.info("Читаю выгрузку из %s и строю индекс…", settings.data_dir)
    index = kb.load(settings.data_dir)
    log.info("База знаний загружена: %d фрагментов", len(index))
    if settings.llm_enabled:
        llm.configure(
            LLMClient(
                api_key=settings.llm_api_key,
                model=settings.llm_model,
                base_url=settings.llm_base_url,
                project=settings.llm_project,
            )
        )
        log.info("LLM подключён: %s", settings.llm_base_url)
    else:
        log.warning("LLM_API_KEY или LLM_MODEL не заданы: бот отдаёт материалы без текста ответа")
    bot = None
    delivery_task = None
    try:
        await db.connect(settings.database_url)
        log.info("База данных подключена")
        bot = Bot(settings.telegram_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        dispatcher = Dispatcher()
        dispatcher.include_router(router)
        delivery_task = asyncio.create_task(worker.run(bot), name="reminder-delivery")
        # aiogram otherwise closes the session in start_polling's finally,
        # before this worker can be stopped. This scope owns the session.
        await dispatcher.start_polling(bot, close_bot_session=False)
    finally:
        try:
            if delivery_task is not None:
                delivery_task.cancel()
                with suppress(asyncio.CancelledError):
                    await delivery_task
        finally:
            try:
                await db.close()
            finally:
                if bot is not None:
                    await bot.session.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        asyncio.run(run())
    except (ConfigError, DataError, DatabaseError) as error:
        sys.exit(f"Ошибка настройки: {error}")
    except TelegramUnauthorizedError:
        sys.exit(
            "Ошибка настройки: Telegram не принял токен. Проверьте TELEGRAM_BOT_TOKEN в файле .env."
        )


if __name__ == "__main__":
    main()
