import asyncio
import importlib
import json
import logging
import os
import sys
import threading
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any, List, Optional

from babas import __version__, ui
from babas.bridge.server import BridgeServer
from babas.config import Settings
from babas.drivers.base import BrowserDriver, DriverError, Element, Target
from babas.drivers.extension_driver import ExtensionDriver
from babas.logs import setup_logging
from babas.safety import SafetyPolicy, redact
from babas.stt import make_stt

log = logging.getLogger("babas.devrepl")

PROMPT_LABEL = "babas"
WATCH_INTERVAL = 0.2
DEFAULT_SCROLL = 500
MCP_START_GRACE_S = 0.2
MCP_EXTRA_HINT = "Install the MCP extra: pip install 'babas[mcp]'"

HELP_COMMANDS = [
    ("ls", "list the elements of the page (highlights them)"),
    ("click N", "click element N of the last ls"),
    ("type N text...", "type text into element N"),
    ("select N value...", "choose an option of select N"),
    ("press KEY [N]", "press a key (Enter, Tab, Escape, ...), optionally on element N"),
    ("scroll up|down [px]", "scroll the page (default 500)"),
    ("read [N]", "read the page text, or the text of element N"),
    ("goto URL", "open an http(s) URL in the current tab"),
    ("hl all|off", "show or hide the highlights"),
    ("tabs", "list the tabs BABAS knows"),
    ("new [URL]", "open a new tab"),
    ("switch NAME", "make a tab current"),
    ("close [NAME]", "close a tab (default: the current one)"),
    ("shot [file.png]", "save a screenshot of the visible area"),
    ("help, ?", "this list"),
    ("quit, exit, q", "stop"),
]


def _term() -> ui.Term:
    return ui.detect(sys.stdout)


def _say(status: str, text: str) -> None:
    ui.emit(ui.step(_term(), status, text, wrap_text=False))


def render_help(term: ui.Term) -> str:
    return ui.table(term, ["Command", "What it does"], HELP_COMMANDS, styles=[("bold", "bright_cyan"), ()])


async def _ainput(prompt: str) -> str:
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()

    def deliver(value: Optional[str], error: Optional[Exception]) -> None:
        if future.done():
            return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(value or "")

    def worker() -> None:
        try:
            line = input(ui.for_input(prompt))
        except Exception as exc:
            outcome = (None, exc)
        else:
            outcome = (line, None)
        try:
            loop.call_soon_threadsafe(deliver, *outcome)
        except RuntimeError:
            pass

    threading.Thread(target=worker, name="babas-input", daemon=True).start()
    return await future


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _escape_newlines(text: str) -> str:
    return text.replace("\r", "").replace("\n", "\\n")


def _format_element(el: Element, term: Optional[ui.Term] = None) -> str:
    term = term if term is not None else ui.Term()
    line = f"{term.paint(f'[{el.index}]', 'bright_cyan')} {term.paint(_one_line(ui.clean(el.role)), 'dim')}"
    line += f' "{redact(_one_line(ui.clean(el.label)))}"'
    if el.flags:
        line += " " + term.paint("{" + ",".join(ui.clean(flag) for flag in el.flags) + "}", "yellow")
    if el.value:
        line += f" = {term.paint(redact(_escape_newlines(ui.clean(el.value))), 'green')}"
    return line


def _parse_index(token: str) -> Optional[int]:
    try:
        value = int(token)
    except ValueError:
        return None
    return value if value >= 0 else None


async def _ensure_started(driver: BrowserDriver) -> None:
    if "main" not in driver.list_tabs():
        await driver.start()


class _Session:
    def __init__(self, driver: BrowserDriver):
        self.driver = driver
        self.snapshot_id: Optional[str] = None

    def target(self, index: int) -> Target:
        return Target(index=index, snapshot_id=self.snapshot_id)

    async def run(self, line: str) -> bool:
        stripped = line.strip()
        if not stripped:
            return True
        cmd = stripped.split(None, 1)[0].lower()
        if cmd in ("quit", "exit", "q"):
            return False
        if cmd in ("help", "?"):
            ui.emit(render_help(_term()))
            return True
        try:
            await _ensure_started(self.driver)
            await self._dispatch(cmd, line.lstrip())
        except DriverError as exc:
            _say("failure", f"ERROR {exc.code}: {redact(ui.clean(exc.message))}")
            if exc.data:
                _say("info", f"data: {redact(ui.clean(json.dumps(exc.data, ensure_ascii=False, default=str)))}")
        except Exception as exc:
            log.exception("repl command failed: %s", cmd)
            _say("failure", f"ERROR INTERNAL: {type(exc).__name__}: {redact(ui.clean(str(exc)))}")
        return True

    def _need_snapshot(self) -> bool:
        if self.snapshot_id is None:
            _say("failure", "ERROR: run ls first")
            return False
        return True

    @staticmethod
    def _usage(text: str) -> None:
        _say("failure", f"ERROR: usage: {text}")

    async def _dispatch(self, cmd: str, line: str) -> None:
        driver = self.driver
        parts = line.split()
        args = parts[1:]

        if cmd == "ls":
            state = await driver.get_page_state(highlight=True)
            self.snapshot_id = state.snapshot_id
            term = _term()
            ui.emit(f"{term.paint('URL:', 'dim')} {redact(_one_line(ui.clean(state.url)))}")
            ui.emit(f"{term.paint('TITLE:', 'dim')} {redact(_one_line(ui.clean(state.title)))}")
            for el in state.elements:
                ui.emit(_format_element(el, term))
        elif cmd == "click":
            index = _parse_index(args[0]) if len(args) == 1 else None
            if index is None:
                return self._usage("click N")
            if self._need_snapshot():
                await driver.click(self.target(index))
                _say("success", "OK")
        elif cmd in ("type", "select"):
            pieces = line.split(None, 2)
            index = _parse_index(pieces[1]) if len(pieces) == 3 else None
            if index is None:
                return self._usage(f"{cmd} N {'text' if cmd == 'type' else 'value'}...")
            if self._need_snapshot():
                if cmd == "type":
                    await driver.type_text(self.target(index), pieces[2])
                    _say("success", "OK")
                else:
                    _say("success", f"Selected: {redact(ui.clean(str(await driver.select_option(self.target(index), pieces[2]))))}")
        elif cmd == "press":
            if len(args) not in (1, 2):
                return self._usage("press KEY [N]")
            index = _parse_index(args[1]) if len(args) == 2 else None
            if len(args) == 2 and index is None:
                return self._usage("press KEY [N]")
            if index is not None and not self._need_snapshot():
                return
            await driver.press_key(args[0], self.target(index) if index is not None else None)
            _say("success", "OK")
        elif cmd == "scroll":
            amount = _parse_index(args[1]) if len(args) == 2 else DEFAULT_SCROLL
            if len(args) not in (1, 2) or args[0].lower() not in ("up", "down") or amount is None:
                return self._usage("scroll up|down [px]")
            await driver.scroll(args[0].lower(), amount)  # type: ignore[arg-type]
            _say("success", "OK")
        elif cmd == "read":
            index = _parse_index(args[0]) if len(args) == 1 else None
            if len(args) > 1 or (len(args) == 1 and index is None):
                return self._usage("read [N]")
            if index is not None and not self._need_snapshot():
                return
            ui.emit(redact(ui.clean(await driver.read_text(self.target(index) if index is not None else None))))
        elif cmd == "goto":
            if len(args) != 1:
                return self._usage("goto URL")
            await driver.goto(args[0])
            _say("success", f"URL: {redact(ui.clean(await driver.current_url()))}")
        elif cmd == "hl":
            if len(args) != 1 or args[0].lower() not in ("all", "off"):
                return self._usage("hl all|off")
            await driver.highlight(args[0].lower())  # type: ignore[arg-type]
            _say("success", "OK")
        elif cmd == "tabs":
            term = _term()
            rows = [[term.glyph("pointer") if name == driver.current_tab else "", name] for name in driver.list_tabs()]
            ui.emit(ui.table(term, ["", "Tab"], rows, styles=[("bright_cyan",), ("bold",)]))
        elif cmd == "new":
            if len(args) > 1:
                return self._usage("new [URL]")
            name = await driver.new_tab(url=args[0] if args else None)
            _say("success", f"New tab: '{name}'")
        elif cmd == "switch":
            if len(args) != 1:
                return self._usage("switch NAME")
            await driver.switch_tab(args[0])
            _say("success", f"Current tab: '{args[0]}'")
        elif cmd == "close":
            if len(args) > 1:
                return self._usage("close [NAME]")
            name = args[0] if args else driver.current_tab
            if await driver.close_tab(args[0] if args else None):
                _say("success", f"Tab closed: '{name}'")
            else:
                _say("failure", f"ERROR: cannot close '{name}' (unknown tab, or the main tab)")
        elif cmd in ("shot", "screenshot", "ss"):
            if len(args) > 1:
                return self._usage("shot [file.png]")
            filename = args[0] if args else f"screenshot_{datetime.now().strftime('%H%M%S')}.png"
            png = await driver.screenshot()
            await asyncio.to_thread(Path(filename).write_bytes, png)
            _say("success", f"Saved: {filename}")
        else:
            _say("warn", f"Unknown command '{redact(ui.clean(cmd))}'. Type help to see the commands.")


async def repl(driver: BrowserDriver, bridge: BridgeServer) -> None:
    session = _Session(driver)
    term = _term()
    ui.emit()
    ui.emit(ui.rule(term, "Developer console"))
    _say("info", "BABAS developer console. Type help for the commands.")
    while True:
        try:
            line = await _ainput(ui.prompt(term, PROMPT_LABEL))
        except EOFError:
            ui.emit()
            return
        if not await session.run(line):
            return


_run_repl = repl


async def _watch_connection(bridge: BridgeServer) -> None:
    was_connected = bridge.connected
    while True:
        await asyncio.sleep(WATCH_INTERVAL)
        now_connected = bridge.connected
        if now_connected == was_connected:
            continue
        was_connected = now_connected
        if now_connected:
            _say("success", f"Extension connected (v{bridge.extension_version})")
        else:
            _say("warn", "Extension disconnected")


def _pairing_problem(path: Path) -> Optional[str]:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            return f"The pairing file {path} cannot be read ({exc.strerror or type(exc).__name__}). Run: python -m babas pair --reset"
        except ValueError:
            return f"The pairing file {path} is corrupt. Run: python -m babas pair --reset"
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            return f"The pairing file {path} is corrupt. Run: python -m babas pair --reset"
        return None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        return f"Cannot create the pairing folder {path.parent}: {exc.strerror or type(exc).__name__}"
    if not os.access(path.parent, os.W_OK | os.X_OK):
        return f"Cannot create the pairing file {path}: the folder is not writable"
    return None


def import_mcp_server() -> ModuleType:
    try:
        return importlib.import_module("babas.mcp_server")
    except ImportError as exc:
        if (exc.name or "mcp").split(".")[0] != "mcp":
            raise
        ui.emit(ui.step(ui.detect(sys.stderr), "failure", MCP_EXTRA_HINT, wrap_text=False), stream=sys.stderr)
        raise SystemExit(1) from None


def _key_note(settings: Settings) -> Optional[str]:
    fallback = "ANTHROPIC_API_KEY" if settings.llm_provider == "anthropic" else "OPENAI_API_KEY"
    missing: List[str] = []
    if settings.needs_api_key and not settings.llm_api_key:
        missing.append(f"BABAS_LLM_API_KEY (or {fallback})")
    if not settings.llm_model:
        missing.append("BABAS_LLM_MODEL")
    if not missing:
        return None
    verb, then = ("is", "it is") if len(missing) == 1 else ("are", "they are")
    return f"Note: {' and '.join(missing)} {verb} not set, so tasks from the side panel will fail until {then}."


async def _close_quietly(obj: Any) -> None:
    close = getattr(obj, "close", None)
    if close is None:
        return
    try:
        await close()
    except Exception as exc:
        log.debug("close failed: %s", type(exc).__name__)


async def run_serve(settings: Settings, repl: bool = False, mcp_http: bool = False) -> None:
    if repl and mcp_http:
        print("--mcp-http cannot be combined with --repl", file=sys.stderr)
        raise SystemExit(2)
    setup_logging("WARNING" if repl else settings.log_level)
    mcp_module = import_mcp_server() if mcp_http else None
    pairing_file = settings.pairing_file
    problem = _pairing_problem(pairing_file)
    if problem:
        _say("failure", problem)
        raise SystemExit(1)
    bridge = BridgeServer(
        settings.bridge_host, settings.bridge_port, list(settings.extension_origins), pairing_file=pairing_file
    )
    try:
        await bridge.start()
    except OSError as exc:
        _say("failure", f"Could not listen on {settings.bridge_host}:{settings.bridge_port}: {exc}")
        raise SystemExit(1) from None
    term = _term()
    ui.emit()
    ui.emit(
        ui.banner(
            term,
            [
                ("mode", "developer console" if repl else "side panel"),
                ("model", settings.llm_model or "not set"),
                ("lang", settings.language),
                ("safety", "on"),
            ],
            version=__version__,
        )
    )
    ui.emit()
    _say("running", f"BABAS bridge on ws://{settings.bridge_host}:{bridge.port} {term.glyph('dash')} waiting for the extension{term.glyph('ellipsis')}")
    ui.emit(f"    {term.paint('Pairing file:', 'dim')} {pairing_file}")
    watcher = asyncio.get_running_loop().create_task(_watch_connection(bridge))
    session: Any = None
    runtime: Any = None
    mcp_task: Optional["asyncio.Task[None]"] = None
    try:
        if repl:
            await _run_repl(ExtensionDriver(bridge), bridge)
        else:
            from babas.llm import make_llm
            from babas.session import Session

            note = _key_note(settings)
            if note:
                ui.emit()
                ui.emit(ui.note(term, note))
            session = Session(
                bridge,
                lambda: make_llm(settings),
                SafetyPolicy(settings.allowed_domains),
                language=settings.language,
                max_steps=settings.max_steps,
                max_retries=settings.max_retries,
                stt_factory=lambda: make_stt(settings),
            )
            if mcp_module is None:
                await asyncio.Event().wait()
            else:
                runtime = mcp_module.build_runtime(settings, bridge, busy=lambda: session.running)
                runtime.attach(bridge, panel_tasks=False)
                session.busy = lambda: runtime.lock.locked()
                mcp_task = asyncio.get_running_loop().create_task(
                    mcp_module.serve_http(runtime, "127.0.0.1", settings.mcp_http_port)
                )
                await asyncio.wait({mcp_task}, timeout=MCP_START_GRACE_S)
                if not mcp_task.done():
                    _say("success", f"MCP over HTTP: http://127.0.0.1:{settings.mcp_http_port}/mcp")
                await _wait_for_mcp(mcp_task)
    finally:
        watcher.cancel()
        if mcp_task is not None and not mcp_task.done():
            mcp_task.cancel()
        if mcp_task is not None:
            await asyncio.gather(mcp_task, return_exceptions=True)
        if session is not None:
            await session.stop()
        await bridge.stop()
        if runtime is not None:
            await _close_quietly(runtime.llm)


async def _wait_for_mcp(task: "asyncio.Task[None]") -> None:
    try:
        await task
    except OSError as exc:
        _say("failure", f"Could not start the MCP HTTP endpoint: {exc.strerror or type(exc).__name__}")
        raise SystemExit(1) from None
    except Exception as exc:
        log.error("the MCP HTTP endpoint failed: %s", type(exc).__name__)
        _say("failure", f"The MCP HTTP endpoint failed ({type(exc).__name__}).")
        raise SystemExit(1) from None
    _say("warn", "The MCP HTTP endpoint stopped.")
