"""Клиент LLM (OpenAI-совместимый API). Роль A."""


async def complete(messages: list[dict], json_schema: dict | None = None) -> str | dict:
    """Вернуть текст ответа модели или разобранный JSON, если передана схема."""
    raise NotImplementedError
