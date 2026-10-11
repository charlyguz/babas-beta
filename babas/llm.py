import json
import logging
import os
import re
from typing import Any, Optional, Protocol, runtime_checkable

import openai
from openai import AsyncOpenAI

log = logging.getLogger("babas.llm")

DEFAULT_MODEL = "gpt-5.2"
DEFAULT_TIMEOUT = 60.0
_FENCE = re.compile(r"^```[A-Za-z0-9_-]*[ \t]*\r?\n?|\r?\n?```[ \t]*$")
_SAFE_CODE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


class LLMError(Exception):
    pass


@runtime_checkable
class LLMClient(Protocol):
    async def complete_json(self, system: str, user: str, schema: Optional[dict] = None) -> dict: ...


def extract_json(text: str) -> dict:
    cleaned = _FENCE.sub("", (text or "").strip()).strip()
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


def _describe_error(exc: openai.OpenAIError) -> str:
    # Never use exc.message or exc.body: providers echo parts of the key or the request there.
    if isinstance(exc, openai.APITimeoutError):
        return "The LLM provider did not answer in time."
    if isinstance(exc, openai.APIConnectionError):
        return "Could not reach the LLM provider (connection error)."
    if isinstance(exc, openai.APIStatusError):
        status = exc.status_code
        if isinstance(exc, openai.AuthenticationError):
            text = "The LLM provider rejected the API key (HTTP 401)."
        elif isinstance(exc, openai.PermissionDeniedError):
            text = "The LLM provider denied access (HTTP 403)."
        elif isinstance(exc, openai.NotFoundError):
            text = "The LLM model or endpoint was not found (HTTP 404); check BABAS_LLM_MODEL."
        elif isinstance(exc, openai.RateLimitError):
            text = "The LLM provider is rate limiting or out of quota (HTTP 429)."
        elif isinstance(exc, openai.InternalServerError):
            text = f"The LLM provider had a server error (HTTP {status})."
        else:
            text = f"The LLM provider rejected the request (HTTP {status})."
        code = getattr(exc, "code", None)
        if isinstance(code, str) and _SAFE_CODE.match(code):
            text += f" Code: {code}."
        return text
    return f"The LLM request failed ({type(exc).__name__})."


class OpenAIClient:
    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None, base_url: Optional[str] = None):
        self._api_key = api_key
        self._base_url = base_url
        self.model = model or os.getenv("BABAS_LLM_MODEL") or DEFAULT_MODEL
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            api_key = self._api_key or os.getenv("OPENAI_API_KEY")
            if not api_key:
                raise LLMError("OPENAI_API_KEY is not set. Put it in .env or the environment.")
            self._client = AsyncOpenAI(api_key=api_key, base_url=self._base_url, timeout=DEFAULT_TIMEOUT)
        return self._client

    async def complete_json(self, system: str, user: str, schema: Optional[dict] = None) -> dict:
        client = self._get_client()
        log.debug("LLM request: model=%s system_chars=%d user_chars=%d", self.model, len(system), len(user))
        try:
            response = await client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                response_format={"type": "json_object"},
            )
        except openai.OpenAIError as exc:
            raise LLMError(_describe_error(exc)) from None
        choices = getattr(response, "choices", None)
        if not choices:
            raise LLMError("The model returned no answer.")
        message = choices[0].message
        content = getattr(message, "content", None)
        if not content or not content.strip():
            if getattr(message, "refusal", None):
                raise LLMError("The model refused to answer.")
            raise LLMError("The model returned an empty answer.")
        return extract_json(content)

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.close()
