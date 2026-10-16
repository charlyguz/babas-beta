import asyncio
import contextlib
import inspect
import logging
import os
import socket
import sys
from typing import Annotated, Any, Callable, Dict, List, Optional, Set, Tuple

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

from babas import __version__
from babas.actions import ActionExecutor
from babas.agent import STOPPED_ANSWER, Agent, RunResult, summarize_step
from babas.bridge.server import BridgeServer
from babas.channels import PanelChannel, UserChannel
from babas.config import Settings
from babas.drivers.base import FLAG_PASSWORD, DriverError, ErrorCode, PageState, clip_text
from babas.drivers.extension_driver import ExtensionDriver
from babas.gate import ActionDenied, HandoffRequired, SafeDriver
from babas.llm import LLMClient, LLMConfigError, LLMError, make_llm
from babas.safety import SafetyPolicy, redact
from babas.schemas import ActStep, StepBase, changes_page
from babas.session import DISCONNECTED_TEXT, FAILED_TEXT, MISSING_KEY_TEXT

log = logging.getLogger("babas.mcp_server")

MAX_ELEMENTS = 150
MAX_TASK_CHARS = 4000
MAX_ERROR_CHARS = 300
MCP_PATH = "/mcp"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
CANCEL_RETRY_S = 0.25
CANCEL_ATTEMPTS = 40
REAP_TIMEOUT_S = 5.0
HTTP_SHUTDOWN_S = 5.0
INFO_TIMEOUT_S = 10.0

CONTROLLED_TEXT = (
    "BABAS está siendo controlado por otra aplicación (un cliente MCP). "
    "Pida las tareas desde esa aplicación; este panel le avisa y le pide permiso cuando hace falta."
)
STOPPED_TEXTS = {"es": STOPPED_ANSWER, "en": "The task was stopped."}

BUSY_ERROR = (
    "BABAS is already working on a task started from the side panel. "
    "Ask the user to press Stop there, or wait until it finishes."
)
DECLINED_ERROR = "The user declined this action in BABAS."
NEEDS_STATE_ERROR = (
    "Element indices only hold for the page state you read. "
    "Call get_page_state first (and again after the page changes), then use its element indices."
)
PORT_IN_USE_TEXT = (
    "Port {port} is in use (is `babas serve` running?). "
    "Stop it or use its HTTP endpoint: python -m babas serve --mcp-http"
)

_DECLINE_WHY = {
    "timeout": "no answer in time",
    "stopped": "the user pressed Stop",
    "no_ui": "BABAS could not reach the side panel",
}
_ERROR_HINTS = {
    ErrorCode.STALE_SNAPSHOT: "The page changed since get_page_state: call get_page_state again and use the new element indices.",
    ErrorCode.HALTED: "The user pressed Stop in BABAS: ask them to press Resume (Reanudar) in the side panel before trying again.",
    ErrorCode.UNSCRIPTABLE: "BABAS cannot read this page: use act with goto to open a website.",
}

INSTRUCTIONS = (
    "BABAS controls the user's real Chrome through the BABAS extension and can do any web task: web search, media, "
    "forms, email, school sites, government procedures and general browsing. "
    "Call get_page_state first and use the element indices it returns with act; indices only hold for the page state "
    "you read, so call get_page_state again after the page changes or after a STALE_SNAPSHOT error. "
    "Use run_task to hand a whole task to BABAS's own agent instead. "
    "Consequential steps (sending, submitting, paying, typing personal data) and any site outside the allowed list "
    "must be approved by the user in the BABAS side panel. You cannot skip that: if the user declines, the action is "
    "not done. Logins and CAPTCHAs are always done by the user: when a tool answers 'Hand-off needed', ask the user "
    "to do it in the browser, then call get_page_state again. The user can press Stop in the side panel at any time."
)
RUN_TASK_DESCRIPTION = (
    "Give BABAS a task in plain language, for example 'find a pozole recipe on YouTube and open the first video'. "
    "BABAS observes the page, plans and performs the steps itself in the user's browser, explains each step in the side "
    "panel and asks the user before anything consequential. It can take minutes. "
    "Returns {status: done|failed|stopped|max_steps, answer, steps}; status 'stopped' means the user pressed Stop or "
    "declined an action."
)
GET_PAGE_STATE_DESCRIPTION = (
    "Read the current page of the user's browser: url, title, the interactive elements with the index to use in act "
    "(at most 150, values with personal data masked), the visible text, captcha and password-field flags, and "
    "'prompt', a compact text version. Call it before act and again after the page changes."
)
ACT_DESCRIPTION = (
    "Run ONE action in the user's browser, for example {'action': {'action': 'click', 'index': 4}} or "
    "{'action': {'action': 'goto', 'value': 'https://www.wikipedia.org'}}. Element indices come from the latest "
    "get_page_state. Consequential actions wait for the user's approval in the BABAS side panel and fail with "
    "'declined' if the user says no; passwords and CAPTCHAs always need a hand-off to the user. "
    "Returns {ok, detail, url, title}."
)


def _safe_text(text: object, limit: int = MAX_ERROR_CHARS) -> str:
    return clip_text(redact(str(text)), limit)


def _driver_error_text(exc: DriverError) -> str:
    message = _safe_text(exc.message)
    text = f"{exc.code}: {message}" if message else exc.code
    hint = _ERROR_HINTS.get(exc.code)
    return f"{text} {hint}" if hint else text


def _declined_text(exc: ActionDenied) -> str:
    if exc.reason == "site":
        why = "the site was not allowed"
    else:
        why = _DECLINE_WHY.get(exc.detail) or _DECLINE_WHY.get(exc.reason, "")
    return f"{DECLINED_ERROR[:-1]} ({why})." if why else DECLINED_ERROR


def _tool_error(exc: BaseException) -> ToolError:
    # Messages go to the MCP client, so they carry codes and redacted text only, never traces or typed values.
    if isinstance(exc, ToolError):
        return exc
    if isinstance(exc, ActionDenied):
        return ToolError(_declined_text(exc))
    if isinstance(exc, HandoffRequired):
        return ToolError(f"Hand-off needed: {exc.message}")
    if isinstance(exc, DriverError):
        return ToolError(_driver_error_text(exc))
    if isinstance(exc, ValueError):
        return ToolError(f"Invalid action: {_safe_text(exc)}")
    if isinstance(exc, LLMError):
        return ToolError(f"The language model failed: {_safe_text(exc)}")
    log.warning("tool failed with %s", type(exc).__name__)
    return ToolError(f"BABAS failed unexpectedly ({type(exc).__name__}). See the BABAS log on the server for details.")


class _LlmNotConfigured(BaseException):
    # Not an Exception on purpose: Agent turns every Exception from the LLM into a retry, and a missing key never heals.
    pass


class _ConfigGuard:
    def __init__(self, inner: LLMClient):
        self.inner = inner

    async def complete_json(self, system: str, user: str, schema: Optional[dict] = None) -> dict:
        try:
            return await self.inner.complete_json(system, user, schema)
        except LLMConfigError as exc:
            raise _LlmNotConfigured(str(exc)) from None


class _Progress:
    def __init__(self, ctx: Optional[Context], total: int):
        self.ctx = ctx
        self.total = total
        self.steps = 0

    async def on_event(self, name: str, data: Dict[str, Any]) -> None:
        if name != "step":
            return
        count = data.get("count")
        if isinstance(count, int) and count > self.steps:
            self.steps = count
        if self.ctx is None:
            return
        outcome = "ok" if data.get("ok") else "failed"
        message = f"{data.get('summary', '')} ({outcome})"
        try:
            await self.ctx.report_progress(self.steps, self.total, message)
        except Exception as exc:
            log.debug("progress not delivered: %s", type(exc).__name__)


def _state_payload(state: PageState) -> Dict[str, Any]:
    elements = list(state.elements)
    kept = elements
    if len(elements) > MAX_ELEMENTS:
        ordered = [el for el in elements if el.in_viewport] + [el for el in elements if not el.in_viewport]
        kept = sorted(ordered[:MAX_ELEMENTS], key=lambda el: el.index)
    shown: List[Dict[str, Any]] = []
    for element in kept:
        item = element.model_dump()
        if element.has(FLAG_PASSWORD) or (element.type or "").lower() == "password":
            item["value"] = None
        elif element.value:
            item["value"] = redact(element.value)
        shown.append(item)
    data = state.model_dump()
    data["elements"] = shown
    data["elements_total"] = len(elements)
    data["elements_omitted"] = len(elements) - len(shown)
    data["prompt"] = state.to_prompt(MAX_ELEMENTS)
    return data


async def _cancel(task: "asyncio.Task[Any]") -> None:
    # On Python < 3.12 asyncio.wait_for can swallow a cancellation that arrives together with its result, so insist.
    for _ in range(CANCEL_ATTEMPTS):
        task.cancel()
        done, _pending = await asyncio.wait({task}, timeout=CANCEL_RETRY_S)
        if done:
            return
    log.warning("the run did not stop after %d attempts", CANCEL_ATTEMPTS)


async def _reap(task: "asyncio.Task[Any]") -> None:
    task.cancel()
    try:
        await asyncio.wait({task}, timeout=REAP_TIMEOUT_S)
    except asyncio.CancelledError:
        task.cancel()


async def _where(driver: SafeDriver) -> Tuple[str, str]:
    # BrowserDriver has no title accessor, so the extension's tab.info (allowlist-exempt, metadata only) is used when present.
    request = getattr(getattr(getattr(driver, "inner", driver), "bridge", None), "request", None)
    try:
        if request is not None:
            info = await request("tab.info", timeout=INFO_TIMEOUT_S)
            url, title = info.get("url"), info.get("title")
            if isinstance(url, str):
                return redact(url), redact(title) if isinstance(title, str) else ""
        return redact(await driver.current_url()), ""
    except Exception as exc:
        log.debug("page info unavailable: %s", type(exc).__name__)
        return "", ""


class McpRuntime:
    def __init__(
        self,
        settings: Settings,
        driver_factory: Callable[[], SafeDriver],
        llm: LLMClient,
        channel: UserChannel,
        *,
        busy: Optional[Callable[[], bool]] = None,
    ):
        self.settings = settings
        self.driver_factory = driver_factory
        self.llm = llm
        self.channel = channel
        self.busy: Callable[[], bool] = busy or (lambda: False)
        self.lock = asyncio.Lock()
        self.last_state: Optional[PageState] = None
        self._runs: Set["asyncio.Task[RunResult]"] = set()

    @property
    def active(self) -> bool:
        return self.lock.locked()

    def attach(self, bridge: BridgeServer, *, panel_tasks: bool) -> None:
        bridge.on("run.stop", self._on_run_stop)
        if panel_tasks:
            bridge.on("task.start", self._on_task_start)

    async def _on_run_stop(self, payload: Dict[str, Any]) -> None:
        running = [task for task in self._runs if not task.done()]
        if not running:
            return
        log.info("stop requested")
        await asyncio.gather(*(_cancel(task) for task in running))

    async def _on_task_start(self, payload: Dict[str, Any]) -> None:
        await self.channel.log("info", CONTROLLED_TEXT)
        await self.channel.run_state("failed", CONTROLLED_TEXT)

    def _require_idle(self) -> None:
        if self.busy():
            raise ToolError(BUSY_ERROR)

    async def _new_driver(self) -> SafeDriver:
        driver = self.driver_factory()
        await driver.start()
        return driver

    async def _panel(self, kind: str, text: str, ok: Optional[bool] = None) -> None:
        try:
            await self.channel.log(kind, text, ok)
        except Exception as exc:
            log.debug("panel log not delivered: %s", type(exc).__name__)

    async def _panel_state(self, state: str, summary: str = "") -> None:
        try:
            await self.channel.run_state(state, summary)
        except Exception as exc:
            log.debug("panel run state not delivered: %s", type(exc).__name__)

    async def get_page_state(self) -> Dict[str, Any]:
        self._require_idle()
        async with self.lock:
            self._require_idle()
            try:
                driver = await self._new_driver()
                state = await driver.get_page_state()
            except Exception as exc:
                raise _tool_error(exc) from None
            self.last_state = state
        log.info("page state read (%d elements)", len(state.elements))
        return _state_payload(state)

    async def act(self, action: StepBase) -> Dict[str, Any]:
        self._require_idle()
        async with self.lock:
            self._require_idle()
            if getattr(action, "index", None) is not None and self.last_state is None:
                raise ToolError(NEEDS_STATE_ERROR)
            summary = summarize_step(action)
            log.info("act %s", getattr(action, "action", "?"))
            try:
                driver = await self._new_driver()
                shots = self.settings.home / "screenshots"
                if getattr(action, "action", "") == "screenshot":
                    await asyncio.to_thread(shots.mkdir, parents=True, exist_ok=True)
                outcome = await ActionExecutor(driver, screenshot_dir=str(shots)).execute(action, self.last_state)
            except Exception as exc:
                if isinstance(exc, DriverError) and exc.code == ErrorCode.STALE_SNAPSHOT:
                    self.last_state = None
                if isinstance(exc, HandoffRequired):
                    await self._panel("info", exc.message)
                elif not isinstance(exc, ActionDenied):
                    await self._panel("step", summary, False)
                raise _tool_error(exc) from None
            if changes_page(action):
                self.last_state = None
            ok = outcome.ok is not False
            await self._panel("step", summary, ok)
            url, title = await _where(driver)
            return {"ok": ok, "detail": redact(outcome.detail or ""), "url": url, "title": title}

    async def run_task(self, task: str, ctx: Optional[Context] = None) -> Dict[str, Any]:
        text = task.strip() if isinstance(task, str) else ""
        if not text:
            raise ToolError("The task must not be empty.")
        if len(text) > MAX_TASK_CHARS:
            raise ToolError(f"The task is too long (at most {MAX_TASK_CHARS} characters).")
        self._require_idle()
        async with self.lock:
            self._require_idle()
            log.info("task started (%d characters)", len(text))
            self.last_state = None
            progress = _Progress(ctx, self.settings.max_steps)
            inner = asyncio.get_running_loop().create_task(self._execute(text, progress))
            self._runs.add(inner)
            inner.add_done_callback(self._runs.discard)
            try:
                await asyncio.wait({inner})
            except asyncio.CancelledError:
                await _reap(inner)
                raise
            return await self._outcome(inner, progress)

    async def _execute(self, text: str, progress: _Progress) -> RunResult:
        try:
            driver = await self._new_driver()
            await self._panel_state("running")
            agent = Agent(
                driver,
                _ConfigGuard(self.llm),
                self.channel,
                language=self.settings.language,
                max_steps=self.settings.max_steps,
                max_retries=self.settings.max_retries,
                on_event=progress.on_event,
            )
            result = await agent.run(text)
        except asyncio.CancelledError:
            await asyncio.shield(self._panel_state("stopped"))
            raise
        log.info("task finished: %s after %d steps", result.status, result.steps)
        await self._panel_state(result.status, result.answer)
        return result

    async def _outcome(self, inner: "asyncio.Task[RunResult]", progress: _Progress) -> Dict[str, Any]:
        if inner.cancelled():
            answer = STOPPED_TEXTS.get(self.settings.language, STOPPED_TEXTS["en"])
            return {"status": "stopped", "answer": answer, "steps": progress.steps}
        error = inner.exception()
        if error is None:
            result = inner.result()
            return {"status": result.status, "answer": redact(result.answer), "steps": result.steps}
        if isinstance(error, _LlmNotConfigured):
            await self._fail(MISSING_KEY_TEXT)
            raise ToolError(str(error))
        if isinstance(error, DriverError):
            halted = error.code == ErrorCode.HALTED
            disconnected = error.code == ErrorCode.NO_EXTENSION
            if not halted:
                await self._fail(DISCONNECTED_TEXT if disconnected else FAILED_TEXT)
            raise _tool_error(error)
        if isinstance(error, Exception):
            log.error("task failed: %s: %s", type(error).__name__, _safe_text(error))
            await self._fail(FAILED_TEXT)
            raise _tool_error(error)
        raise error

    async def _fail(self, message: str) -> None:
        await self._panel("error", message)
        await self._panel_state("failed", message)


def build_mcp_server(runtime: McpRuntime) -> MCPServer:
    # Builtin dict[str, Any] (not typing.Dict) so the SDK returns the dict itself as structured content, not {"result": ...}.
    root = logging.getLogger()
    handlers_before = list(root.handlers)
    server = MCPServer(name="babas", title="BABAS", instructions=INSTRUCTIONS, version=__version__)
    # The SDK installs a root handler that would print every babas log line a second time on stderr.
    for handler in list(root.handlers):
        if handler not in handlers_before:
            root.removeHandler(handler)

    @server.tool(name="run_task", description=RUN_TASK_DESCRIPTION)
    async def run_task(
        task: Annotated[str, Field(description="What the user wants done, in plain language.", min_length=1)],
        ctx: Context,
    ) -> dict[str, Any]:
        return await runtime.run_task(task, ctx)

    @server.tool(name="get_page_state", description=GET_PAGE_STATE_DESCRIPTION)
    async def get_page_state() -> dict[str, Any]:
        return await runtime.get_page_state()

    @server.tool(name="act", description=ACT_DESCRIPTION)
    async def act(
        action: Annotated[ActStep, Field(description="The action to run; its 'action' field names the action type.")],
    ) -> dict[str, Any]:
        return await runtime.act(action)

    return server


def build_runtime(
    settings: Settings,
    bridge: BridgeServer,
    llm: Optional[LLMClient] = None,
    *,
    busy: Optional[Callable[[], bool]] = None,
) -> McpRuntime:
    policy = SafetyPolicy(settings.allowed_domains)
    channel = PanelChannel(bridge)

    def driver_factory() -> SafeDriver:
        return SafeDriver(ExtensionDriver(bridge), policy, channel)

    return McpRuntime(settings, driver_factory, llm if llm is not None else make_llm(settings), channel, busy=busy)


async def _close_llm(llm: LLMClient) -> None:
    close = getattr(llm, "close", None)
    if close is None:
        return
    try:
        result = close()
        if inspect.isawaitable(result):
            await result
    except Exception as exc:
        log.debug("closing the language model client failed: %s", type(exc).__name__)


async def run_stdio(settings: Settings) -> None:
    # stdout is the protocol channel here: nothing on this path may print.
    bridge = BridgeServer(
        settings.bridge_host,
        settings.bridge_port,
        list(settings.extension_origins),
        pairing_file=settings.pairing_file,
    )
    try:
        await bridge.start()
    except OSError:
        sys.stderr.write(PORT_IN_USE_TEXT.format(port=settings.bridge_port) + "\n")
        sys.stderr.flush()
        raise SystemExit(1) from None
    llm: Optional[LLMClient] = None
    try:
        llm = make_llm(settings)
        runtime = build_runtime(settings, bridge, llm)
        runtime.attach(bridge, panel_tasks=True)
        log.info("MCP server on stdio, bridge on %s", bridge.url)
        await build_mcp_server(runtime).run_stdio_async()
    finally:
        await bridge.stop()
        if llm is not None:
            await _close_llm(llm)


def _transport_security(port: int) -> TransportSecuritySettings:
    hosts = [f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"]
    if port == 80:
        hosts += ["127.0.0.1", "localhost", "[::1]"]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=[f"http://{host}" for host in hosts],
    )


def _ensure_port_free(host: str, port: int) -> None:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            if os.name != "nt":
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((host, port))
    except OSError as exc:
        raise OSError(exc.errno, f"MCP HTTP port {port} is not available ({exc.strerror or 'in use'})") from None


async def serve_http(runtime: McpRuntime, host: str, port: int) -> None:
    # This endpoint can drive the user's real browser and has no login, so it only ever listens on loopback and
    # rejects any Host or Origin that is not loopback (DNS rebinding and cross-site requests from web pages).
    if host not in LOOPBACK_HOSTS:
        raise ValueError(f"The MCP HTTP endpoint only listens on loopback addresses, not {host!r}.")
    import uvicorn

    class _Server(uvicorn.Server):
        # The caller owns SIGINT/SIGTERM; uvicorn must not take them over or re-raise them after it stops.
        @contextlib.contextmanager
        def capture_signals(self):  # type: ignore[no-untyped-def]
            yield

        def install_signal_handlers(self) -> None:
            return None

    _ensure_port_free(host, port)
    app = build_mcp_server(runtime).streamable_http_app(
        streamable_http_path=MCP_PATH, transport_security=_transport_security(port), host=host
    )
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
        log_config=None,
        access_log=False,
        timeout_graceful_shutdown=2,
    )
    http = _Server(config)
    serving = asyncio.get_running_loop().create_task(http.serve())
    log.info("MCP server on http://%s:%d%s", host if ":" not in host else f"[{host}]", port, MCP_PATH)
    try:
        await asyncio.wait({serving})
    except asyncio.CancelledError:
        http.should_exit = True
        try:
            await asyncio.wait({serving}, timeout=HTTP_SHUTDOWN_S)
        except asyncio.CancelledError:
            pass
        if not serving.done():
            http.force_exit = True
            serving.cancel()
        raise
    if not serving.cancelled():
        serving.result()
