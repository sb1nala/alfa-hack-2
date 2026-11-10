"""Настройки из переменных окружения."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class ConfigError(RuntimeError):
    """Не хватает обязательной переменной окружения."""


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    data_dir: Path
    db_path: Path
    default_timezone: str


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ConfigError(
            "Не задана переменная TELEGRAM_BOT_TOKEN. Скопируйте .env.example в .env "
            "и впишите токен бота от @BotFather."
        )
    return Settings(
        telegram_bot_token=token,
        llm_base_url=env.get("LLM_BASE_URL", "").strip(),
        llm_api_key=env.get("LLM_API_KEY", "").strip(),
        llm_model=env.get("LLM_MODEL", "").strip(),
        data_dir=Path(env.get("DATA_DIR", "data")),
        db_path=Path(env.get("DB_PATH", "storage/bot.sqlite3")),
        default_timezone=env.get("DEFAULT_TIMEZONE", "Europe/Moscow").strip() or "Europe/Moscow",
    )
