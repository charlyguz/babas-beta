import asyncio
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Optional

import openai
import pytest

from babas import llm
from babas.llm import DEFAULT_MODEL, LLMClient, LLMError, OpenAIClient, extract_json

try:
    import httpx2 as http_lib
except ImportError:
    import httpx as http_lib

ROOT = Path(__file__).resolve().parent.parent
SECRET_KEY = "sk-test-SECRET-0123456789"
SECRET_BODY = "my CURP is GOMC800101HDFRRL09"


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
    ],
)
def test_extract_json_accepts(text: str, expected: dict) -> None:
    assert extract_json(text) == expected


@pytest.mark.parametrize(
    "text",
    ["", "   \n", "no json here", "{", "}{", '{"a": 1', '{"a": }', "{'a': 1}", "```json\n```", '["a", "b"]', "[1, 2, 3]"],
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


class FakeCompletions:
    def __init__(self, outcomes: List[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls: List[dict] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeAsyncOpenAI:
    instances: List["FakeAsyncOpenAI"] = []
    outcomes: List[Any] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.chat = SimpleNamespace(completions=FakeCompletions(FakeAsyncOpenAI.outcomes))
        self.closed = False
        FakeAsyncOpenAI.instances.append(self)

    async def close(self) -> None:
        self.closed = True


def completion(content: Optional[str], refusal: Optional[str] = None) -> Any:
    message = SimpleNamespace(content=content, refusal=refusal)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


@pytest.fixture
def fake_openai(monkeypatch: pytest.MonkeyPatch):
    FakeAsyncOpenAI.instances = []
    FakeAsyncOpenAI.outcomes = []
    monkeypatch.setattr(llm, "AsyncOpenAI", FakeAsyncOpenAI)
    monkeypatch.setenv("OPENAI_API_KEY", SECRET_KEY)
    monkeypatch.delenv("BABAS_LLM_MODEL", raising=False)
    return FakeAsyncOpenAI


def make_status_error(cls: type, status: int, message: str = "boom", body: Any = None) -> Exception:
    request = http_lib.Request("POST", "https://api.example.test/v1/chat/completions")
    response = http_lib.Response(status, request=request)
    return cls(message, response=response, body=body)


def make_request() -> Any:
    return http_lib.Request("POST", "https://api.example.test/v1/chat/completions")


async def test_complete_json_success(fake_openai) -> None:
    fake_openai.outcomes = [completion('{"steps": [{"action": "done", "value": "Listo."}]}')]
    client = OpenAIClient()
    result = await client.complete_json("SYSTEM TEXT", "USER TEXT", {"type": "object"})
    assert result == {"steps": [{"action": "done", "value": "Listo."}]}
    (call,) = fake_openai.instances[0].chat.completions.calls
    assert call["model"] == DEFAULT_MODEL == "gpt-5.2"
    assert call["response_format"] == {"type": "json_object"}
    assert call["messages"] == [
        {"role": "system", "content": "SYSTEM TEXT"},
        {"role": "user", "content": "USER TEXT"},
    ]
    assert "response_format" in call and "schema" not in call


async def test_client_is_built_lazily_once_with_the_key(fake_openai) -> None:
    fake_openai.outcomes = [completion("{}"), completion("{}")]
    client = OpenAIClient(base_url="http://localhost:11434/v1")
    assert fake_openai.instances == []
    await client.complete_json("s", "u")
    await client.complete_json("s", "u")
    (built,) = fake_openai.instances
    assert built.kwargs["api_key"] == SECRET_KEY
    assert built.kwargs["base_url"] == "http://localhost:11434/v1"
    assert built.kwargs["timeout"] > 0


async def test_explicit_arguments_win_over_the_environment(fake_openai, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BABAS_LLM_MODEL", "from-env")
    fake_openai.outcomes = [completion("{}")]
    client = OpenAIClient(api_key="sk-explicit", model="explicit-model")
    await client.complete_json("s", "u")
    built = fake_openai.instances[0]
    assert built.kwargs["api_key"] == "sk-explicit"
    assert built.chat.completions.calls[0]["model"] == "explicit-model"


def test_model_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BABAS_LLM_MODEL", raising=False)
    assert OpenAIClient().model == "gpt-5.2"
    monkeypatch.setenv("BABAS_LLM_MODEL", "qwen3:14b")
    assert OpenAIClient().model == "qwen3:14b"
    assert OpenAIClient(model="mine").model == "mine"


async def test_fenced_answer_is_parsed(fake_openai) -> None:
    fake_openai.outcomes = [completion('```json\n{"steps": []}\n```')]
    assert await OpenAIClient().complete_json("s", "u") == {"steps": []}


@pytest.mark.parametrize("content", [None, "", "   \n"])
async def test_empty_content_raises(fake_openai, content: Optional[str]) -> None:
    fake_openai.outcomes = [completion(content)]
    with pytest.raises(LLMError, match="empty"):
        await OpenAIClient().complete_json("s", "u")


async def test_refusal_and_missing_choices_raise(fake_openai) -> None:
    fake_openai.outcomes = [completion(None, refusal="I cannot help"), SimpleNamespace(choices=[])]
    client = OpenAIClient()
    with pytest.raises(LLMError, match="refused"):
        await client.complete_json("s", "u")
    with pytest.raises(LLMError, match="no answer"):
        await client.complete_json("s", "u")


async def test_invalid_json_content_raises(fake_openai) -> None:
    fake_openai.outcomes = [completion("Lo siento, no puedo.")]
    with pytest.raises(LLMError):
        await OpenAIClient().complete_json("s", "u")


@pytest.mark.parametrize(
    "error_factory,fragment",
    [
        (lambda: make_status_error(openai.AuthenticationError, 401), "API key"),
        (lambda: make_status_error(openai.PermissionDeniedError, 403), "denied"),
        (lambda: make_status_error(openai.NotFoundError, 404), "not found"),
        (lambda: make_status_error(openai.RateLimitError, 429), "rate limiting"),
        (lambda: make_status_error(openai.BadRequestError, 400), "rejected the request"),
        (lambda: make_status_error(openai.InternalServerError, 500), "server error"),
        (lambda: openai.APIConnectionError(request=make_request()), "connection"),
        (lambda: openai.APITimeoutError(request=make_request()), "in time"),
    ],
)
async def test_api_errors_become_llm_errors(fake_openai, error_factory, fragment: str) -> None:
    fake_openai.outcomes = [error_factory()]
    with pytest.raises(LLMError) as info:
        await OpenAIClient().complete_json("s", "u")
    assert fragment in str(info.value)
    assert info.value.__cause__ is None


async def test_error_messages_never_leak_the_key_or_the_request(fake_openai) -> None:
    body = {
        "message": f"Incorrect API key provided: {SECRET_KEY}. Input was: {SECRET_BODY}",
        "code": "invalid_api_key",
        "type": "invalid_request_error",
    }
    error = make_status_error(openai.AuthenticationError, 401, message=body["message"], body=body)
    fake_openai.outcomes = [error]
    with pytest.raises(LLMError) as info:
        await OpenAIClient().complete_json("system", f"user says {SECRET_BODY}")
    text = str(info.value)
    assert SECRET_KEY not in text and SECRET_BODY not in text and "Incorrect" not in text
    assert "invalid_api_key" in text
    assert len(text) < 200


async def test_unsafe_error_codes_are_not_echoed(fake_openai) -> None:
    body = {"message": "x", "code": f"leak {SECRET_BODY}"}
    fake_openai.outcomes = [make_status_error(openai.BadRequestError, 400, body=body)]
    with pytest.raises(LLMError) as info:
        await OpenAIClient().complete_json("s", "u")
    assert SECRET_BODY not in str(info.value)


async def test_unknown_openai_errors_are_converted(fake_openai) -> None:
    fake_openai.outcomes = [openai.OpenAIError(f"weird {SECRET_KEY}")]
    with pytest.raises(LLMError) as info:
        await OpenAIClient().complete_json("s", "u")
    assert SECRET_KEY not in str(info.value)


async def test_missing_key_raises_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    class MustNotBeBuilt:
        def __init__(self, **kwargs: Any) -> None:
            raise AssertionError("AsyncOpenAI must not be built without a key")

    monkeypatch.setattr(llm, "AsyncOpenAI", MustNotBeBuilt)
    client = OpenAIClient()
    with pytest.raises(LLMError) as info:
        await client.complete_json("s", "u")
    assert "OPENAI_API_KEY" in str(info.value)
    assert len(str(info.value)) < 100


async def test_key_is_read_when_first_needed(fake_openai, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = OpenAIClient()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-later")
    fake_openai.outcomes = [completion("{}")]
    await client.complete_json("s", "u")
    assert fake_openai.instances[0].kwargs["api_key"] == "sk-later"


async def test_cancellation_propagates(fake_openai) -> None:
    fake_openai.outcomes = [asyncio.CancelledError()]
    with pytest.raises(asyncio.CancelledError):
        await OpenAIClient().complete_json("s", "u")


async def test_close_closes_the_underlying_client(fake_openai) -> None:
    fake_openai.outcomes = [completion("{}")]
    client = OpenAIClient()
    await client.close()
    await client.complete_json("s", "u")
    await client.close()
    assert fake_openai.instances[0].closed is True
    await client.close()


def test_open_ai_client_satisfies_the_protocol() -> None:
    assert isinstance(OpenAIClient(), LLMClient)


def test_import_and_construction_are_silent_without_a_key() -> None:
    code = (
        "from babas.llm import OpenAIClient, LLMClient, LLMError, extract_json\n"
        "OpenAIClient()\n"
        "OpenAIClient(model='x', base_url='http://localhost:1/v1')\n"
    )
    env = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"}
    done = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == "" and done.stderr == ""
