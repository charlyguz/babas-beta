from datetime import datetime
from functools import wraps
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from playwright.async_api import Locator, Page

from babas.schemas import Step

if TYPE_CHECKING:
    from babas.drivers.playwright_driver import PlaywrightDriver


class ActionRegistry:
    _actions: Dict[str, Callable] = {}
    _descriptions: Dict[str, str] = {}

    @classmethod
    def register(cls, action_name: str, description: str = ""):
        def decorator(func: Callable):
            @wraps(func)
            def wrapper(*args, **kwargs):
                return func(*args, **kwargs)
            cls._actions[action_name] = wrapper
            cls._descriptions[action_name] = description
            return wrapper
        return decorator

    @classmethod
    def get(cls, action_name: str) -> Optional[Callable]:
        return cls._actions.get(action_name)

    @classmethod
    def get_all_descriptions(cls) -> str:
        lines = []
        for name, desc in cls._descriptions.items():
            lines.append(f"- {name}: {desc}")
        return "\n".join(lines)


class ActionExecutor:
    def __init__(self, driver: "PlaywrightDriver"):
        self.driver = driver
        self.last_error = None
        self.last_context = None
        self._register_all_actions()

    @property
    def page(self) -> Page:
        return self.driver.page

    @property
    def current_tab_name(self) -> str:
        return self.driver.current_tab

    def _register_all_actions(self):
        @ActionRegistry.register("goto", "Navigate to URL")
        async def goto(executor, step):
            print(f"   Navigating to {step.value}...")
            await executor.page.goto(step.value, wait_until="domcontentloaded", timeout=30000)
            await executor.page.wait_for_timeout(2000)
            print(f"   Page loaded")

        @ActionRegistry.register("wait", "Wait X milliseconds")
        async def wait(executor, step):
            ms = step.ms or 2000
            print(f"   Waiting {ms}ms...")
            await executor.page.wait_for_timeout(ms)

        @ActionRegistry.register("click", "Click element by CSS selector")
        async def click(executor, step):
            print(f"   Click: {step.selector}")
            await executor.page.locator(step.selector).first.click(timeout=10000)
            print(f"   Done")

        @ActionRegistry.register("type_text", "Type text in field")
        async def type_text(executor, step):
            print(f"   Typing: '{step.value}' in {step.selector}")
            await executor.page.locator(step.selector).first.fill(step.value, timeout=10000)
            print(f"   Done")

        @ActionRegistry.register("press_key", "Press key")
        async def press_key(executor, step):
            print(f"   Key: {step.value}")
            await executor.page.keyboard.press(step.value)

        @ActionRegistry.register("screenshot", "Take screenshot")
        async def screenshot(executor, step):
            filename = f"screenshot_{datetime.now().strftime('%H%M%S')}.png"
            await executor.page.screenshot(path=filename)
            print(f"   Saved: {filename}")

        @ActionRegistry.register("scroll", "Scroll up/down")
        async def scroll(executor, step):
            amount = step.ms or 500
            direction = 1 if step.value == "down" else -1
            await executor.page.mouse.wheel(0, amount * direction)
            print(f"   Scroll {step.value}")

        @ActionRegistry.register("smart_search", "Auto-find search box and type")
        async def smart_search(executor, step):
            print(f"   Searching for: '{step.value}'")
            search_selectors = [
                "input[type='search']", "input[name*='search']", "input[name*='query']", "input[name*='q']",
                "input[placeholder*='Search']", "input[placeholder*='Buscar']",
                "input[aria-label*='Search']", "input[aria-label*='Buscar']",
                "[role='searchbox']", "[role='combobox']",
                "input[id*='search']", "input[class*='search']", "textarea[name*='search']"
            ]
            for selector in search_selectors:
                try:
                    elem = executor.page.locator(selector).first
                    if await elem.is_visible(timeout=1000):
                        await elem.click()
                        await elem.fill(step.value)
                        await elem.press("Enter")
                        print(f"   Search done with: {selector}")
                        return
                except Exception:
                    continue
            raise RuntimeError("Search box not found")

        @ActionRegistry.register("smart_click", "Click by visible text")
        async def smart_click(executor, step):
            print(f"   Looking for: '{step.value}'")
            strategies = [
                f"text='{step.value}'", f"button:has-text('{step.value}')",
                f"a:has-text('{step.value}')", f"[role='button']:has-text('{step.value}')",
                f"*:has-text('{step.value}')"
            ]
            for strategy in strategies:
                try:
                    elem = executor.page.locator(strategy).first
                    if await elem.is_visible(timeout=1000):
                        await elem.click()
                        print(f"   Clicked: {strategy}")
                        return
                except Exception:
                    continue
            raise RuntimeError(f"Element not found: {step.value}")

        @ActionRegistry.register("analyze_page", "Analyze page elements")
        async def analyze_page(executor, step):
            print(f"   Analyzing page...")
            summary = await executor.get_current_context()
            executor.last_context = summary
            print(f"   URL: {summary.get('url', 'unknown')}")
            print(f"   Title: {summary.get('title', 'unknown')}")
            if summary.get("elements", {}).get("search_boxes"):
                print(f"   Search boxes found: {len(summary['elements']['search_boxes'])}")
            return summary

        @ActionRegistry.register("new_tab", "Open new tab")
        async def new_tab(executor, step):
            await executor.driver.new_tab(step.tab_name or step.value)

        @ActionRegistry.register("switch_tab", "Switch to tab")
        async def switch_tab(executor, step):
            tab_name = step.tab_name or step.value
            page = executor.driver.get_tab_page(tab_name)
            if page:
                await executor.driver.switch_tab(tab_name)
                print(f"   Switched to: '{tab_name}'")
            else:
                raise ValueError(f"Tab '{tab_name}' not found")

        @ActionRegistry.register("close_tab", "Close tab")
        async def close_tab(executor, step):
            await executor.driver.close_tab(step.tab_name or step.value or None)

        @ActionRegistry.register("search_youtube", "Search on YouTube")
        async def search_youtube(executor, step):
            print(f"   YouTube: '{step.value}'")
            await executor._close_popups()
            search = await executor._find_element(['input[name="search_query"]', 'input#search', 'input[placeholder*="Search"]'])
            await search.fill(step.value)
            await search.press("Enter")
            await executor.page.wait_for_timeout(2000)
            print(f"   Search done")

        @ActionRegistry.register("click_first_video", "Click first YouTube video")
        async def click_first_video(executor, step):
            print(f"   Selecting video...")
            await executor.page.wait_for_selector("ytd-video-renderer", timeout=10000)
            await executor.page.locator("ytd-video-renderer a#thumbnail").first.click()
            print(f"   Video selected")

        @ActionRegistry.register("open_gmail", "Open Gmail")
        async def open_gmail(executor, step):
            await executor.page.goto("https://mail.google.com", wait_until="domcontentloaded")
            await executor.page.wait_for_timeout(3000)
            print(f"   Gmail opened")

        @ActionRegistry.register("compose_email", "Compose email")
        async def compose_email(executor, step):
            print(f"   Composing email...")
            await executor.page.locator('div[role="button"]:has-text("Compose"), div[role="button"]:has-text("Redactar")').first.click()
            await executor.page.wait_for_timeout(2000)
            await executor.page.locator('input[name="to"]').fill(step.to)
            if step.subject:
                await executor.page.locator('input[name="subjectbox"]').fill(step.subject)
            if step.body:
                await executor.page.locator('div[role="textbox"]').last.fill(step.body)
            print(f"   Email ready")

        @ActionRegistry.register("send_email", "Send email")
        async def send_email(executor, step):
            await executor.page.locator('div[role="button"]:has-text("Send"), div[role="button"]:has-text("Enviar")').first.click()
            await executor.page.wait_for_timeout(2000)
            print(f"   Email sent")

        @ActionRegistry.register("open_whatsapp", "Open WhatsApp Web")
        async def open_whatsapp(executor, step):
            await executor.page.goto("https://web.whatsapp.com", wait_until="domcontentloaded")
            print(f"   WhatsApp - Scan QR if needed")
            await executor.page.wait_for_timeout(5000)

        @ActionRegistry.register("search_contact", "Search WhatsApp contact")
        async def search_contact(executor, step):
            search = await executor._find_element(['div[contenteditable="true"][data-tab="3"]', 'div[title*="Search"]'])
            await search.fill(step.value)
            await executor.page.wait_for_timeout(2000)
            await executor.page.locator('div[role="listitem"]').first.click()

        @ActionRegistry.register("send_whatsapp_message", "Send WhatsApp message")
        async def send_whatsapp_message(executor, step):
            msg_box = await executor._find_element(['div[contenteditable="true"][data-tab="10"]', 'footer div[contenteditable="true"]'])
            await msg_box.fill(step.body)
            await msg_box.press("Enter")
            print(f"   Message sent")

    async def _find_element(self, selectors: List[str]) -> Locator:
        for selector in selectors:
            try:
                elem = self.page.locator(selector).first
                if await elem.is_visible(timeout=2000):
                    return elem
            except Exception:
                continue
        raise RuntimeError("Element not found")

    async def _close_popups(self) -> None:
        popups = ['button[aria-label*="Accept"]', 'button:has-text("Accept")']
        for sel in popups:
            try:
                if await self.page.locator(sel).is_visible(timeout=1000):
                    await self.page.locator(sel).first.click()
                    break
            except Exception:
                continue

    async def execute(self, step: Step) -> Any:
        action_func = ActionRegistry.get(step.action)
        if action_func:
            return await action_func(self, step)
        else:
            print(f"   Unknown action: {step.action}")
            return None

    async def get_current_context(self) -> Dict:
        return await self.driver.legacy_context()
