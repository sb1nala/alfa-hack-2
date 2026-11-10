"""Клиент LLM для OpenAI-совместимого API (по умолчанию Yandex AI Studio). Роль A."""

import asyncio
import json
import logging
import re
from dataclasses import dataclass

import aiohttp

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://ai.api.cloud.yandex.net/v1"
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


class LLMError(RuntimeError):
    """Модель недоступна или вернула то, что нельзя использовать."""


@dataclass
class LLMClient:
    api_key: str
    model: str
    base_url: str = DEFAULT_BASE_URL
    project: str = ""  # для Yandex AI Studio — идентификатор каталога
    timeout: float = 40.0
    retries: int = 2
    retry_delay: float = 1.0
    temperature: float = 0.2
    max_tokens: int = 900
    # Не все совместимые API принимают response_format; после первого отказа больше не просим.
    schema_supported: bool = True

    async def complete(self, messages: list[dict], json_schema: dict | None = None) -> str | dict:
        """Текст ответа модели или разобранный JSON, если передана схема."""
        use_schema = json_schema is not None and self.schema_supported
        try:
            content = await self._request(messages, json_schema if use_schema else None)
        except _BadRequest as error:
            if not use_schema:
                raise LLMError(str(error)) from error
            log.warning("API не принял response_format, дальше прошу JSON только в тексте запроса")
            self.schema_supported = False
            try:
                content = await self._request(messages, None)
            except _BadRequest as second_error:
                raise LLMError(str(second_error)) from second_error
        if json_schema is None:
            return content
        return parse_json(content)

    async def _request(self, messages: list[dict], json_schema: dict | None) -> str:
        body: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if json_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "reply", "schema": json_schema, "strict": True},
            }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if self.project:
            headers["OpenAI-Project"] = self.project
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        timeout = aiohttp.ClientTimeout(total=self.timeout)

        last_error = "нет ответа"
        for attempt in range(self.retries + 1):
            if attempt:
                await asyncio.sleep(self.retry_delay * attempt)
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(url, json=body, headers=headers) as response:
                        text = await response.text()
                        if response.status == 400:
                            raise _BadRequest(f"LLM API отклонил запрос (400): {text[:300]}")
                        if response.status in (401, 403):
                            raise LLMError(
                                f"LLM API не принял ключ ({response.status}). "
                                "Проверьте LLM_API_KEY, LLM_PROJECT и права сервисного аккаунта."
                            )
                        if response.status in RETRY_STATUSES:
                            last_error = f"статус {response.status}"
                            continue
                        if response.status != 200:
                            raise LLMError(f"LLM API вернул {response.status}: {text[:300]}")
                        return _content(text)
            except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                last_error = type(error).__name__
        raise LLMError(f"LLM API недоступен после {self.retries + 1} попыток: {last_error}")


class _BadRequest(Exception):
    pass


def _content(response_text: str) -> str:
    try:
        content = json.loads(response_text)["choices"][0]["message"]["content"]
    except (ValueError, LookupError, TypeError) as error:
        raise LLMError(f"Неожиданный ответ LLM API: {response_text[:300]}") from error
    if not isinstance(content, str) or not content.strip():
        raise LLMError("LLM API вернул пустой ответ")
    return content


def parse_json(content: str) -> dict:
    """Достать JSON-объект из ответа модели: чистый, в ```-ограждении или внутри текста."""
    text = _FENCE.sub("", content.strip()).strip()
    candidates = [text]
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    raise LLMError(f"Модель вернула не JSON-объект: {content[:200]}")


_client: LLMClient | None = None


def configure(client: LLMClient | None) -> None:
    global _client
    _client = client


def is_configured() -> bool:
    return _client is not None


async def complete(messages: list[dict], json_schema: dict | None = None) -> str | dict:
    """Вернуть текст ответа модели или разобранный JSON, если передана схема."""
    if _client is None:
        raise LLMError("LLM не настроен: задайте LLM_API_KEY и LLM_MODEL в .env")
    return await _client.complete(messages, json_schema)
