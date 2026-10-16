import asyncio
import logging
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, List, Optional

import openai
import pytest
from openai import AsyncOpenAI

from babas import llm
from babas.config import Settings
from babas.llm import (
    DEFAULT_MODEL,
    AnthropicClient,
    LLMClient,
    LLMConfigError,
    LLMError,
    OpenAIClient,
    OpenAICompatibleClient,
    extract_json,
    make_llm,
)
from fake_openai_server import CHAT_PATH, FakeOpenAIServer

ROOT = Path(__file__).resolve().parent.parent
SECRET_KEY = "sk-test-SECRET-0123456789"
SECRET_BODY = "my CURP is GOMC800101HDFRRL09"
SCHEMA = {"type": "object", "properties": {"steps": {"type": "array"}}}
ANTHROPIC_HINT = "Install the Anthropic extra: pip install 'babas[anthropic]'"


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "BABAS_LLM_MODEL", "BABAS_LLM_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


@pytest.fixture
def server():
    with FakeOpenAIServer() as running:
        yield running


@pytest.fixture
async def make_client():
    made: List[Any] = []

    def build(target: FakeOpenAIServer, **overrides: Any) -> OpenAICompatibleClient:
        options = {"base_url": target.base_url, "api_key": SECRET_KEY, "model": "test-model", "json_mode": "auto"}
        options.update(overrides)
        client = OpenAICompatibleClient(**options)
        made.append(client)
        return client

    yield build
    for client in made:
        await client.close()


class RecordingOpenAI(AsyncOpenAI):
    built: List[dict] = []

    def __init__(self, **kwargs: Any) -> None:
        RecordingOpenAI.built.append(kwargs)
        super().__init__(**kwargs)


@pytest.fixture
def recording_openai(monkeypatch: pytest.MonkeyPatch):
    RecordingOpenAI.built = []
    monkeypatch.setattr(llm, "AsyncOpenAI", RecordingOpenAI)
    return RecordingOpenAI


@pytest.fixture
def no_openai_client(monkeypatch: pytest.MonkeyPatch) -> None:
    class MustNotBeBuilt:
        def __init__(self, **kwargs: Any) -> None:
            raise AssertionError("AsyncOpenAI must not be built when the configuration is incomplete")

    monkeypatch.setattr(llm, "AsyncOpenAI", MustNotBeBuilt)


@pytest.mark.parametrize(
    "text,expected",
    [
        ('{"steps": []}', {"steps": []}),
        ('  \n{"a": 1}\n  ', {"a": 1}),
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('```\n{"a": 1}\n```', {"a": 1}),
        ('```JSON {"a": 1}```', {"a": 1}),
        ('Aqui esta el plan:\n```json\n{"a": 1}\n```\nSuerte.', {"a": 1}),
        ('Sure! {"a": 1} Hope that helps.', {"a": 1}),
        ('{"a": {"b": {"c": [1, {"d": 2}]}}, "e": "}"}', {"a": {"b": {"c": [1, {"d": 2}]}}, "e": "}"}),
        ('Use {curly} words: {"a": 1}', {"a": 1}),
        ('{"text": "hola \\"mundo\\" {x}"}', {"text": 'hola "mundo" {x}'}),
        ('{"explanation": "Voy a abrir la página"}', {"explanation": "Voy a abrir la página"}),
        ('<think>I could return {"x": 1} or not</think>\n{"a": 1}', {"a": 1}),
        ('Let me think about {braces}.\n</think>\n\n```json\n{"a": 1}\n```', {"a": 1}),
    ],
)
def test_extract_json_accepts(text: str, expected: dict) -> None:
    assert extract_json(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   \n",
        "no json here",
        "{",
        "}{",
        '{"a": 1',
        '{"a": }',
        "{'a': 1}",
        "```json\n```",
        '["a", "b"]',
        "[1, 2, 3]",
        "<think>only thoughts {x}</think>",
    ],
)
def test_extract_json_rejects(text: str) -> None:
    with pytest.raises(LLMError):
        extract_json(text)


def test_extract_json_error_does_not_echo_the_text() -> None:
    with pytest.raises(LLMError) as info:
        extract_json('{"curp": "GOMC800101HDFRRL09", oops}')
    assert "GOMC800101HDFRRL09" not in str(info.value)


def test_extract_json_always_returns_a_dict() -> None:
    assert extract_json('prefix [1, {"a": [2]}] suffix') == {"a": [2]}


async def test_auto_falls_back_through_every_mode_and_caches_the_winner(server, make_client) -> None:
    server.reject_types = {"json_schema", "json_object"}
    server.queue('{"n": 1}', '{"n": 2}')
    client = make_client(server)
    assert client.selected_mode is None
    assert await client.complete_json("s", "u", SCHEMA) == {"n": 1}
    assert server.format_types == ["json_schema", "json_object", None]
    assert client.selected_mode == "none"
    assert "response_format" not in server.requests[-1].body
    assert await client.complete_json("s", "u", SCHEMA) == {"n": 2}
    assert server.format_types[3:] == [None]


async def test_auto_stops_at_json_object_and_caches_it(server, make_client) -> None:
    server.reject_types = {"json_schema"}
    server.queue('{"n": 1}', '{"n": 2}')
    client = make_client(server)
    await client.complete_json("s", "u", SCHEMA)
    await client.complete_json("s", "u", SCHEMA)
    assert server.format_types == ["json_schema", "json_object", "json_object"]
    assert client.selected_mode == "json_object"


async def test_auto_keeps_json_schema_when_the_provider_accepts_it(server, make_client) -> None:
    server.queue('{"n": 1}', '{"n": 2}')
    client = make_client(server)
    await client.complete_json("s", "u", SCHEMA)
    await client.complete_json("s", "u", SCHEMA)
    assert server.format_types == ["json_schema", "json_schema"]
    assert client.selected_mode == "json_schema"


async def test_auto_without_a_schema_starts_at_json_object(server, make_client) -> None:
    server.reject_types = {"json_object"}
    server.queue('{"n": 1}')
    client = make_client(server)
    assert await client.complete_json("s", "u") == {"n": 1}
    assert server.format_types == ["json_object", None]


async def test_auto_also_falls_back_on_http_422(server, make_client) -> None:
    server.queue_error(422, "Unprocessable")
    server.queue('{"n": 1}')
    client = make_client(server)
    assert await client.complete_json("s", "u", SCHEMA) == {"n": 1}
    assert server.format_types == ["json_schema", "json_object"]


async def test_auto_gives_up_after_the_last_mode_and_caches_nothing(server, make_client) -> None:
    server.reject_types = {"json_schema", "json_object"}
    server.queue_error(400, "Bad request")
    client = make_client(server)
    with pytest.raises(LLMError, match="rejected the request"):
        await client.complete_json("s", "u", SCHEMA)
    assert server.format_types == ["json_schema", "json_object", None]
    assert client.selected_mode is None


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("json_schema", {"type": "json_schema", "json_schema": {"name": "babas_plan", "schema": SCHEMA, "strict": False}}),
        ("json_object", {"type": "json_object"}),
        ("none", None),
    ],
)
async def test_each_json_mode_sends_the_right_response_format(server, make_client, mode: str, expected: Optional[dict]) -> None:
    server.queue('{"n": 1}')
    client = make_client(server, json_mode=mode)
    assert await client.complete_json("s", "u", SCHEMA) == {"n": 1}
    (request,) = server.requests
    if expected is None:
        assert "response_format" not in request.body
    else:
        assert request.body["response_format"] == expected


async def test_json_schema_mode_without_a_schema_sends_json_object(server, make_client) -> None:
    server.queue('{"n": 1}')
    client = make_client(server, json_mode="json_schema")
    await client.complete_json("s", "u")
    assert server.format_types == ["json_object"]


@pytest.mark.parametrize("mode", ["json_schema", "json_object"])
async def test_explicit_modes_never_fall_back(server, make_client, mode: str) -> None:
    server.reject_types = {mode}
    client = make_client(server, json_mode=mode)
    with pytest.raises(LLMError, match="rejected the request"):
        await client.complete_json("s", "u", SCHEMA)
    assert server.format_types == [mode]


async def test_request_shape_authorization_and_base_url(server, make_client) -> None:
    server.queue('{"n": 1}')
    client = make_client(server, model="qwen2.5:7b")
    await client.complete_json("SYSTEM TEXT", "USER TEXT", SCHEMA)
    (request,) = server.requests
    assert request.method == "POST" and request.path == CHAT_PATH
    assert request.headers["authorization"] == f"Bearer {SECRET_KEY}"
    assert request.body["model"] == "qwen2.5:7b"
    assert request.body["messages"] == [
        {"role": "system", "content": "SYSTEM TEXT"},
        {"role": "user", "content": "USER TEXT"},
    ]


async def test_client_is_built_lazily_once_with_the_right_options(server, make_client, recording_openai) -> None:
    server.queue('{"n": 1}', '{"n": 2}')
    client = make_client(server, api_key=None, timeout=12.5)
    assert recording_openai.built == []
    await client.complete_json("s", "u")
    await client.complete_json("s", "u")
    (kwargs,) = recording_openai.built
    assert kwargs == {"base_url": server.base_url, "api_key": "not-needed", "timeout": 12.5, "max_retries": 2}


async def test_local_endpoint_works_without_a_key(server) -> None:
    server.queue('{"n": 1}')
    settings = Settings.from_env({"BABAS_LLM_BASE_URL": server.base_url, "BABAS_LLM_MODEL": "local-model"})
    client = make_llm(settings)
    try:
        assert await client.complete_json("s", "u", SCHEMA) == {"n": 1}
    finally:
        await client.close()
    (request,) = server.requests
    assert request.headers["authorization"] == "Bearer not-needed"
    assert request.body["model"] == "local-model"


async def test_openai_cloud_without_a_key_fails_on_the_first_call(no_openai_client) -> None:
    client = make_llm(Settings.from_env({}))
    with pytest.raises(LLMConfigError) as info:
        await client.complete_json("s", "u", SCHEMA)
    assert "BABAS_LLM_API_KEY" in str(info.value) and "OPENAI_API_KEY" in str(info.value)
    assert isinstance(info.value, LLMError)
    await client.close()


async def test_openai_cloud_with_a_key_from_the_environment_is_accepted(no_openai_client) -> None:
    settings = Settings.from_env({"OPENAI_API_KEY": SECRET_KEY})
    assert settings.llm_api_key == SECRET_KEY
    client = make_llm(settings)
    assert isinstance(client, OpenAICompatibleClient)
    assert client.base_url is None and client.model == DEFAULT_MODEL


@pytest.mark.parametrize(
    "status,requests_sent,fragment",
    [
        (401, 1, "API key"),
        (403, 1, "denied"),
        (404, 1, "not found"),
        (429, 3, "rate limiting"),
        (500, 3, "server error"),
    ],
)
async def test_other_errors_do_not_try_the_next_mode(
    server, make_client, status: int, requests_sent: int, fragment: str
) -> None:
    server.queue_error(status, f"Incorrect API key provided: {SECRET_KEY}. Input was: {SECRET_BODY}", times=3)
    client = make_client(server)
    with pytest.raises(LLMError) as info:
        await client.complete_json("s", f"user says {SECRET_BODY}", SCHEMA)
    text = str(info.value)
    assert fragment in text
    assert SECRET_KEY not in text and SECRET_BODY not in text and "Incorrect" not in text
    assert info.value.__cause__ is None
    assert server.format_types == ["json_schema"] * requests_sent
    assert client.selected_mode is None


async def test_safe_error_codes_are_kept_and_unsafe_ones_dropped(server, make_client) -> None:
    server.queue_error(401, "x", code="invalid_api_key")
    server.queue_error(400, "x", code=f"leak {SECRET_BODY}")
    client = make_client(server, json_mode="json_object")
    with pytest.raises(LLMError) as info:
        await client.complete_json("s", "u")
    assert "invalid_api_key" in str(info.value) and len(str(info.value)) < 200
    with pytest.raises(LLMError) as info:
        await client.complete_json("s", "u")
    assert SECRET_BODY not in str(info.value)


def test_unknown_openai_errors_are_described_without_their_text() -> None:
    text = llm._describe_error(openai.OpenAIError(f"weird {SECRET_KEY}"))
    assert SECRET_KEY not in text and "OpenAIError" in text


async def test_a_slow_provider_times_out(server, make_client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm, "MAX_RETRIES", 0)
    server.delay = 3
    client = make_client(server, timeout=0.2, json_mode="json_object")
    with pytest.raises(LLMError, match="in time"):
        await client.complete_json("s", "u")


async def test_an_unreachable_provider_is_a_connection_error(make_client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm, "MAX_RETRIES", 0)
    with FakeOpenAIServer() as closed:
        url = closed.base_url
    client = OpenAICompatibleClient(base_url=url, api_key=SECRET_KEY, model="m", json_mode="json_object")
    try:
        with pytest.raises(LLMError, match="connection"):
            await client.complete_json("s", "u")
    finally:
        await client.close()


async def test_fenced_and_prose_wrapped_answers_are_parsed(server, make_client) -> None:
    server.queue('```json\n{"steps": []}\n```', 'Claro:\n{"steps": [{"action": "done"}]}\nListo.')
    client = make_client(server)
    assert await client.complete_json("s", "u") == {"steps": []}
    assert await client.complete_json("s", "u") == {"steps": [{"action": "done"}]}


async def test_reasoning_prefix_is_ignored(server, make_client) -> None:
    server.queue('<think>The answer may look like {"steps": 1}</think>\n{"steps": []}')
    assert await make_client(server).complete_json("s", "u") == {"steps": []}


@pytest.mark.parametrize("content", [None, "", "   \n"])
async def test_empty_content_raises(server, make_client, content: Optional[str]) -> None:
    server.queue_completion(content)
    with pytest.raises(LLMError, match="empty"):
        await make_client(server).complete_json("s", "u")


async def test_refusal_and_missing_choices_raise(server, make_client) -> None:
    server.queue_completion(None, refusal="I cannot help")
    server.queue_reply(200, {"id": "x", "object": "chat.completion", "created": 1, "model": "m", "choices": []})
    client = make_client(server)
    with pytest.raises(LLMError, match="refused"):
        await client.complete_json("s", "u")
    with pytest.raises(LLMError, match="no answer"):
        await client.complete_json("s", "u")


async def test_invalid_json_raises(server, make_client) -> None:
    server.queue("Lo siento, no puedo.", '{"steps": [oops]}')
    client = make_client(server)
    with pytest.raises(LLMError):
        await client.complete_json("s", "u")
    with pytest.raises(LLMError, match="not valid JSON"):
        await client.complete_json("s", "u")


async def test_a_truncated_answer_says_so(server, make_client) -> None:
    server.queue_completion('{"steps": [{"action": "cl', finish_reason="length")
    with pytest.raises(LLMError, match="cut off"):
        await make_client(server).complete_json("s", "u")


async def test_the_selected_mode_is_logged_without_the_prompt_or_the_key(
    server, make_client, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="babas.llm")
    server.reject_types = {"json_schema"}
    server.queue('{"n": 1}')
    await make_client(server).complete_json("SYSTEM SECRET PROMPT", f"user {SECRET_BODY}", SCHEMA)
    assert "json mode selected: json_object" in caplog.text
    assert SECRET_KEY not in caplog.text and SECRET_BODY not in caplog.text and "SYSTEM SECRET PROMPT" not in caplog.text


async def test_cancellation_propagates(server, make_client) -> None:
    server.delay = 5
    client = make_client(server)
    task = asyncio.ensure_future(client.complete_json("s", "u"))
    for _ in range(100):
        if server.requests:
            break
        await asyncio.sleep(0.02)
    assert server.requests
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_close_closes_the_underlying_client_and_allows_reuse(server, make_client, recording_openai) -> None:
    server.queue('{"n": 1}', '{"n": 2}')
    client = make_client(server)
    await client.close()
    await client.complete_json("s", "u")
    await client.close()
    await client.complete_json("s", "u")
    assert len(recording_openai.built) == 2
    await client.close()
    await client.close()


def test_an_unknown_json_mode_is_a_configuration_error() -> None:
    with pytest.raises(LLMConfigError, match="json_mode"):
        OpenAICompatibleClient(base_url=None, api_key=None, model="m", json_mode="bogus")


def test_clients_satisfy_the_protocol() -> None:
    assert isinstance(OpenAICompatibleClient(base_url=None, api_key=None, model="m"), LLMClient)
    assert isinstance(OpenAIClient(), LLMClient)
    assert issubclass(OpenAIClient, OpenAICompatibleClient)


def test_openai_client_model_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    assert OpenAIClient().model == DEFAULT_MODEL == "gpt-5.2"
    monkeypatch.setenv("BABAS_LLM_MODEL", "qwen3:14b")
    assert OpenAIClient().model == "qwen3:14b"
    assert OpenAIClient(model="mine").model == "mine"


async def test_openai_client_keeps_the_p2_wire_behavior(server, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", SECRET_KEY)
    server.queue('{"steps": []}')
    client = OpenAIClient(base_url=server.base_url)
    try:
        assert await client.complete_json("s", "u", SCHEMA) == {"steps": []}
    finally:
        await client.close()
    (request,) = server.requests
    assert request.body["model"] == "gpt-5.2"
    assert request.body["response_format"] == {"type": "json_object"}
    assert request.headers["authorization"] == f"Bearer {SECRET_KEY}"


async def test_openai_client_explicit_arguments_win_over_the_environment(server, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BABAS_LLM_MODEL", "from-env")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    server.queue("{}")
    client = OpenAIClient(api_key="sk-explicit", model="explicit-model", base_url=server.base_url)
    try:
        await client.complete_json("s", "u")
    finally:
        await client.close()
    (request,) = server.requests
    assert request.body["model"] == "explicit-model"
    assert request.headers["authorization"] == "Bearer sk-explicit"


async def test_openai_client_reads_the_key_when_first_needed(server, monkeypatch: pytest.MonkeyPatch) -> None:
    client = OpenAIClient(base_url=server.base_url)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-later")
    server.queue("{}")
    try:
        await client.complete_json("s", "u")
    finally:
        await client.close()
    assert server.requests[0].headers["authorization"] == "Bearer sk-later"


async def test_openai_client_without_a_key_or_base_url_raises_at_call_time(no_openai_client) -> None:
    client = OpenAIClient()
    with pytest.raises(LLMConfigError) as info:
        await client.complete_json("s", "u")
    assert "OPENAI_API_KEY" in str(info.value)


async def test_openai_client_with_a_local_base_url_needs_no_key(server) -> None:
    server.queue("{}")
    client = OpenAIClient(base_url=server.base_url)
    try:
        await client.complete_json("s", "u")
    finally:
        await client.close()
    assert server.requests[0].headers["authorization"] == "Bearer not-needed"


def test_make_llm_builds_the_openai_compatible_client_from_settings() -> None:
    settings = Settings.from_env(
        {
            "BABAS_LLM_BASE_URL": "http://localhost:11434/v1",
            "BABAS_LLM_MODEL": "qwen2.5:7b",
            "BABAS_LLM_JSON_MODE": "json_object",
            "BABAS_LLM_TIMEOUT": "12",
        }
    )
    client = make_llm(settings)
    assert type(client) is OpenAICompatibleClient
    assert client.base_url == "http://localhost:11434/v1"
    assert (client.model, client.json_mode, client.timeout) == ("qwen2.5:7b", "json_object", 12.0)
    default = make_llm(Settings.from_env({}))
    assert (default.base_url, default.model, default.json_mode) == (None, "gpt-5.2", "auto")


def build_fake_anthropic() -> types.ModuleType:
    module = types.ModuleType("anthropic")

    class AnthropicError(Exception):
        pass

    class APIError(AnthropicError):
        pass

    class APIConnectionError(APIError):
        pass

    class APITimeoutError(APIConnectionError):
        pass

    class APIStatusError(APIError):
        def __init__(self, message: str, status_code: int) -> None:
            super().__init__(message)
            self.status_code = status_code
            self.message = message

    class AsyncAnthropic:
        instances: List["AsyncAnthropic"] = []
        outcomes: List[Any] = []

        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.calls: List[dict] = []
            self.closed = False
            self.messages = SimpleNamespace(create=self._create)
            AsyncAnthropic.instances.append(self)

        async def _create(self, **kwargs: Any) -> Any:
            self.calls.append(kwargs)
            outcome = AsyncAnthropic.outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        async def close(self) -> None:
            self.closed = True

    for item in (AnthropicError, APIError, APIConnectionError, APITimeoutError, APIStatusError, AsyncAnthropic):
        setattr(module, item.__name__, item)
    return module


@pytest.fixture
def fake_anthropic(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    module = build_fake_anthropic()
    monkeypatch.setitem(sys.modules, "anthropic", module)
    return module


def anthropic_message(*blocks: Any, stop_reason: str = "end_turn") -> Any:
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason)


def text_block(text: str) -> Any:
    return SimpleNamespace(type="text", text=text)


def anthropic_client(**overrides: Any) -> AnthropicClient:
    options = {"api_key": SECRET_KEY, "model": "test-model"}
    options.update(overrides)
    return AnthropicClient(**options)


def test_anthropic_missing_package_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "anthropic", None)
    with pytest.raises(LLMConfigError) as info:
        AnthropicClient(api_key=SECRET_KEY, model="test-model")
    assert str(info.value) == ANTHROPIC_HINT
    assert isinstance(info.value, LLMError)


async def test_make_llm_does_not_raise_when_the_anthropic_package_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "anthropic", None)
    settings = Settings.from_env(
        {"BABAS_LLM_PROVIDER": "anthropic", "BABAS_LLM_MODEL": "test-model", "ANTHROPIC_API_KEY": SECRET_KEY}
    )
    client = make_llm(settings)
    assert isinstance(client, LLMClient)
    with pytest.raises(LLMConfigError, match="babas\\[anthropic\\]"):
        await client.complete_json("s", "u")
    await client.close()


async def test_make_llm_picks_anthropic_and_defers_configuration_errors(fake_anthropic) -> None:
    ready = make_llm(
        Settings.from_env(
            {"BABAS_LLM_PROVIDER": "anthropic", "BABAS_LLM_MODEL": "test-model", "BABAS_LLM_API_KEY": SECRET_KEY}
        )
    )
    assert isinstance(ready, AnthropicClient) and ready.model == "test-model"

    no_key = make_llm(Settings.from_env({"BABAS_LLM_PROVIDER": "anthropic", "BABAS_LLM_MODEL": "test-model"}))
    with pytest.raises(LLMConfigError, match="ANTHROPIC_API_KEY"):
        await no_key.complete_json("s", "u")

    no_model = make_llm(Settings.from_env({"BABAS_LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": SECRET_KEY}))
    with pytest.raises(LLMConfigError, match="BABAS_LLM_MODEL"):
        await no_model.complete_json("s", "u")
    assert fake_anthropic.AsyncAnthropic.instances == []


def test_anthropic_client_checks_the_package_before_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "anthropic", None)
    with pytest.raises(LLMConfigError, match="Install the Anthropic extra"):
        AnthropicClient(api_key=None, model=None)


async def test_anthropic_request_and_response(fake_anthropic) -> None:
    fake_anthropic.AsyncAnthropic.outcomes = [
        anthropic_message(
            SimpleNamespace(type="thinking", thinking="{not json}"), text_block('```json\n{"a":'), text_block(" 1}\n```")
        )
    ]
    client = anthropic_client(base_url="http://localhost:9/anthropic", timeout=7.5)
    assert fake_anthropic.AsyncAnthropic.instances == []
    assert await client.complete_json("SYSTEM TEXT", "USER TEXT", SCHEMA) == {"a": 1}
    (built,) = fake_anthropic.AsyncAnthropic.instances
    assert built.kwargs == {"api_key": SECRET_KEY, "base_url": "http://localhost:9/anthropic", "timeout": 7.5, "max_retries": 2}
    assert built.calls == [
        {
            "model": "test-model",
            "max_tokens": 4096,
            "system": "SYSTEM TEXT",
            "messages": [{"role": "user", "content": "USER TEXT"}],
        }
    ]


async def test_anthropic_bad_answers_raise(fake_anthropic) -> None:
    fake_anthropic.AsyncAnthropic.outcomes = [
        anthropic_message(),
        anthropic_message(text_block("Lo siento.")),
        anthropic_message(text_block("declined"), stop_reason="refusal"),
        anthropic_message(text_block('{"steps": [{"act'), stop_reason="max_tokens"),
    ]
    client = anthropic_client()
    with pytest.raises(LLMError, match="empty"):
        await client.complete_json("s", "u")
    with pytest.raises(LLMError, match="no JSON"):
        await client.complete_json("s", "u")
    with pytest.raises(LLMError, match="refused"):
        await client.complete_json("s", "u")
    with pytest.raises(LLMError, match="cut off"):
        await client.complete_json("s", "u")


@pytest.mark.parametrize(
    "error_factory,fragment",
    [
        (lambda m: m.APIStatusError(f"bad key {SECRET_KEY} {SECRET_BODY}", 401), "API key"),
        (lambda m: m.APIStatusError(f"nope {SECRET_BODY}", 403), "denied"),
        (lambda m: m.APIStatusError("missing", 404), "not found"),
        (lambda m: m.APIStatusError("slow down", 429), "rate limiting"),
        (lambda m: m.APIStatusError(f"overloaded {SECRET_KEY}", 529), "server error (HTTP 529)"),
        (lambda m: m.APIStatusError(f"bad {SECRET_BODY}", 400), "rejected the request"),
        (lambda m: m.APIConnectionError(f"dns {SECRET_KEY}"), "connection"),
        (lambda m: m.APITimeoutError(f"late {SECRET_KEY}"), "in time"),
        (lambda m: m.AnthropicError(f"weird {SECRET_KEY} {SECRET_BODY}"), "request failed"),
    ],
)
async def test_anthropic_errors_become_safe_llm_errors(fake_anthropic, error_factory: Callable, fragment: str) -> None:
    fake_anthropic.AsyncAnthropic.outcomes = [error_factory(fake_anthropic)]
    client = anthropic_client()
    with pytest.raises(LLMError) as info:
        await client.complete_json("s", "u")
    text = str(info.value)
    assert fragment in text
    assert SECRET_KEY not in text and SECRET_BODY not in text
    assert info.value.__cause__ is None


async def test_anthropic_cancellation_propagates_and_close_closes(fake_anthropic) -> None:
    fake_anthropic.AsyncAnthropic.outcomes = [asyncio.CancelledError()]
    client = anthropic_client()
    await client.close()
    with pytest.raises(asyncio.CancelledError):
        await client.complete_json("s", "u")
    await client.close()
    assert fake_anthropic.AsyncAnthropic.instances[0].closed is True


def test_import_and_construction_are_silent_without_a_key() -> None:
    code = (
        "from babas.llm import OpenAIClient, OpenAICompatibleClient, AnthropicClient, LLMClient, LLMError, make_llm\n"
        "OpenAIClient()\n"
        "OpenAIClient(model='x', base_url='http://localhost:1/v1')\n"
        "OpenAICompatibleClient(base_url=None, api_key=None, model='x')\n"
        "from babas.config import Settings\n"
        "make_llm(Settings.from_env({}))\n"
        "make_llm(Settings.from_env({'BABAS_LLM_PROVIDER': 'anthropic'}))\n"
    )
    env = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"}
    done = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == "" and done.stderr == ""
