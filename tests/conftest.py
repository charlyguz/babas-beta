import asyncio
import copy
import functools
import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Dict, List, Optional

import pytest
import pytest_asyncio
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright, expect

from babas.bridge.protocol import PINNED_EXTENSION_ID
from babas.bridge.server import BridgeServer

ROOT = Path(__file__).resolve().parent.parent
PAGES_DIR = ROOT / "tests" / "fixtures" / "pages"
EXTENSION_DIST = Path(os.environ.get("BABAS_EXT_DIST") or ROOT / "extension" / "dist")
TEST_SITES = ["gob.mx", "google.com", "google.com.mx", "youtube.com", "whatsapp.com", "wikipedia.org", "127.0.0.1"]
VIEWPORT = {"width": 1280, "height": 720}
PANEL_URL = f"chrome-extension://{PINNED_EXTENSION_ID}/sidepanel.html"
PANEL_CONNECT_TIMEOUT_MS = 15000


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return None


async def _set_bridge_port(sw, port: int) -> None:
    # The worker handle can be returned before chrome.* is bound inside it.
    sites = ", ".join(f'"{site}"' for site in TEST_SITES)
    expression = f"chrome.storage.local.set({{bridgePort: {port}, allowedSites: [{sites}]}})"
    for _ in range(50):
        try:
            await sw.evaluate(expression)
            return
        except PlaywrightError:
            await asyncio.sleep(0.1)
    await sw.evaluate(expression)


@pytest.fixture(scope="session")
def pages_url():
    handler = functools.partial(_QuietHandler, directory=str(PAGES_DIR))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest_asyncio.fixture
async def pw():
    async with async_playwright() as p:
        yield p


@pytest_asyncio.fixture
async def playwright_driver(pw):
    from babas.drivers.playwright_driver import PlaywrightDriver

    browser = await pw.chromium.launch(headless=True)
    try:
        context = await browser.new_context(viewport=VIEWPORT)
        page = await context.new_page()
        driver = PlaywrightDriver(context, page, browser=browser)
        await driver.start()
        try:
            yield driver
        finally:
            await driver.close()
    finally:
        await browser.close()


@pytest.fixture
def chromium_extra_args():
    # Override in a test module (or indirectly parametrize it) to give ext_env extra Chromium flags.
    return []


@pytest_asyncio.fixture
async def ext_env(pw, tmp_path, chromium_extra_args):
    if not (EXTENSION_DIST / "manifest.json").is_file():
        pytest.skip("extension/dist/manifest.json is missing: run `cd extension && npm ci && npm run build` first")
    dist = str(EXTENSION_DIST)
    bridge = BridgeServer(port=0)
    await bridge.start()
    context = None
    try:
        context = await pw.chromium.launch_persistent_context(
            str(tmp_path / "profile"),
            channel="chromium",
            headless=True,
            viewport=VIEWPORT,
            args=["--disable-extensions-except=" + dist, "--load-extension=" + dist, *chromium_extra_args],
            env={**os.environ, "LANGUAGE": "es_MX:es"},
        )
        if context.service_workers:
            sw = context.service_workers[0]
        else:
            sw = await context.wait_for_event("serviceworker", timeout=15000)
        await _set_bridge_port(sw, bridge.port)
        assert await bridge.wait_connected(15), "the extension did not connect to the test bridge within 15 s"
        yield SimpleNamespace(bridge=bridge, context=context, sw=sw)
    finally:
        try:
            if context is not None:
                await context.close()
        finally:
            await bridge.stop()


@pytest_asyncio.fixture
async def extension_driver(ext_env):
    from babas.drivers.extension_driver import ExtensionDriver

    driver = ExtensionDriver(ext_env.bridge)
    await driver.start()
    try:
        yield driver
    finally:
        await driver.close()


@pytest.fixture(params=["playwright", "extension"])
def driver(request):
    return request.getfixturevalue(f"{request.param}_driver")


class BridgeLog(list):
    """(request type, payload) for every request the Python side sent to the extension."""

    def of(self, *types: str) -> List[Dict[str, Any]]:
        return [payload for kind, payload in self if kind in types]

    def count(self, *types: str) -> int:
        return len(self.of(*types))


@pytest.fixture
def fake_llm():
    from helpers import FakeLLM

    return FakeLLM([])


@pytest_asyncio.fixture
async def session(ext_env, fake_llm):
    from babas.safety import DEFAULT_ALLOWED_DOMAINS, SafetyPolicy
    from babas.session import Session

    policy = SafetyPolicy([*DEFAULT_ALLOWED_DOMAINS, "127.0.0.1"])
    running = Session(ext_env.bridge, lambda: fake_llm, policy)
    try:
        yield running
    finally:
        await running.stop()


@pytest_asyncio.fixture
async def work_page(ext_env):
    # The extension adopts the most recently used tab that is not one of its own pages, so keep it the only one.
    pages = [p for p in ext_env.context.pages if not p.url.startswith("chrome-extension://")]
    return pages[0] if pages else await ext_env.context.new_page()


@pytest_asyncio.fixture
async def panel(ext_env, work_page):
    page = await ext_env.context.new_page()
    script_errors: List[str] = []
    page.on("pageerror", lambda error: script_errors.append(str(error)))
    await page.goto(PANEL_URL)
    await expect(page.get_by_test_id("status")).to_have_text("Conectado", timeout=PANEL_CONNECT_TIMEOUT_MS)
    yield page
    assert script_errors == [], f"the side panel raised uncaught errors: {script_errors}"


@pytest.fixture
def bridge_log(ext_env, monkeypatch):
    calls = BridgeLog()
    original = ext_env.bridge.request

    async def recording(type: str, payload: Optional[Dict[str, Any]] = None, timeout: float = 30.0) -> Dict[str, Any]:
        calls.append((type, copy.deepcopy(payload or {})))
        return await original(type, payload, timeout)

    monkeypatch.setattr(ext_env.bridge, "request", recording)
    return calls


@pytest.fixture
def fixture_url(pages_url) -> Callable[[str], str]:
    def build(name: str) -> str:
        return f"{pages_url}/{name}"

    return build


@pytest.fixture
def open_fixture(work_page, fixture_url) -> Callable[[str], Awaitable[None]]:
    async def open_it(name: str) -> None:
        await work_page.goto(fixture_url(name))

    return open_it


@pytest.fixture
def field_value(work_page) -> Callable[[str], Awaitable[str]]:
    async def read(selector: str) -> str:
        return await work_page.eval_on_selector(
            selector, "el => (typeof el.value === 'string' ? el.value : el.textContent || '')"
        )

    return read


@pytest.fixture
def submit_task(panel) -> Callable[[str], Awaitable[None]]:
    async def submit(text: str) -> None:
        await panel.get_by_test_id("task-input").fill(text)
        await panel.get_by_test_id("send").click()

    return submit


@pytest.fixture
def point_extension_at(ext_env) -> Callable[[BridgeServer], Awaitable[None]]:
    async def point(bridge: BridgeServer) -> None:
        await _set_bridge_port(ext_env.sw, bridge.port)

    return point
