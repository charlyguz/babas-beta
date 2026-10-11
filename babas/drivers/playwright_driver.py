import asyncio
import importlib.resources
import logging
import os
import platform
import re
import socket
import subprocess
from contextlib import asynccontextmanager
from functools import wraps
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Literal, NamedTuple, Optional, Type, TypeVar, cast

from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Locator,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
)
from pydantic import BaseModel, ValidationError

from babas.drivers.base import (
    FLAG_PASSWORD,
    BrowserDriver,
    DriverError,
    Element,
    ErrorCode,
    PageState,
    ScrollDirection,
    Target,
)

log = logging.getLogger("babas.playwright_driver")

PAGE_SCRIPT = importlib.resources.files("babas.drivers").joinpath("page_script.js").read_text(encoding="utf-8")

DEBUG_PORTS = [9222, 9223, 9224, 9225]

ACTION_TIMEOUT_MS = 5000
GOTO_TIMEOUT_MS = 30000
LOAD_TIMEOUT_MS = 10000

KEY_ALIASES = {"Return": "Enter", "Esc": "Escape"}
MARK_PATTERN = re.compile(r"^[A-Za-z0-9]{1,32}$")
PASSWORD_PROBE = (
    "el => (el instanceof HTMLInputElement && (el.getAttribute('type') || 'text').toLowerCase() === 'password')"
    " || (el.getAttribute('autocomplete') || '').toLowerCase().includes('password')"
)

F = TypeVar("F", bound=Callable[..., Awaitable[Any]])
M = TypeVar("M", bound=BaseModel)


def _mapped(fn: F) -> F:
    @wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except PlaywrightTimeoutError as e:
            raise DriverError(ErrorCode.TIMEOUT, str(e)) from e
        except PlaywrightError as e:
            raise DriverError(ErrorCode.INTERNAL, str(e)) from e

    return cast(F, wrapper)


def _parse(model: Type[M], data: Any) -> M:
    try:
        return model.model_validate(data)
    except ValidationError as e:
        raise DriverError(ErrorCode.INTERNAL, f"Unexpected page response: {e}") from e


def _field(result: Any, key: str, kind: type) -> Any:
    value = result.get(key) if isinstance(result, dict) else None
    if not isinstance(value, kind):
        raise DriverError(ErrorCode.INTERNAL, f"Unexpected page response: missing '{key}'")
    return value


def _target(target: Target) -> Dict[str, Any]:
    return target.model_dump(exclude_none=True)


class _Located(NamedTuple):
    locator: Locator
    mark: Optional[str]


class PlaywrightDriver(BrowserDriver):
    name = "playwright"

    def __init__(
        self,
        context: BrowserContext,
        initial_page: Page,
        *,
        attached: bool = False,
        browser: Optional[Browser] = None,
    ):
        self.context = context
        self.attached = attached
        self.browser = browser
        self._tabs: Dict[str, Page] = {"main": initial_page}
        self._current = "main"
        self._tab_counter = 0
        self._started = False

    @property
    def page(self) -> Page:
        return self._tabs[self._current]

    @property
    def current_tab(self) -> str:
        return self._current

    def list_tabs(self) -> List[str]:
        return list(self._tabs.keys())

    def get_tab_page(self, name: str) -> Optional[Page]:
        return self._tabs.get(name)

    @_mapped
    async def start(self) -> None:
        if self._started:
            return
        await self.context.add_init_script(script=PAGE_SCRIPT)
        self._started = True

    async def _ensure(self, page: Page) -> None:
        if not await page.evaluate("() => !!window.__babas"):
            await page.evaluate("() => {" + PAGE_SCRIPT + "\n}")

    async def _call(self, cmd: Dict[str, Any], page: Optional[Page] = None) -> Any:
        page = page or self.page
        await self._ensure(page)
        response = await page.evaluate("c => window.__babas.handle(c)", cmd)
        if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
            raise DriverError(ErrorCode.INTERNAL, "Unexpected page response")
        if not response["ok"]:
            error = response.get("error")
            error = error if isinstance(error, dict) else {}
            code = error.get("code")
            message = error.get("message")
            data = error.get("data")
            raise DriverError(
                code if isinstance(code, str) else ErrorCode.INTERNAL,
                message if isinstance(message, str) else "",
                data if isinstance(data, dict) else None,
            )
        return response.get("result")

    async def _unmark(self, page: Page) -> None:
        try:
            await self._call({"op": "unmark"}, page)
        except Exception as e:
            log.debug("unmark failed: %s", e)

    @asynccontextmanager
    async def _located(self, target: Target) -> AsyncIterator[_Located]:
        page = self.page
        if target.index is None and target.selector:
            yield _Located(page.locator(target.selector).first, None)
            return
        result = await self._call({"op": "mark", "target": _target(target)}, page)
        mark = _field(result, "mark", str)
        if not MARK_PATTERN.match(mark):
            raise DriverError(ErrorCode.INTERNAL, "Unexpected page response: bad mark")
        try:
            yield _Located(page.locator(f'[data-babas-mark="{mark}"]'), mark)
        finally:
            await self._unmark(page)

    @_mapped
    async def goto(self, url: str) -> None:
        page = self.page
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            raise
        except PlaywrightError as e:
            raise DriverError(ErrorCode.NAVIGATION_FAILED, str(e)) from e
        try:
            await page.wait_for_load_state("load", timeout=LOAD_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            pass

    @_mapped
    async def current_url(self) -> str:
        return self.page.url

    @_mapped
    async def get_page_state(self, highlight: bool = False) -> PageState:
        result = await self._call({"op": "snapshot", "highlight": highlight, "max_elements": 250, "max_text": 2000})
        state = _parse(PageState, result)
        state.tab = self.current_tab
        return state

    @_mapped
    async def describe(self, target: Target) -> Element:
        result = await self._call({"op": "describe", "target": _target(target)})
        return _parse(Element, _field(result, "element", dict))

    @_mapped
    async def is_visible(self, target: Target) -> bool:
        result = await self._call({"op": "exists", "target": _target(target)})
        return _field(result, "visible", bool)

    @_mapped
    async def click(self, target: Target, *, confirmation: Optional[str] = None) -> None:
        async with self._located(target) as (locator, _):
            await locator.click(timeout=ACTION_TIMEOUT_MS)

    @_mapped
    async def type_text(self, target: Target, text: str, *, confirmation: Optional[str] = None) -> None:
        async with self._located(target) as (locator, mark):
            if mark is None:
                is_password = await locator.evaluate(PASSWORD_PROBE, timeout=ACTION_TIMEOUT_MS)
            else:
                is_password = (await self.describe(Target(selector=f'[data-babas-mark="{mark}"]'))).has(FLAG_PASSWORD)
            if is_password:
                raise DriverError(ErrorCode.PASSWORD_FIELD, "BABAS never types passwords: ask the user to type it")
            await locator.fill(text, timeout=ACTION_TIMEOUT_MS)

    @_mapped
    async def press_key(self, key: str, target: Optional[Target] = None, *, confirmation: Optional[str] = None) -> None:
        key = KEY_ALIASES.get(key, key)
        if target is None:
            await self.page.keyboard.press(key)
            return
        async with self._located(target) as (locator, _):
            await locator.press(key, timeout=ACTION_TIMEOUT_MS)

    @_mapped
    async def select_option(self, target: Target, value: str, *, confirmation: Optional[str] = None) -> str:
        # authorized is always True here: on this path only the Python gate enforces confirmations
        result = await self._call({"op": "select", "target": _target(target), "value": value, "authorized": True})
        return _field(result, "selected", str)

    @_mapped
    async def scroll(self, direction: ScrollDirection = "down", amount: int = 500) -> None:
        await self._call({"op": "scroll", "direction": direction, "amount": amount})

    @_mapped
    async def read_text(self, target: Optional[Target] = None) -> str:
        cmd: Dict[str, Any] = {"op": "read"}
        if target is not None:
            cmd["target"] = _target(target)
        result = await self._call(cmd)
        return _field(result, "text", str)

    @_mapped
    async def screenshot(self) -> bytes:
        return await self.page.screenshot()

    @_mapped
    async def highlight(self, mode: Literal["all", "off"] = "all") -> None:
        await self._call({"op": "highlight", "mode": mode})

    @_mapped
    async def new_tab(self, name: Optional[str] = None, url: Optional[str] = None) -> str:
        self._tab_counter += 1
        tab_name = name or f"tab_{self._tab_counter}"
        page = await self.context.new_page()
        self._tabs[tab_name] = page
        print(f"   New tab: '{tab_name}'")
        self._current = tab_name
        if url:
            await self.goto(url)
        return tab_name

    @_mapped
    async def switch_tab(self, name: str) -> None:
        if name not in self._tabs:
            raise DriverError(ErrorCode.TAB_NOT_FOUND, f"Tab '{name}' not found")
        self._current = name

    @_mapped
    async def close_tab(self, name: Optional[str] = None) -> bool:
        tab_name = name or self._current
        page = self._tabs.get(tab_name)
        if page is None or tab_name == "main":
            return False
        await page.close()
        del self._tabs[tab_name]
        print(f"   Tab closed: '{tab_name}'")
        if tab_name == self._current:
            self._current = "main"
        return True

    @_mapped
    async def wait(self, ms: int) -> None:
        await self.page.wait_for_timeout(max(ms, 0))

    async def close(self) -> int:
        closed = 0
        for page in list(self._tabs.values()):
            try:
                if not page.is_closed():
                    await page.close()
                    closed += 1
            except Exception:
                pass
        try:
            if self.attached:
                if self.browser is not None:
                    await self.browser.close()
            else:
                await self.context.close()
        except Exception:
            pass
        return closed


def check_port(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(('localhost', port)) == 0

def scan_debug_ports() -> Optional[int]:
    for port in DEBUG_PORTS:
        if check_port(port):
            return port
    return None

def get_browser_info() -> Dict[str, Dict]:
    system = platform.system().lower()
    if system == "windows":
        chrome_paths = [
            os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]
        brave_paths = [
            os.path.expandvars(r"%PROGRAMFILES%\BraveSoftware\Brave-Browser\Application\brave.exe"),
            os.path.expandvars(r"%PROGRAMFILES(X86)%\BraveSoftware\Brave-Browser\Application\brave.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"),
        ]
        edge_paths = [
            os.path.expandvars(r"%PROGRAMFILES%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe"),
        ]
        chrome_path = next((p for p in chrome_paths if os.path.exists(p)), None)
        brave_path = next((p for p in brave_paths if os.path.exists(p)), None)
        edge_path = next((p for p in edge_paths if os.path.exists(p)), None)
        chrome_profile = os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data")
        brave_profile = os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data")
        edge_profile = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\User Data")
        return {
            "brave": {"path": brave_path, "profile": brave_profile, "port": 9222},
            "chrome": {"path": chrome_path, "profile": chrome_profile, "port": 9223},
            "edge": {"path": edge_path, "profile": edge_profile, "port": 9224},
        }
    else:
        return {
            "brave": {
                "path": "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser" if system == "darwin" else "/usr/bin/brave-browser",
                "profile": os.path.expanduser("~/.config/BraveSoftware/Brave-Browser"),
                "port": 9222
            },
            "chrome": {
                "path": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" if system == "darwin" else "/usr/bin/google-chrome",
                "profile": os.path.expanduser("~/.config/google-chrome"),
                "port": 9223
            },
        }

def get_available_browsers() -> List[str]:
    browsers = get_browser_info()
    available = []
    for name in ["brave", "chrome", "edge"]:
        info = browsers.get(name, {})
        if info.get("path") and os.path.exists(info["path"]):
            available.append(name)
    return available

def get_preferred_browser() -> Optional[str]:
    available = get_available_browsers()
    return available[0] if available else None

async def try_connect_to_browser(playwright: Playwright) -> tuple[Optional[Any], Optional[int], Optional[str]]:
    port = scan_debug_ports()
    if port:
        try:
            browser = await playwright.chromium.connect_over_cdp(f"http://localhost:{port}")
            return browser, port, "detected"
        except Exception:
            pass
    return None, None, None

def is_profile_locked(profile_path: str) -> bool:
    lock_files = [
        os.path.join(profile_path, "lockfile"),
        os.path.join(profile_path, "SingletonLock"),
        os.path.join(profile_path, "SingletonSocket"),
    ]
    for lock in lock_files:
        if os.path.exists(lock):
            return True
    return False

def get_babas_profile_path(browser_name: str) -> str:
    system = platform.system().lower()
    if system == "windows":
        return os.path.expandvars(rf"%LOCALAPPDATA%\babas-{browser_name}-profile")
    else:
        return os.path.expanduser(f"~/.babas-{browser_name}-profile")

def launch_browser_with_debug(browser_name: str) -> tuple[bool, int, str]:
    browsers = get_browser_info()
    info = browsers.get(browser_name)
    if not info or not info.get("path") or not os.path.exists(info["path"]):
        return False, 0, "Browser not found"
    port = info.get("port", 9222)
    system = platform.system().lower()
    profile = get_babas_profile_path(browser_name)
    print(f"   BABAS profile: {os.path.basename(profile)}")
    args = [
        info["path"],
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
    ]
    try:
        if system == "windows":
            subprocess.Popen(args, creationflags=subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return True, port, "ok"
    except Exception as e:
        return False, 0, str(e)

async def try_launch_and_connect(playwright: Playwright, browser_name: str) -> tuple[bool, Any, int]:
    print(f"\nLaunching {browser_name.capitalize()}...")
    success, port, msg = launch_browser_with_debug(browser_name)
    if not success:
        print(f"   Error: {msg}")
        return False, None, 0
    print(f"   Connecting...")
    for i in range(30):
        await asyncio.sleep(0.5)
        if check_port(port):
            await asyncio.sleep(1)
            try:
                browser = await playwright.chromium.connect_over_cdp(f"http://localhost:{port}")
                contexts = browser.contexts
                pages_count = len(contexts[0].pages) if contexts else 0
                print(f"   Connected! ({pages_count} tabs)")
                return True, browser, port
            except Exception:
                continue
    print("   Could not connect")
    return False, None, 0
