from pathlib import Path

import pytest

from app.config import ConfigError, load_settings


def test_missing_token_names_the_variable():
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        load_settings({})


def test_blank_token_is_rejected():
    with pytest.raises(ConfigError):
        load_settings({"TELEGRAM_BOT_TOKEN": "   "})


def test_defaults_when_only_token_is_set():
    settings = load_settings({"TELEGRAM_BOT_TOKEN": "123:abc"})
    assert settings.telegram_bot_token == "123:abc"
    assert settings.data_dir == Path("data")
    assert settings.default_timezone == "Europe/Moscow"


def test_values_are_read_from_env():
    settings = load_settings(
        {"TELEGRAM_BOT_TOKEN": "t", "LLM_MODEL": "m", "DATA_DIR": "/app/data", "DB_PATH": "/s/b.db"}
    )
    assert settings.llm_model == "m"
    assert settings.data_dir == Path("/app/data")
    assert settings.db_path == Path("/s/b.db")
