"""Запуск бота: python -m app.main"""

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError

from app.bot.handlers import router
from app.config import ConfigError, load_settings
from app.kb import search as kb
from app.kb.ingest import DataError

log = logging.getLogger(__name__)


async def run() -> None:
    settings = load_settings()
    log.info("Читаю выгрузку из %s и строю индекс…", settings.data_dir)
    index = kb.load(settings.data_dir)
    log.info("База знаний загружена: %d фрагментов", len(index))
    bot = Bot(settings.telegram_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    await dispatcher.start_polling(bot)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        asyncio.run(run())
    except (ConfigError, DataError) as error:
        sys.exit(f"Ошибка настройки: {error}")
    except TelegramUnauthorizedError:
        sys.exit(
            "Ошибка настройки: Telegram не принял токен. Проверьте TELEGRAM_BOT_TOKEN в файле .env."
        )


if __name__ == "__main__":
    main()
