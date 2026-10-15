import asyncio
import base64
import binascii
import logging
from typing import Any, Callable, Dict, Optional, Tuple

from babas.agent import Agent
from babas.bridge.server import BridgeServer
from babas.channels import PanelChannel
from babas.drivers.base import DriverError, ErrorCode
from babas.drivers.extension_driver import ExtensionDriver
from babas.gate import SafeDriver
from babas.llm import LLMClient, LLMConfigError, LLMError
from babas.safety import SafetyPolicy, redact
from babas.stt import (
    BAD_AUDIO_TEXT,
    EMPTY_TEXT,
    ERROR_CODES,
    MAX_TRANSCRIPT_CHARS,
    NOT_CONFIGURED_TEXT,
    PROVIDER_TEXT,
    STT_BUSY_TEXT,
    SpeechToText,
    STTConfigError,
    STTError,
)

log = logging.getLogger("babas.session")

BUSY_TEXT = "Ya estoy trabajando en otra tarea. Presione «Detener» si quiere empezar otra."
MISSING_KEY_TEXT = (
    "Falta configurar el modelo de IA. Pida a quien instaló BABAS que revise BABAS_LLM_API_KEY y BABAS_LLM_MODEL."
)
FAILED_TEXT = "No pude terminar la tarea. Puede intentarlo de nuevo."
DISCONNECTED_TEXT = "Se perdió la conexión con el navegador. Revise que la extensión esté activa e intente de nuevo."
CANCEL_RETRY_S = 0.25
CANCEL_ATTEMPTS = 40
VOICE_MIME = "audio/wav"
MAX_REQUEST_ID_CHARS = 128
MAX_AUDIO_BYTES = 3 * 1024 * 1024
MAX_AUDIO_BASE64_CHARS = 4 * ((MAX_AUDIO_BYTES + 2) // 3)
MAX_DURATION_MS = 30000
MAX_MESSAGE_CHARS = 500
TRANSCRIPT_REPLY_TIMEOUT_S = 10.0


class _ModelNotConfigured(BaseException):
    # Not an Exception on purpose: Agent turns every Exception from the LLM into a retry, and a missing key never heals.
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _ConfigGuard:
    def __init__(self, inner: LLMClient):
        self.inner = inner

    async def complete_json(self, system: str, user: str, schema: Optional[dict] = None) -> dict:
        try:
            return await self.inner.complete_json(system, user, schema)
        except LLMConfigError as exc:
            raise _ModelNotConfigured(str(exc)) from None


async def _close_client(client: Any, what: str) -> None:
    close = getattr(client, "close", None)
    if close is None:
        return
    try:
        await close()
    except Exception as exc:
        log.debug("closing %s failed: %s", what, type(exc).__name__)


def _request_id(payload: Any) -> Optional[str]:
    value = payload.get("request_id") if isinstance(payload, dict) else None
    if isinstance(value, str) and 1 <= len(value) <= MAX_REQUEST_ID_CHARS:
        return value
    return None


def _decode_audio(payload: Dict[str, Any]) -> Optional[bytes]:
    duration = payload.get("duration_ms")
    encoded = payload.get("audio_base64")
    if payload.get("mime") != VOICE_MIME:
        return None
    if isinstance(duration, bool) or not isinstance(duration, int) or not 1 <= duration <= MAX_DURATION_MS:
        return None
    if not isinstance(encoded, str) or not 0 < len(encoded) <= MAX_AUDIO_BASE64_CHARS:
        return None
    try:
        audio = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None
    return audio if 0 < len(audio) <= MAX_AUDIO_BYTES else None


class Session:
    def __init__(
        self,
        bridge: BridgeServer,
        llm_factory: Callable[[], LLMClient],
        policy: SafetyPolicy,
        *,
        language: str = "en",
        max_steps: int = 30,
        max_retries: int = 2,
        busy: Optional[Callable[[], bool]] = None,
        stt_factory: Optional[Callable[[], SpeechToText]] = None,
    ):
        self.bridge = bridge
        self.llm_factory = llm_factory
        self.policy = policy
        self.language = language
        self.max_steps = max_steps
        self.max_retries = max_retries
        self.busy = busy
        self.stt_factory = stt_factory
        self._task: Optional["asyncio.Task[None]"] = None
        self._stt: Optional[SpeechToText] = None
        self._transcribing: Optional["asyncio.Task[None]"] = None
        bridge.on("task.start", self._on_task_start)
        bridge.on("run.stop", self._on_run_stop)
        bridge.on("stt.transcribe", self._on_stt_transcribe)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def stop(self) -> None:
        task = self._task
        if task is not None and not task.done():
            await self._cancel(task)
        dictation = self._transcribing
        if dictation is not None and not dictation.done():
            dictation.cancel()
        stt, self._stt = self._stt, None
        if stt is not None:
            await _close_client(stt, "the speech client")

    async def _cancel(self, task: "asyncio.Task[None]") -> None:
        # On Python < 3.12 asyncio.wait_for can swallow a cancellation that arrives together with its result, so insist.
        for _ in range(CANCEL_ATTEMPTS):
            task.cancel()
            done, _pending = await asyncio.wait({task}, timeout=CANCEL_RETRY_S)
            if done:
                return
        log.warning("the run did not stop after %d attempts", CANCEL_ATTEMPTS)

    def _other_run_active(self) -> bool:
        if self.busy is None:
            return False
        try:
            return bool(self.busy())
        except Exception as exc:
            log.debug("busy check failed: %s", type(exc).__name__)
            return False

    async def _on_task_start(self, payload: Dict[str, Any]) -> None:
        text = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(text, str) or not text.strip():
            log.debug("ignored task.start without text")
            return
        if self.running or self._other_run_active():
            await PanelChannel(self.bridge).log("info", BUSY_TEXT)
            return
        task = asyncio.get_running_loop().create_task(self._run(text.strip()))
        self._task = task
        task.add_done_callback(self._finished)

    async def _on_run_stop(self, payload: Dict[str, Any]) -> None:
        task = self._task
        if task is not None and not task.done():
            log.info("stop requested")
            await self._cancel(task)

    def _finished(self, task: "asyncio.Task[None]") -> None:
        if self._task is task:
            self._task = None
        if not task.cancelled() and task.exception() is not None:
            log.error("run task ended with %s", type(task.exception()).__name__)

    async def _run(self, text: str) -> None:
        log.info("task started (%d characters)", len(text))
        channel = PanelChannel(self.bridge)
        llm: Any = None
        try:
            driver = SafeDriver(ExtensionDriver(self.bridge), self.policy, channel)
            await driver.start()
            await channel.run_state("running")
            llm = self.llm_factory()
            agent = Agent(
                driver,
                _ConfigGuard(llm),
                channel,
                language=self.language,
                max_steps=self.max_steps,
                max_retries=self.max_retries,
            )
            result = await agent.run(text)
            log.info("task finished: %s after %d steps", result.status, result.steps)
            await channel.run_state(result.status, result.answer)
        except asyncio.CancelledError:
            log.info("task cancelled")
            await asyncio.shield(channel.run_state("stopped"))
            raise
        except _ModelNotConfigured as exc:
            await self._not_configured(channel, exc.reason)
        except LLMConfigError as exc:
            await self._not_configured(channel, str(exc))
        except LLMError as exc:
            await self._fail(channel, FAILED_TEXT, exc)
        except DriverError as exc:
            if exc.code == ErrorCode.HALTED:
                await channel.run_state("stopped")
                return
            await self._fail(channel, DISCONNECTED_TEXT if exc.code == ErrorCode.NO_EXTENSION else FAILED_TEXT, exc)
        except Exception as exc:
            await self._fail(channel, FAILED_TEXT, exc)
        finally:
            if llm is not None:
                await _close_client(llm, "the model client")

    async def _not_configured(self, channel: PanelChannel, reason: str) -> None:
        log.warning("the language model is not configured: %s", redact(reason))
        await channel.log("error", MISSING_KEY_TEXT)
        await channel.run_state("failed", MISSING_KEY_TEXT)

    async def _fail(self, channel: PanelChannel, message: str, exc: Exception) -> None:
        log.error("task failed: %s: %s", type(exc).__name__, redact(str(exc)))
        await channel.log("error", message)
        await channel.run_state("failed", message)

    def _transcriber(self) -> SpeechToText:
        if self._stt is None:
            if self.stt_factory is None:
                raise STTConfigError(NOT_CONFIGURED_TEXT)
            try:
                self._stt = self.stt_factory()
            except STTError:
                raise
            except Exception as exc:
                log.error("could not create the speech client: %s", type(exc).__name__)
                raise STTError("provider", PROVIDER_TEXT) from None
        return self._stt

    async def _on_stt_transcribe(self, payload: Dict[str, Any]) -> None:
        try:
            await self._dictate(payload)
        except Exception as exc:
            log.error("voice request failed: %s", type(exc).__name__)

    async def _dictate(self, payload: Dict[str, Any]) -> None:
        request_id = _request_id(payload)
        if request_id is None:
            log.debug("ignored stt.transcribe without a valid request_id")
            return
        if self._transcribing is not None:
            await self._reply_transcript(request_id, error=("busy", STT_BUSY_TEXT))
            return
        audio = _decode_audio(payload)
        if audio is None:
            log.debug("rejected an invalid stt.transcribe payload")
            await self._reply_transcript(request_id, error=("bad_audio", BAD_AUDIO_TEXT))
            return
        log.info("voice transcription started (%d bytes, %s ms)", len(audio), payload.get("duration_ms"))
        self._transcribing = asyncio.current_task()
        try:
            text = await self._transcribe(audio)
        except STTError as exc:
            code = exc.code if exc.code in ERROR_CODES else "provider"
            log.info("voice transcription failed (%s)", code)
            error = (code, redact(str(exc.message))[:MAX_MESSAGE_CHARS])
        else:
            log.info("voice transcription finished (%d characters)", len(text))
            error = None
        finally:
            self._transcribing = None
        if error is not None:
            await self._reply_transcript(request_id, error=error)
        else:
            await self._reply_transcript(request_id, text=text)

    async def _transcribe(self, audio: bytes) -> str:
        try:
            text = await self._transcriber().transcribe(audio, VOICE_MIME, self.language)
        except STTError:
            raise
        except Exception as exc:
            log.warning("speech-to-text failed: %s", type(exc).__name__)
            raise STTError("provider", PROVIDER_TEXT) from None
        if not isinstance(text, str):
            raise STTError("provider", PROVIDER_TEXT)
        text = text.strip()[:MAX_TRANSCRIPT_CHARS].rstrip()
        if not text:
            raise STTError("empty", EMPTY_TEXT)
        return text

    async def _reply_transcript(
        self, request_id: str, *, text: Optional[str] = None, error: Optional[Tuple[str, str]] = None
    ) -> None:
        payload: Dict[str, Any] = {"request_id": request_id}
        if error is not None:
            payload["error"] = {"code": error[0], "message": error[1]}
        else:
            payload["text"] = text
        try:
            await self.bridge.request("ui.transcript", payload, timeout=TRANSCRIPT_REPLY_TIMEOUT_S)
        except Exception as exc:
            reason = exc.code if isinstance(exc, DriverError) else type(exc).__name__
            log.debug("could not deliver the transcript: %s", reason)
