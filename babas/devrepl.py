import asyncio
import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from babas.bridge.server import BridgeServer, default_pairing_file
from babas.drivers.base import BrowserDriver, DriverError, Element, Target
from babas.drivers.extension_driver import ExtensionDriver
from babas.logs import setup_logging
from babas.safety import SafetyPolicy

log = logging.getLogger("babas.devrepl")

PROMPT = "babas> "
WATCH_INTERVAL = 0.2
DEFAULT_SCROLL = 500

HELP_LINES = [
    "COMMANDS:",
    "   ls                    -> list the elements of the page (highlights them)",
    "   click N               -> click element N of the last ls",
    "   type N text...        -> type text into element N",
    "   select N value...     -> choose an option of select N",
    "   press KEY [N]         -> press a key (Enter, Tab, Escape, ...), optionally on element N",
    "   scroll up|down [px]   -> scroll the page (default 500)",
    "   read [N]              -> read the page text, or the text of element N",
    "   goto URL              -> open an http(s) URL in the current tab",
    "   hl all|off            -> show or hide the highlights",
    "   tabs                  -> list the tabs BABAS knows",
    "   new [URL]             -> open a new tab",
    "   switch NAME           -> make a tab current",
    "   close [NAME]          -> close a tab (default: the current one)",
    "   shot [file.png]       -> save a screenshot of the visible area",
    "   help, ?               -> this list",
    "   quit, exit, q         -> stop",
]


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
            line = input(prompt)
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


def _format_element(el: Element) -> str:
    line = f'[{el.index}] {el.role} "{_one_line(el.label)}"'
    if el.flags:
        line += " {" + ",".join(el.flags) + "}"
    if el.value:
        line += f" = {_escape_newlines(el.value)}"
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
            for text in HELP_LINES:
                print(text)
            return True
        try:
            await _ensure_started(self.driver)
            await self._dispatch(cmd, line.lstrip())
        except DriverError as exc:
            print(f"   ERROR {exc.code}: {exc.message}")
            if exc.data:
                print(f"   data: {json.dumps(exc.data, ensure_ascii=False, default=str)}")
        except Exception as exc:
            log.exception("repl command failed: %s", cmd)
            print(f"   ERROR INTERNAL: {type(exc).__name__}: {exc}")
        return True

    def _need_snapshot(self) -> bool:
        if self.snapshot_id is None:
            print("   ERROR: run ls first")
            return False
        return True

    @staticmethod
    def _usage(text: str) -> None:
        print(f"   ERROR: usage: {text}")

    async def _dispatch(self, cmd: str, line: str) -> None:
        driver = self.driver
        parts = line.split()
        args = parts[1:]

        if cmd == "ls":
            state = await driver.get_page_state(highlight=True)
            self.snapshot_id = state.snapshot_id
            print(f"URL: {state.url}")
            print(f"TITLE: {state.title}")
            for el in state.elements:
                print(_format_element(el))
        elif cmd == "click":
            index = _parse_index(args[0]) if len(args) == 1 else None
            if index is None:
                return self._usage("click N")
            if self._need_snapshot():
                await driver.click(self.target(index))
                print("   OK")
        elif cmd in ("type", "select"):
            pieces = line.split(None, 2)
            index = _parse_index(pieces[1]) if len(pieces) == 3 else None
            if index is None:
                return self._usage(f"{cmd} N {'text' if cmd == 'type' else 'value'}...")
            if self._need_snapshot():
                if cmd == "type":
                    await driver.type_text(self.target(index), pieces[2])
                    print("   OK")
                else:
                    print(f"   Selected: {await driver.select_option(self.target(index), pieces[2])}")
        elif cmd == "press":
            if len(args) not in (1, 2):
                return self._usage("press KEY [N]")
            index = _parse_index(args[1]) if len(args) == 2 else None
            if len(args) == 2 and index is None:
                return self._usage("press KEY [N]")
            if index is not None and not self._need_snapshot():
                return
            await driver.press_key(args[0], self.target(index) if index is not None else None)
            print("   OK")
        elif cmd == "scroll":
            amount = _parse_index(args[1]) if len(args) == 2 else DEFAULT_SCROLL
            if len(args) not in (1, 2) or args[0].lower() not in ("up", "down") or amount is None:
                return self._usage("scroll up|down [px]")
            await driver.scroll(args[0].lower(), amount)  # type: ignore[arg-type]
            print("   OK")
        elif cmd == "read":
            index = _parse_index(args[0]) if len(args) == 1 else None
            if len(args) > 1 or (len(args) == 1 and index is None):
                return self._usage("read [N]")
            if index is not None and not self._need_snapshot():
                return
            print(await driver.read_text(self.target(index) if index is not None else None))
        elif cmd == "goto":
            if len(args) != 1:
                return self._usage("goto URL")
            await driver.goto(args[0])
            print(f"   URL: {await driver.current_url()}")
        elif cmd == "hl":
            if len(args) != 1 or args[0].lower() not in ("all", "off"):
                return self._usage("hl all|off")
            await driver.highlight(args[0].lower())  # type: ignore[arg-type]
            print("   OK")
        elif cmd == "tabs":
            print("Tabs:")
            for name in driver.list_tabs():
                marker = "->" if name == driver.current_tab else "  "
                print(f"   {marker} [{name}]")
        elif cmd == "new":
            if len(args) > 1:
                return self._usage("new [URL]")
            name = await driver.new_tab(url=args[0] if args else None)
            print(f"   New tab: '{name}'")
        elif cmd == "switch":
            if len(args) != 1:
                return self._usage("switch NAME")
            await driver.switch_tab(args[0])
            print(f"   Current tab: '{args[0]}'")
        elif cmd == "close":
            if len(args) > 1:
                return self._usage("close [NAME]")
            name = args[0] if args else driver.current_tab
            if await driver.close_tab(args[0] if args else None):
                print(f"   Tab closed: '{name}'")
            else:
                print(f"   ERROR: cannot close '{name}' (unknown tab, or the main tab)")
        elif cmd in ("shot", "screenshot", "ss"):
            if len(args) > 1:
                return self._usage("shot [file.png]")
            filename = args[0] if args else f"screenshot_{datetime.now().strftime('%H%M%S')}.png"
            png = await driver.screenshot()
            await asyncio.to_thread(Path(filename).write_bytes, png)
            print(f"   Saved: {filename}")
        else:
            print(f"   Unknown command '{cmd}'. Type help to see the commands.")


async def repl(driver: BrowserDriver, bridge: BridgeServer) -> None:
    session = _Session(driver)
    print("BABAS developer console. Type help for the commands.")
    while True:
        try:
            line = await _ainput(PROMPT)
        except EOFError:
            print()
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
            print(f"Extension connected (v{bridge.extension_version})")
        else:
            print("Extension disconnected")


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


async def run_serve(host: str, port: int, repl: bool) -> None:
    setup_logging("WARNING" if repl else "INFO")
    pairing_file = default_pairing_file()
    problem = _pairing_problem(pairing_file)
    if problem:
        print(problem)
        raise SystemExit(1)
    bridge = BridgeServer(host, port, pairing_file=pairing_file)
    try:
        await bridge.start()
    except OSError as exc:
        print(f"Could not listen on {host}:{port}: {exc}")
        raise SystemExit(1) from None
    print(f"BABAS bridge on ws://{host}:{bridge.port} — waiting for the extension…")
    print(f"Pairing file: {pairing_file}")
    watcher = asyncio.get_running_loop().create_task(_watch_connection(bridge))
    session: Any = None
    try:
        if repl:
            await _run_repl(ExtensionDriver(bridge), bridge)
        else:
            from babas.llm import OpenAIClient
            from babas.session import Session

            if not os.environ.get("OPENAI_API_KEY"):
                print("Note: OPENAI_API_KEY is not set, so tasks from the side panel will fail until it is.")
            session = Session(bridge, lambda: OpenAIClient(), SafetyPolicy.from_env())
            await asyncio.Event().wait()
    finally:
        watcher.cancel()
        if session is not None:
            await session.stop()
        await bridge.stop()
