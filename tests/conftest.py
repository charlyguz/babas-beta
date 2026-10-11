import asyncio
import functools
import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from babas.bridge.server import BridgeServer

ROOT = Path(__file__).resolve().parent.parent
PAGES_DIR = ROOT / "tests" / "fixtures" / "pages"
EXTENSION_DIST = Path(os.environ.get("BABAS_EXT_DIST") or ROOT / "extension" / "dist")
TEST_SITES = ["gob.mx", "google.com", "google.com.mx", "youtube.com", "whatsapp.com", "wikipedia.org", "127.0.0.1"]
VIEWPORT = {"width": 1280, "height": 720}


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


@pytest_asyncio.fixture
async def ext_env(pw, tmp_path):
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
            args=["--disable-extensions-except=" + dist, "--load-extension=" + dist],
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
