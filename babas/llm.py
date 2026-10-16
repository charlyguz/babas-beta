import json
import logging
import os
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Protocol, runtime_checkable

import openai
from openai import AsyncOpenAI

if TYPE_CHECKING:
    from babas.config import Settings

log = logging.getLogger("babas.llm")

DEFAULT_MODEL = "gpt-5.2"
DEFAULT_TIMEOUT = 60.0
MAX_RETRIES = 2
ANTHROPIC_MAX_TOKENS = 4096
JSON_MODES = ("auto", "json_schema", "json_object", "none")
_FALLBACK_ORDER = ("json_schema", "json_object", "none")
_FENCE = re.compile(r"^```[A-Za-z0-9_-]*[ \t]*\r?\n?|\r?\n?```[ \t]*$")
_SAFE_CODE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
_ANTHROPIC_HINT = "Install the Anthropic extra: pip install 'babas[anthropic]'"


class LLMError(Exception):
    pass


class LLMConfigError(LLMError):
    pass


@runtime_checkable
class LLMClient(Protocol):
    async def complete_json(self, system: str, user: str, schema: Optional[dict] = None) -> dict: ...


def _strip_reasoning(text: str) -> str:
    # Reasoning models (Qwen, DeepSeek-R1) put a <think> block before the answer, and it may contain braces.
    _, separator, tail = text.rpartition("</think>")
    return tail if separator and "{" in tail else text


def extract_json(text: str) -> dict:
    cleaned = _FENCE.sub("", _strip_reasoning(text or "").strip()).strip()
    if not cleaned:
        raise LLMError("The model returned an empty answer.")
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end <= start:
        raise LLMError("The model answer has no JSON object.")
    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        data = _first_object(cleaned, start)
        if data is None:
            raise LLMError(f"The model answer is not valid JSON ({exc.msg} at character {exc.pos}).") from None
    if not isinstance(data, dict):
        raise LLMError("The model answer is not a JSON object.")
    return data


def _first_object(text: str, start: int) -> Optional[dict]:
    # Fallback for prose with stray braces around the real object.
    decoder = json.JSONDecoder()
    position = start
    while position != -1:
        try:
            data, _ = decoder.raw_decode(text, position)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            return data
        position = text.find("{", position + 1)
    return None


def _status_text(status: int) -> str:
    if status == 401:
        return "The LLM provider rejected the API key (HTTP 401)."
    if status == 403:
        return "The LLM provider denied access (HTTP 403)."
    if status == 404:
        return "The LLM model or endpoint was not found (HTTP 404); check BABAS_LLM_MODEL."
    if status == 429:
        return "The LLM provider is rate limiting or out of quota (HTTP 429)."
    if status >= 500:
        return f"The LLM provider had a server error (HTTP {status})."
    return f"The LLM provider rejected the request (HTTP {status})."


def _describe_error(exc: openai.OpenAIError) -> str:
    # Never use exc.message or exc.body: providers echo parts of the key or the request there.
    if isinstance(exc, openai.APITimeoutError):
        return "The LLM provider did not answer in time."
    if isinstance(exc, openai.APIConnectionError):
        return "Could not reach the LLM provider (connection error)."
    if isinstance(exc, openai.APIStatusError):
        text = _status_text(exc.status_code)
        code = getattr(exc, "code", None)
        if isinstance(code, str) and _SAFE_CODE.match(code):
            text += f" Code: {code}."
        return text
    return f"The LLM request failed ({type(exc).__name__})."


def _describe_anthropic_error(anthropic: Any, exc: BaseException) -> str:
    if isinstance(exc, anthropic.APITimeoutError):
        return "The LLM provider did not answer in time."
    if isinstance(exc, anthropic.APIConnectionError):
        return "Could not reach the LLM provider (connection error)."
    if isinstance(exc, anthropic.APIStatusError):
        return _status_text(exc.status_code)
    return f"The LLM request failed ({type(exc).__name__})."


class OpenAICompatibleClient:
    def __init__(
        self,
        *,
        base_url: Optional[str],
        api_key: Optional[str],
        model: str,
        json_mode: str = "auto",
        timeout: float = DEFAULT_TIMEOUT,
        require_key: bool = False,
        key_env: str = "OPENAI_API_KEY",
    ) -> None:
        if json_mode not in JSON_MODES:
            raise LLMConfigError(f"json_mode must be one of {', '.join(JSON_MODES)}.")
        self.base_url = base_url
        self.model = model
        self.json_mode = json_mode
        self.timeout = timeout
        self.selected_mode: Optional[str] = None if json_mode == "auto" else json_mode
        self._api_key = api_key
        self._require_key = require_key
        self._key_env = key_env
        self._client: Any = None

    def _resolve_key(self) -> Optional[str]:
        return self._api_key

    def _get_client(self, key: Optional[str]) -> Any:
        if self._client is None:
            self._client = AsyncOpenAI(
                base_url=self.base_url, api_key=key or "not-needed", timeout=self.timeout, max_retries=MAX_RETRIES
            )
        return self._client

    def _modes_to_try(self, schema: Optional[dict]) -> List[str]:
        if self.json_mode == "json_schema":
            return ["json_schema" if schema else "json_object"]
        if self.json_mode != "auto":
            return [self.json_mode]
        start = _FALLBACK_ORDER.index(self.selected_mode) if self.selected_mode else 0
        return [mode for mode in _FALLBACK_ORDER[start:] if mode != "json_schema" or schema]

    def _request(self, mode: str, messages: List[Dict[str, str]], schema: Optional[dict]) -> Dict[str, Any]:
        request: Dict[str, Any] = {"model": self.model, "messages": messages}
        if mode == "json_schema":
            request["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "babas_plan", "schema": schema, "strict": False},
            }
        elif mode == "json_object":
            request["response_format"] = {"type": "json_object"}
        return request

    async def complete_json(self, system: str, user: str, schema: Optional[dict] = None) -> dict:
        key = self._resolve_key()
        if self._require_key and not key:
            raise LLMConfigError(f"No API key is set: put BABAS_LLM_API_KEY (or {self._key_env}) in .env or the environment.")
        client = self._get_client(key)
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        modes = self._modes_to_try(schema)
        log.debug(
            "LLM request: model=%s json_mode=%s system_chars=%d user_chars=%d",
            self.model,
            self.json_mode,
            len(system),
            len(user),
        )
        for index, mode in enumerate(modes):
            try:
                response = await client.chat.completions.create(**self._request(mode, messages, schema))
            except (openai.BadRequestError, openai.UnprocessableEntityError) as exc:
                if index + 1 < len(modes):
                    log.debug("LLM json mode %s was rejected (HTTP %s); trying %s", mode, exc.status_code, modes[index + 1])
                    continue
                raise LLMError(_describe_error(exc)) from None
            except openai.OpenAIError as exc:
                raise LLMError(_describe_error(exc)) from None
            if self.json_mode == "auto" and mode != self.selected_mode:
                log.debug("LLM json mode selected: %s", mode)
                self.selected_mode = mode
            return self._parse(response)
        raise LLMError("The LLM request was not sent.")

    @staticmethod
    def _parse(response: Any) -> dict:
        choices = getattr(response, "choices", None)
        if not choices:
            raise LLMError("The model returned no answer.")
        choice = choices[0]
        message = choice.message
        content = getattr(message, "content", None)
        if not isinstance(content, str) or not content.strip():
            if getattr(message, "refusal", None):
                raise LLMError("The model refused to answer.")
            raise LLMError("The model returned an empty answer.")
        try:
            return extract_json(content)
        except LLMError:
            if getattr(choice, "finish_reason", None) == "length":
                raise LLMError("The model answer was cut off (output length limit).") from None
            raise

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.close()


class OpenAIClient(OpenAICompatibleClient):
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        *,
        json_mode: str = "json_object",
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            model=model or os.getenv("BABAS_LLM_MODEL") or DEFAULT_MODEL,
            json_mode=json_mode,
            timeout=timeout,
            require_key=not base_url,
        )

    def _resolve_key(self) -> Optional[str]:
        return self._api_key or os.getenv("OPENAI_API_KEY")


class AnthropicClient:
    def __init__(
        self,
        *,
        api_key: Optional[str],
        model: Optional[str],
        base_url: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        try:
            import anthropic
        except ImportError:
            raise LLMConfigError(_ANTHROPIC_HINT) from None
        if not model:
            raise LLMConfigError("BABAS_LLM_MODEL is not set: the Anthropic provider needs a model id.")
        if not api_key:
            raise LLMConfigError("No API key is set: put BABAS_LLM_API_KEY (or ANTHROPIC_API_KEY) in .env or the environment.")
        self._anthropic = anthropic
        self._api_key = api_key
        self.base_url = base_url
        self.model = model
        self.timeout = timeout
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                self._client = self._anthropic.AsyncAnthropic(
                    api_key=self._api_key, base_url=self.base_url, timeout=self.timeout, max_retries=MAX_RETRIES
                )
            except Exception:
                raise LLMConfigError("Could not set up the Anthropic client; check BABAS_LLM_BASE_URL.") from None
        return self._client

    async def complete_json(self, system: str, user: str, schema: Optional[dict] = None) -> dict:
        client = self._get_client()
        log.debug("LLM request: model=%s system_chars=%d user_chars=%d", self.model, len(system), len(user))
        try:
            response = await client.messages.create(
                model=self.model,
                max_tokens=ANTHROPIC_MAX_TOKENS,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except self._anthropic.AnthropicError as exc:
            raise LLMError(_describe_anthropic_error(self._anthropic, exc)) from None
        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason == "refusal":
            raise LLMError("The model refused to answer.")
        blocks = getattr(response, "content", None) or []
        text = "".join(block.text for block in blocks if getattr(block, "type", None) == "text")
        try:
            return extract_json(text)
        except LLMError:
            if stop_reason == "max_tokens":
                raise LLMError("The model answer was cut off (output length limit).") from None
            raise

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.close()


class _UnavailableClient:
    def __init__(self, error: LLMConfigError) -> None:
        self._message = str(error)

    async def complete_json(self, system: str, user: str, schema: Optional[dict] = None) -> dict:
        raise LLMConfigError(self._message)

    async def close(self) -> None:
        return None


def make_llm(settings: "Settings") -> LLMClient:
    if settings.llm_provider == "anthropic":
        try:
            return AnthropicClient(
                api_key=settings.llm_api_key,
                model=settings.llm_model,
                base_url=settings.llm_base_url,
                timeout=settings.llm_timeout,
            )
        except LLMConfigError as exc:
            log.debug("LLM provider is not usable yet: %s", exc)
            return _UnavailableClient(exc)
    return OpenAICompatibleClient(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model or DEFAULT_MODEL,
        json_mode=settings.llm_json_mode,
        timeout=settings.llm_timeout,
        require_key=settings.needs_api_key,
    )
