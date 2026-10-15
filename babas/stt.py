import asyncio
import base64
import io
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional, Protocol, runtime_checkable
from urllib.parse import urlsplit

import openai
from openai import AsyncOpenAI

from babas.llm import _describe_error

if TYPE_CHECKING:
    from babas.config import Settings

log = logging.getLogger("babas.stt")

ERROR_CODES = ("busy", "not_configured", "provider", "bad_audio", "empty")
MAX_TRANSCRIPT_CHARS = 4000
# One attempt plus one retry must fit well inside the 90 s the side panel waits for a transcript.
STT_TIMEOUT_S = 30.0
STT_MAX_RETRIES = 1
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


def clean_transcript(text: str) -> str:
    cleaned = " ".join((text or "").split())
    while len(cleaned) >= 2 and cleaned[0] in _OPENING_QUOTES and cleaned[-1] in _CLOSING_QUOTES:
        cleaned = cleaned[1:-1].strip()
    return cleaned[:MAX_TRANSCRIPT_CHARS].rstrip()


def _language_code(language: Optional[str]) -> Optional[str]:
    primary = (language or "").strip().lower().split("-")[0]
    return primary if len(primary) == 2 and primary.isalpha() else None


def _host(base_url: Optional[str]) -> Optional[str]:
    if not base_url:
        return None
    try:
        return (urlsplit(base_url).hostname or "").lower() or None
    except ValueError:
        return None


def _check_audio(audio: bytes, mime: str) -> None:
    if not audio or not isinstance(mime, str) or mime.strip().lower() not in WAV_MIMES:
        raise STTError("bad_audio", BAD_AUDIO_TEXT)


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
    language: str = "es"
    problem: Optional[str] = None


def _needs_key(base_url: Optional[str]) -> bool:
    return base_url is None or _host(base_url) in (GEMINI_HOST, OPENAI_HOST)


def _auto_backend(base_url: Optional[str], openai_llm: bool) -> Optional[str]:
    host = _host(base_url)
    if host == GEMINI_HOST:
        return "chat"
    if base_url is None:
        return "whisper" if openai_llm else None
    return "whisper" if host == OPENAI_HOST else "chat"


def resolve_stt(settings: "Settings") -> SttPlan:
    language = settings.stt_language or settings.language
    provider = settings.stt_provider
    if provider == "off":
        return SttPlan(None, language=language, problem=OFF_TEXT)
    # An Anthropic key, model or endpoint must never be sent to a speech service by fallback.
    openai_llm = settings.llm_provider == "openai"
    base_url = settings.stt_base_url or (settings.llm_base_url if openai_llm else None)
    api_key = settings.stt_api_key or (settings.llm_api_key if openai_llm else None)
    if provider == "auto":
        auto = _auto_backend(base_url, openai_llm)
        if auto is None:
            return SttPlan(None, language=language, problem=NO_BACKEND_TEXT)
        provider = auto
    if provider == "local":
        return SttPlan("local", settings.stt_model or DEFAULT_LOCAL_MODEL, language=language)
    if provider not in ("chat", "whisper"):
        return SttPlan(None, language=language, problem=NOT_CONFIGURED_TEXT)
    if settings.stt_model:
        model = settings.stt_model
    elif provider == "whisper":
        model = DEFAULT_WHISPER_MODEL
    else:
        model = (settings.llm_model if openai_llm else None) or ""
    if not model:
        return SttPlan(None, base_url=base_url, language=language, problem=MODEL_TEXT)
    if not api_key and _needs_key(base_url):
        return SttPlan(None, model, base_url, language=language, problem=KEY_TEXT)
    return SttPlan(provider, model, base_url, api_key, language)


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
        require_key: bool = False,
    ) -> None:
        self.base_url = base_url
        self.model = model
        self.language = language
        self.timeout = timeout
        self.max_retries = max_retries
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
            raw = await self._call(client, audio, self.language or language)
        except STTError:
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
        content = getattr(choices[0].message, "content", None)
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
        return text if isinstance(text, str) else None


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
            log.debug("speech-to-text is not usable yet: %s", plan.problem)
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
