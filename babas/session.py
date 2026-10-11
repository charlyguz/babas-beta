import asyncio
import logging
from typing import Any, Callable, Dict, Optional

from babas.agent import Agent
from babas.bridge.server import BridgeServer
from babas.channels import PanelChannel
from babas.drivers.base import DriverError, ErrorCode
from babas.drivers.extension_driver import ExtensionDriver
from babas.gate import SafeDriver
from babas.llm import LLMClient, LLMError
from babas.safety import SafetyPolicy, redact

log = logging.getLogger("babas.session")

BUSY_TEXT = "Ya estoy trabajando en otra tarea. Presione «Detener» si quiere empezar otra."
MISSING_KEY_TEXT = "Falta configurar el modelo de IA (OPENAI_API_KEY)."
FAILED_TEXT = "No pude terminar la tarea. Puede intentarlo de nuevo."
DISCONNECTED_TEXT = "Se perdió la conexión con el navegador. Revise que la extensión esté activa e intente de nuevo."
KEY_NAME = "OPENAI_API_KEY"
CANCEL_RETRY_S = 0.25
CANCEL_ATTEMPTS = 40


class _ModelNotConfigured(BaseException):
    # Not an Exception on purpose: Agent turns every Exception from the LLM into a retry, and a missing key never heals.
    pass


class _KeyGuard:
    def __init__(self, inner: LLMClient):
        self.inner = inner

    async def complete_json(self, system: str, user: str, schema: Optional[dict] = None) -> dict:
        try:
            return await self.inner.complete_json(system, user, schema)
        except LLMError as exc:
            if KEY_NAME in str(exc):
                raise _ModelNotConfigured() from None
            raise


class Session:
    def __init__(
        self,
        bridge: BridgeServer,
        llm_factory: Callable[[], LLMClient],
        policy: SafetyPolicy,
        *,
        language: str = "en",
        max_steps: int = 30,
    ):
        self.bridge = bridge
        self.llm_factory = llm_factory
        self.policy = policy
        self.language = language
        self.max_steps = max_steps
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

    async def _on_task_start(self, payload: Dict[str, Any]) -> None:
        text = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(text, str) or not text.strip():
            log.debug("ignored task.start without text")
            return
        if self.running:
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
        try:
            driver = SafeDriver(ExtensionDriver(self.bridge), self.policy, channel)
            await driver.start()
            await channel.run_state("running")
            agent = Agent(
                driver,
                _KeyGuard(self.llm_factory()),
                channel,
                language=self.language,
                max_steps=self.max_steps,
            )
            result = await agent.run(text)
            log.info("task finished: %s after %d steps", result.status, result.steps)
            await channel.run_state(result.status, result.answer)
        except asyncio.CancelledError:
            log.info("task cancelled")
            await asyncio.shield(channel.run_state("stopped"))
            raise
        except _ModelNotConfigured:
            await self._fail(channel, MISSING_KEY_TEXT)
        except LLMError as exc:
            await self._fail(channel, MISSING_KEY_TEXT if KEY_NAME in str(exc) else FAILED_TEXT, exc)
        except DriverError as exc:
            if exc.code == ErrorCode.HALTED:
                await channel.run_state("stopped")
                return
            await self._fail(channel, DISCONNECTED_TEXT if exc.code == ErrorCode.NO_EXTENSION else FAILED_TEXT, exc)
        except Exception as exc:
            await self._fail(channel, FAILED_TEXT, exc)

    async def _fail(self, channel: PanelChannel, message: str, exc: Optional[Exception] = None) -> None:
        if exc is None:
            log.error("task failed: the language model is not configured")
        else:
            log.error("task failed: %s: %s", type(exc).__name__, redact(str(exc)))
        await channel.log("error", message)
        await channel.run_state("failed", message)
