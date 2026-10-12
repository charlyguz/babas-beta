import asyncio
import logging
from typing import Any, Callable, Dict, Optional

from babas.agent import Agent
from babas.bridge.server import BridgeServer
from babas.channels import PanelChannel
from babas.drivers.base import DriverError, ErrorCode
from babas.drivers.extension_driver import ExtensionDriver
from babas.gate import SafeDriver
from babas.llm import LLMClient, LLMConfigError, LLMError
from babas.safety import SafetyPolicy, redact

log = logging.getLogger("babas.session")

BUSY_TEXT = "Ya estoy trabajando en otra tarea. Presione «Detener» si quiere empezar otra."
MISSING_KEY_TEXT = (
    "Falta configurar el modelo de IA. Pida a quien instaló BABAS que revise BABAS_LLM_API_KEY y BABAS_LLM_MODEL."
)
FAILED_TEXT = "No pude terminar la tarea. Puede intentarlo de nuevo."
DISCONNECTED_TEXT = "Se perdió la conexión con el navegador. Revise que la extensión esté activa e intente de nuevo."
CANCEL_RETRY_S = 0.25
CANCEL_ATTEMPTS = 40


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


async def _close_llm(llm: Any) -> None:
    close = getattr(llm, "close", None)
    if close is None:
        return
    try:
        await close()
    except Exception as exc:
        log.debug("closing the model client failed: %s", type(exc).__name__)


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
    ):
        self.bridge = bridge
        self.llm_factory = llm_factory
        self.policy = policy
        self.language = language
        self.max_steps = max_steps
        self.max_retries = max_retries
        self.busy = busy
        self._task: Optional["asyncio.Task[None]"] = None
        bridge.on("task.start", self._on_task_start)
        bridge.on("run.stop", self._on_run_stop)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def stop(self) -> None:
        task = self._task
        if task is not None and not task.done():
            await self._cancel(task)

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
                await _close_llm(llm)

    async def _not_configured(self, channel: PanelChannel, reason: str) -> None:
        log.warning("the language model is not configured: %s", redact(reason))
        await channel.log("error", MISSING_KEY_TEXT)
        await channel.run_state("failed", MISSING_KEY_TEXT)

    async def _fail(self, channel: PanelChannel, message: str, exc: Exception) -> None:
        log.error("task failed: %s: %s", type(exc).__name__, redact(str(exc)))
        await channel.log("error", message)
        await channel.run_state("failed", message)
