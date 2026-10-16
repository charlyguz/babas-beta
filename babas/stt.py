import asyncio
import base64
import importlib.util
import io
import ipaddress
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Optional, Protocol, Tuple, TypeVar, runtime_checkable
from urllib.parse import urlsplit

import openai
from openai import AsyncOpenAI

from babas.llm import _SAFE_CODE

if TYPE_CHECKING:
    from babas.config import Settings

log = logging.getLogger("babas.stt")

T = TypeVar("T")

ERROR_CODES = ("busy", "not_configured", "provider", "bad_audio", "empty", "timeout")
MAX_TRANSCRIPT_CHARS = 4000
# The service worker answers "timeout" after 90 s. Retry-After waits and a stuck local model are cut by these deadlines:
# the HTTP backends stop first (their own error text and log win), Session stops anything else at STT_DEADLINE_S.
STT_TIMEOUT_S = 30.0
STT_MAX_RETRIES = 1
STT_HTTP_DEADLINE_S = 70.0
STT_DEADLINE_S = 75.0
CANCEL_GRACE_S = 2.0
DEFAULT_WHISPER_MODEL = "whisper-1"
DEFAULT_LOCAL_MODEL = "small"
GEMINI_HOST = "generativelanguage.googleapis.com"
OPENAI_HOST = "api.openai.com"
NO_SPEECH_MARK = "[SIN_VOZ]"
WAV_MIMES = frozenset({"audio/wav", "audio/x-wav", "audio/wave"})

BAD_AUDIO_TEXT = "No pude leer la grabación. Intente grabarla de nuevo."
STT_BUSY_TEXT = "Todavía estoy escuchando otra grabación. Espere un momento e intente de nuevo."
EMPTY_TEXT = "No escuché ninguna voz. Presione «Hablar» e intente de nuevo."
PROVIDER_TEXT = "No pude entender la grabación por un problema con el servicio de voz. Intente de nuevo o escriba su petición."
TIMEOUT_TEXT = "El servicio de voz tardó demasiado en responder. Intente de nuevo en un momento o escriba su petición."
AUTH_TEXT = "El servicio de voz no aceptó la clave. Pida a quien instaló BABAS que revise BABAS_STT_API_KEY."
RATE_TEXT = "El servicio de voz está saturado o sin saldo. Espere un momento e intente de nuevo."
CONNECTION_TEXT = "No pude conectarme con el servicio de voz. Revise su conexión a internet e intente de nuevo."
NOT_CONFIGURED_TEXT = "El dictado por voz no está configurado. Pida a quien instaló BABAS que revise BABAS_STT_PROVIDER."
OFF_TEXT = "El dictado por voz está desactivado. Pida a quien instaló BABAS que cambie BABAS_STT_PROVIDER para activarlo."
NO_BACKEND_TEXT = (
    "El dictado por voz necesita un servicio de voz. "
    "Pida a quien instaló BABAS que configure BABAS_STT_PROVIDER (local, whisper o chat) y, si hace falta, BABAS_STT_BASE_URL."
)
KEY_TEXT = "Falta la clave del servicio de voz. Pida a quien instaló BABAS que configure BABAS_STT_API_KEY."
MODEL_TEXT = "Falta indicar el modelo del servicio de voz. Pida a quien instaló BABAS que configure BABAS_STT_MODEL."
CLIENT_TEXT = "No pude preparar el servicio de voz. Pida a quien instaló BABAS que revise BABAS_STT_BASE_URL."
LOCAL_MISSING_TEXT = (
    "Falta instalar el reconocimiento de voz local. "
    'Pida a quien instaló BABAS que ejecute: pip install -e ".[stt]"'
)

PROMPT_ES = (
    "Transcribe literalmente lo que dice la persona en el audio. "
    "Responde SOLO con la transcripción, sin comillas ni comentarios. "
    f"Si no hay voz en el audio, responde exactamente {NO_SPEECH_MARK}"
)
PROMPT_EN = (
    "Transcribe literally what the person says in the audio. "
    "Answer ONLY with the transcription, with no quotes or comments. "
    f"If there is no speech in the audio, answer exactly {NO_SPEECH_MARK}"
)

_NO_SPEECH = re.compile(r"\[\s*SIN[_ ]VOZ\s*\]", re.IGNORECASE)
_OPENING_QUOTES = "\"'“‘«`”"
_CLOSING_QUOTES = "\"'”’»`“"
_ESCAPES = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x9b[0-?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b[PX^_][^\x1b]*\x1b\\"
    r"|\x1b[ -/]*[0-~]"
)
_INVISIBLE = re.compile(r"[\x00-\x1f\x7f-\x9f\u061c\u200b\u200e\u200f\u202a-\u202e\u2060\u2066-\u2069\ufeff]")


class STTError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class STTConfigError(STTError):
    def __init__(self, message: str) -> None:
        super().__init__("not_configured", message)


@runtime_checkable
class SpeechToText(Protocol):
    async def transcribe(self, audio: bytes, mime: str, language: str) -> str: ...

    async def close(self) -> None: ...


def _encloses_whole_text(cleaned: str) -> bool:
    if len(cleaned) < 2 or cleaned[0] not in _OPENING_QUOTES or cleaned[-1] not in _CLOSING_QUOTES:
        return False
    inner = cleaned[1:-1]
    return cleaned[0] not in inner and cleaned[-1] not in inner


def clean_transcript(text: str) -> str:
    # Speech never contains control characters, but a chat model may write anything: the result reaches a terminal and a text box.
    plain = _INVISIBLE.sub(" ", _ESCAPES.sub("", text or ""))
    cleaned = " ".join(plain.split())
    while _encloses_whole_text(cleaned):
        cleaned = cleaned[1:-1].strip()
    return cleaned[:MAX_TRANSCRIPT_CHARS].rstrip()


def _consume(task: "asyncio.Future[Any]") -> None:
    if not task.cancelled():
        task.exception()


async def with_deadline(awaitable: Awaitable[T], seconds: float) -> T:
    # asyncio.wait, not wait_for: on Python < 3.12 wait_for can swallow a cancellation (Stop) that arrives with the result.
    task = asyncio.ensure_future(awaitable)
    task.add_done_callback(_consume)
    try:
        done, _pending = await asyncio.wait({task}, timeout=seconds)
    except BaseException:
        task.cancel()
        raise
    if done:
        return task.result()
    task.cancel()
    await asyncio.wait({task}, timeout=CANCEL_GRACE_S)
    raise STTError("timeout", TIMEOUT_TEXT)


def _language_code(language: Optional[str]) -> Optional[str]:
    primary = (language or "").strip().lower().split("-")[0]
    return primary if len(primary) == 2 and primary.isalpha() else None


def _host(base_url: Optional[str]) -> Optional[str]:
    if not base_url:
        return None
    try:
        return (urlsplit(base_url).hostname or "").lower().rstrip(".") or None
    except ValueError:
        return None


_DEFAULT_PORTS = {"http": 80, "https": 443}
_PRIVATE_SUFFIXES = (".local", ".localhost", ".internal", ".lan")


def _endpoint(base_url: Optional[str]) -> Optional[Tuple[str, str, int]]:
    # An unset base URL is the OpenAI default.
    if not base_url:
        return "https", OPENAI_HOST, 443
    try:
        parts = urlsplit(base_url.strip())
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower().rstrip(".")
    if scheme not in _DEFAULT_PORTS or not host:
        return None
    return scheme, host, _DEFAULT_PORTS[scheme] if port is None else port


def _same_endpoint(first: Optional[str], second: Optional[str]) -> bool:
    endpoint = _endpoint(first)
    return endpoint is not None and endpoint == _endpoint(second)


def _is_local_host(host: Optional[str]) -> bool:
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "." not in host or host.endswith(_PRIVATE_SUFFIXES)
    return address.is_loopback or address.is_private or address.is_link_local


def _check_audio(audio: bytes, mime: str) -> None:
    if not audio or not isinstance(mime, str) or mime.strip().lower() not in WAV_MIMES:
        raise STTError("bad_audio", BAD_AUDIO_TEXT)


def _status_text(status: int) -> str:
    if status == 401:
        return "The speech service rejected the API key (HTTP 401); check BABAS_STT_API_KEY."
    if status == 403:
        return "The speech service denied access (HTTP 403); check BABAS_STT_API_KEY."
    if status == 404:
        return "The speech endpoint or model was not found (HTTP 404); check BABAS_STT_BASE_URL and BABAS_STT_MODEL."
    if status == 429:
        return "The speech service is rate limiting or out of quota (HTTP 429)."
    if status >= 500:
        return f"The speech service had a server error (HTTP {status})."
    if status in (400, 415, 422):
        return (
            f"The speech service rejected the request (HTTP {status}); the model or endpoint may not accept audio, "
            "check BABAS_STT_MODEL and BABAS_STT_BASE_URL."
        )
    return f"The speech service rejected the request (HTTP {status})."


def _describe_error(exc: openai.OpenAIError) -> str:
    # Never use exc.message or exc.body: providers echo parts of the key or the request there.
    if isinstance(exc, openai.APITimeoutError):
        return "The speech service did not answer in time."
    if isinstance(exc, openai.APIConnectionError):
        return "Could not reach the speech service; check BABAS_STT_BASE_URL."
    if isinstance(exc, openai.APIStatusError):
        text = _status_text(exc.status_code)
        code = getattr(exc, "code", None)
        if isinstance(code, str) and _SAFE_CODE.match(code):
            text += f" Code: {code}."
        return text
    return f"The speech request failed ({type(exc).__name__})."


def _provider_error(exc: BaseException) -> STTError:
    if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return STTError("provider", AUTH_TEXT)
    if isinstance(exc, openai.RateLimitError):
        return STTError("provider", RATE_TEXT)
    if isinstance(exc, openai.APIConnectionError):
        return STTError("provider", CONNECTION_TEXT)
    return STTError("provider", PROVIDER_TEXT)


@dataclass(frozen=True)
class SttPlan:
    backend: Optional[str]
    model: str = ""
    base_url: Optional[str] = None
    api_key: Optional[str] = field(default=None, repr=False)
    language: Optional[str] = None
    problem: Optional[str] = None
    reason: str = ""


def _blocked(
    problem: str, reason: str, language: Optional[str], model: str = "", base_url: Optional[str] = None
) -> SttPlan:
    return SttPlan(None, model, base_url, language=language, problem=problem, reason=reason)


def _needs_key(base_url: Optional[str]) -> bool:
    return not _is_local_host(_host(base_url))


def _auto_backend(base_url: Optional[str], openai_llm: bool) -> Optional[str]:
    host = _host(base_url)
    if host == GEMINI_HOST:
        return "chat"
    if base_url is None:
        return "whisper" if openai_llm else None
    return "whisper" if host == OPENAI_HOST else "chat"


def resolve_stt(settings: "Settings") -> SttPlan:
    language = settings.stt_language or None
    provider = settings.stt_provider
    if provider == "off":
        return _blocked(OFF_TEXT, "voice input is turned off (BABAS_STT_PROVIDER=off)", language)
    # An Anthropic key, model or endpoint must never be sent to a speech service by fallback.
    openai_llm = settings.llm_provider == "openai"
    base_url = settings.stt_base_url or (settings.llm_base_url if openai_llm else None)
    # The LLM key and model follow only when the speech endpoint is the LLM endpoint (same scheme, host and port).
    shares_llm = openai_llm and _same_endpoint(base_url, settings.llm_base_url)
    api_key = settings.stt_api_key or (settings.llm_api_key if shares_llm else None)
    if provider == "auto":
        auto = _auto_backend(base_url, openai_llm)
        if auto is None:
            reason = "no speech backend: set BABAS_STT_PROVIDER, or BABAS_STT_BASE_URL and BABAS_STT_MODEL"
            return _blocked(NO_BACKEND_TEXT, reason, language)
        provider = auto
    if provider == "local":
        return SttPlan("local", settings.stt_model or DEFAULT_LOCAL_MODEL, language=language)
    if provider not in ("chat", "whisper"):
        return _blocked(NOT_CONFIGURED_TEXT, f"unknown BABAS_STT_PROVIDER {provider!r}", language)
    if settings.stt_model:
        model = settings.stt_model
    elif provider == "whisper":
        model = DEFAULT_WHISPER_MODEL
    else:
        model = (settings.llm_model if shares_llm else None) or ""
    if not model:
        reason = "BABAS_STT_MODEL is not set"
        if openai_llm and settings.llm_model and not shares_llm:
            reason += "; the LLM model is only reused when the speech endpoint is the LLM endpoint"
        return _blocked(MODEL_TEXT, reason, language, base_url=base_url)
    if not api_key and _needs_key(base_url):
        reason = "BABAS_STT_API_KEY is not set"
        if openai_llm and settings.llm_api_key and not shares_llm:
            reason += "; the LLM key is never sent to a different endpoint"
        return _blocked(KEY_TEXT, reason, language, model, base_url)
    return SttPlan(provider, model, base_url, api_key, language)


def stt_status(settings: "Settings") -> Tuple[bool, Optional[str]]:
    try:
        plan = resolve_stt(settings)
    except Exception as exc:
        log.debug("could not resolve the speech settings: %s", type(exc).__name__)
        return False, NOT_CONFIGURED_TEXT
    if plan.problem is None and plan.backend == "local" and _local_package_missing():
        return False, LOCAL_MISSING_TEXT
    return plan.problem is None, plan.problem


def _local_package_missing() -> bool:
    try:
        return importlib.util.find_spec("faster_whisper") is None
    except (ImportError, ValueError):
        return False


class _OpenAIAudioSTT:
    backend = ""
    check_marker = False

    def __init__(
        self,
        *,
        base_url: Optional[str],
        api_key: Optional[str],
        model: str,
        language: Optional[str] = None,
        timeout: float = STT_TIMEOUT_S,
        max_retries: int = STT_MAX_RETRIES,
        deadline: float = STT_HTTP_DEADLINE_S,
        require_key: bool = False,
    ) -> None:
        self.base_url = base_url
        self.model = model
        self.language = language
        self.timeout = timeout
        self.max_retries = max_retries
        self.deadline = deadline
        self._api_key = api_key
        self._require_key = require_key
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._require_key and not self._api_key:
            raise STTConfigError(KEY_TEXT)
        if self._client is None:
            try:
                self._client = AsyncOpenAI(
                    base_url=self.base_url,
                    api_key=self._api_key or "not-needed",
                    timeout=self.timeout,
                    max_retries=self.max_retries,
                )
            except Exception as exc:
                log.warning("could not set up the speech client: %s", type(exc).__name__)
                raise STTConfigError(CLIENT_TEXT) from None
        return self._client

    async def _call(self, client: Any, audio: bytes, language: str) -> Optional[str]:
        raise NotImplementedError

    async def transcribe(self, audio: bytes, mime: str, language: str) -> str:
        _check_audio(audio, mime)
        client = self._get_client()
        started = time.monotonic()
        try:
            raw = await with_deadline(self._call(client, audio, self.language or language), self.deadline)
        except STTError as exc:
            if exc.code == "timeout":
                log.warning(
                    "speech-to-text request gave up after %.1f s (%s, model %s)", self.deadline, self.backend, self.model
                )
            raise
        except Exception as exc:
            reason = _describe_error(exc) if isinstance(exc, openai.OpenAIError) else type(exc).__name__
            log.warning("speech-to-text request failed (%s, model %s): %s", self.backend, self.model, reason)
            raise _provider_error(exc) from None
        text = clean_transcript(raw or "")
        if not text or (self.check_marker and _NO_SPEECH.search(text)):
            raise STTError("empty", EMPTY_TEXT)
        log.debug(
            "transcribed with %s (%s): %d audio bytes, %d characters, %.1f s",
            self.backend,
            self.model,
            len(audio),
            len(text),
            time.monotonic() - started,
        )
        return text

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.close()
            except Exception as exc:
                log.debug("closing the speech client failed: %s", type(exc).__name__)


class ChatAudioSTT(_OpenAIAudioSTT):
    backend = "chat"
    check_marker = True

    async def _call(self, client: Any, audio: bytes, language: str) -> Optional[str]:
        prompt = PROMPT_ES if (language or "").lower().startswith("es") else PROMPT_EN
        data = base64.b64encode(audio).decode("ascii")
        response = await client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "input_audio", "input_audio": {"data": data, "format": "wav"}},
                    ],
                }
            ],
        )
        choices = getattr(response, "choices", None)
        if not choices:
            log.warning("speech-to-text request failed (chat, model %s): the model returned no answer", self.model)
            raise STTError("provider", PROVIDER_TEXT)
        message = choices[0].message
        content = getattr(message, "content", None)
        if not isinstance(content, str) and getattr(message, "refusal", None):
            log.warning("speech-to-text request failed (chat, model %s): the model refused to answer", self.model)
            raise STTError("provider", PROVIDER_TEXT)
        if getattr(choices[0], "finish_reason", None) == "length":
            log.warning("speech-to-text answer was cut short (chat, model %s): finish_reason=length", self.model)
        return content if isinstance(content, str) else None


class WhisperApiSTT(_OpenAIAudioSTT):
    backend = "whisper"

    async def _call(self, client: Any, audio: bytes, language: str) -> Optional[str]:
        options: dict = {"model": self.model, "file": ("audio.wav", audio, "audio/wav"), "response_format": "json"}
        code = _language_code(language)
        if code:
            options["language"] = code
        response = await client.audio.transcriptions.create(**options)
        if isinstance(response, str):
            return response
        text = getattr(response, "text", None)
        if not isinstance(text, str):
            log.warning("speech-to-text request failed (whisper, model %s): the answer has no text field", self.model)
            raise STTError("provider", PROVIDER_TEXT)
        return text


class LocalWhisperSTT:
    backend = "local"

    def __init__(self, *, model: str = DEFAULT_LOCAL_MODEL, language: Optional[str] = None) -> None:
        self.model = model
        self.language = language
        self._model: Any = None
        self._announced = False
        self._load_lock = threading.Lock()

    def _load(self) -> Any:
        with self._load_lock:
            if self._model is None:
                try:
                    from faster_whisper import WhisperModel
                except ImportError:
                    raise STTConfigError(LOCAL_MISSING_TEXT) from None
                if not self._announced:
                    self._announced = True
                    log.info("Loading the local speech model %r; the first use may download it.", self.model)
                self._model = WhisperModel(self.model, device="cpu", compute_type="int8")
            return self._model

    def _run(self, audio: bytes, language: Optional[str]) -> str:
        model = self._load()
        segments, _info = model.transcribe(io.BytesIO(audio), language=language, vad_filter=True)
        parts = (segment.text.strip() for segment in segments)
        return " ".join(part for part in parts if part)

    async def transcribe(self, audio: bytes, mime: str, language: str) -> str:
        _check_audio(audio, mime)
        started = time.monotonic()
        try:
            raw = await asyncio.to_thread(self._run, audio, _language_code(self.language or language))
        except STTError:
            raise
        except Exception as exc:
            log.warning("speech-to-text request failed (local, model %s): %s", self.model, type(exc).__name__)
            raise STTError("provider", PROVIDER_TEXT) from None
        text = clean_transcript(raw)
        if not text:
            raise STTError("empty", EMPTY_TEXT)
        log.debug(
            "transcribed with local (%s): %d audio bytes, %d characters, %.1f s",
            self.model,
            len(audio),
            len(text),
            time.monotonic() - started,
        )
        return text

    async def close(self) -> None:
        self._model = None


class _UnavailableSTT:
    def __init__(self, message: str) -> None:
        self._message = message

    async def transcribe(self, audio: bytes, mime: str, language: str) -> str:
        raise STTConfigError(self._message)

    async def close(self) -> None:
        return None


def make_stt(settings: "Settings") -> SpeechToText:
    try:
        plan = resolve_stt(settings)
        if plan.problem is not None:
            level = logging.INFO if plan.problem == OFF_TEXT else logging.WARNING
            log.log(level, "speech-to-text is not usable: %s", plan.reason)
            return _UnavailableSTT(plan.problem)
        log.info("speech-to-text: backend=%s model=%s", plan.backend, plan.model)
        if plan.backend == "local":
            return LocalWhisperSTT(model=plan.model, language=plan.language)
        options = {
            "base_url": plan.base_url,
            "api_key": plan.api_key,
            "model": plan.model,
            "language": plan.language,
            "require_key": _needs_key(plan.base_url),
        }
        return WhisperApiSTT(**options) if plan.backend == "whisper" else ChatAudioSTT(**options)
    except Exception as exc:
        log.warning("speech-to-text setup failed: %s", type(exc).__name__)
        return _UnavailableSTT(NOT_CONFIGURED_TEXT)
