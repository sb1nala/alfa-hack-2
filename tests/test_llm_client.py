import json

import pytest
from aiohttp import web

from app.llm import client as llm
from app.llm.client import LLMClient, LLMError, parse_json


def completion(content):
    return web.json_response({"choices": [{"message": {"role": "assistant", "content": content}}]})


class FakeApi:
    """Локальный сервер вместо LLM API: отвечает по очереди заготовленными ответами."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    async def handle(self, request):
        self.requests.append({"headers": dict(request.headers), "body": await request.json()})
        response = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return response() if callable(response) else response


@pytest.fixture
async def start_api():
    runners = []

    async def start(*responses, **client_options):
        api = FakeApi(*responses)
        app = web.Application()
        app.router.add_post("/v1/chat/completions", api.handle)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        runners.append(runner)
        port = site._server.sockets[0].getsockname()[1]
        options = {"retry_delay": 0.0, "timeout": 5.0} | client_options
        client = LLMClient(
            api_key="test-key", model="test-model", base_url=f"http://127.0.0.1:{port}/v1", **options
        )
        return api, client

    yield start
    for runner in runners:
        await runner.cleanup()


MESSAGES = [{"role": "user", "content": "привет"}]
SCHEMA = {"type": "object", "properties": {"kind": {"type": "string"}}}


async def test_returns_text_and_sends_key_model_and_messages(start_api):
    api, client = await start_api(lambda: completion("Здравствуйте"))
    assert await client.complete(MESSAGES) == "Здравствуйте"
    [request] = api.requests
    assert request["headers"]["Authorization"] == "Bearer test-key"
    assert "OpenAI-Project" not in request["headers"]
    assert request["body"]["model"] == "test-model"
    assert request["body"]["messages"] == MESSAGES
    assert "response_format" not in request["body"]


async def test_project_is_sent_as_header(start_api):
    api, client = await start_api(lambda: completion("ок"), project="folder-1")
    await client.complete(MESSAGES)
    assert api.requests[0]["headers"]["OpenAI-Project"] == "folder-1"


async def test_schema_is_requested_and_json_is_parsed(start_api):
    api, client = await start_api(lambda: completion('{"kind": "answer"}'))
    assert await client.complete(MESSAGES, SCHEMA) == {"kind": "answer"}
    response_format = api.requests[0]["body"]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["schema"] == SCHEMA


async def test_api_without_schema_support_gets_a_second_request_without_it(start_api):
    api, client = await start_api(
        lambda: web.Response(status=400, text="response_format is not supported"),
        lambda: completion('```json\n{"kind": "answer"}\n```'),
    )
    assert await client.complete(MESSAGES, SCHEMA) == {"kind": "answer"}
    assert "response_format" in api.requests[0]["body"]
    assert "response_format" not in api.requests[1]["body"]
    await client.complete(MESSAGES, SCHEMA)
    assert "response_format" not in api.requests[2]["body"]  # запомнили, больше не просим


async def test_bad_request_without_schema_is_an_error(start_api):
    _, client = await start_api(lambda: web.Response(status=400, text="bad model"))
    with pytest.raises(LLMError, match="400"):
        await client.complete(MESSAGES)


async def test_temporary_failure_is_retried(start_api):
    api, client = await start_api(
        lambda: web.Response(status=503), lambda: web.Response(status=429), lambda: completion("ок")
    )
    assert await client.complete(MESSAGES) == "ок"
    assert len(api.requests) == 3


async def test_gives_up_after_all_retries(start_api):
    api, client = await start_api(lambda: web.Response(status=503), retries=1)
    with pytest.raises(LLMError, match="недоступен"):
        await client.complete(MESSAGES)
    assert len(api.requests) == 2


async def test_wrong_key_is_not_retried_and_names_the_variable(start_api):
    api, client = await start_api(lambda: web.Response(status=401))
    with pytest.raises(LLMError, match="LLM_API_KEY"):
        await client.complete(MESSAGES)
    assert len(api.requests) == 1


@pytest.mark.parametrize(
    "response",
    [
        lambda: web.Response(text="not json"),
        lambda: web.json_response({"choices": []}),
        lambda: completion("   "),
        lambda: web.json_response({"choices": [{"message": {"content": None}}]}),
    ],
)
async def test_unusable_response_is_an_error(start_api, response):
    _, client = await start_api(response)
    with pytest.raises(LLMError):
        await client.complete(MESSAGES)


async def test_unreachable_server_is_an_error():
    client = LLMClient(
        api_key="k", model="m", base_url="http://127.0.0.1:9/v1", retries=1, retry_delay=0.0, timeout=2.0
    )
    with pytest.raises(LLMError, match="недоступен"):
        await client.complete(MESSAGES)


async def test_schema_answer_that_is_not_json_is_an_error(start_api):
    _, client = await start_api(lambda: completion("Просто текст без JSON"))
    with pytest.raises(LLMError, match="не JSON"):
        await client.complete(MESSAGES, SCHEMA)


@pytest.mark.parametrize(
    "content",
    [
        '{"kind": "answer"}',
        '  {"kind": "answer"}\n',
        '```json\n{"kind": "answer"}\n```',
        '```\n{"kind": "answer"}\n```',
        'Вот ответ: {"kind": "answer"} — готово',
    ],
)
def test_parse_json_accepts_common_wrappings(content):
    assert parse_json(content) == {"kind": "answer"}


@pytest.mark.parametrize("content", ["", "текст", "[1, 2]", '"строка"', "{не json}"])
def test_parse_json_rejects_everything_else(content):
    with pytest.raises(LLMError):
        parse_json(content)


async def test_module_level_complete_needs_configuration(start_api):
    llm.configure(None)
    assert not llm.is_configured()
    with pytest.raises(LLMError, match="LLM_API_KEY"):
        await llm.complete(MESSAGES)
    _, client = await start_api(lambda: completion(json.dumps({"kind": "answer"})))
    llm.configure(client)
    try:
        assert llm.is_configured()
        assert await llm.complete(MESSAGES, SCHEMA) == {"kind": "answer"}
    finally:
        llm.configure(None)
