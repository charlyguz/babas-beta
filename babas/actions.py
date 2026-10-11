import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from babas.drivers.base import (
    FLAG_DISABLED,
    FLAG_PASSWORD,
    FLAG_SEARCH_FORM,
    BrowserDriver,
    DriverError,
    Element,
    ErrorCode,
    PageState,
    Target,
)
from babas.safety import normalize_host, redact
from babas.schemas import SCROLL_DEFAULT_PIXELS, StepBase

log = logging.getLogger("babas.actions")

FIND_TIMEOUT_MS = 2000
FIND_POLL_MS = 250
VIDEO_TIMEOUT_MS = 10000
GOTO_SETTLE_MS = 2000
GMAIL_SETTLE_MS = 3000
WHATSAPP_SETTLE_MS = 5000
COMPOSE_SETTLE_MS = 2000
SEND_SETTLE_MS = 2000
SEARCH_SETTLE_MS = 2000
READ_TEXT_LIMIT = 2000

YOUTUBE_URL = "https://www.youtube.com"
GMAIL_URL = "https://mail.google.com"
WHATSAPP_URL = "https://web.whatsapp.com"

POPUP_SELECTORS: Tuple[str, ...] = (
    'button[aria-label*="Accept"]',
    'button:has-text("Accept")',
    'button:has-text("Aceptar")',
)
YOUTUBE_SEARCH_SELECTORS: Tuple[str, ...] = (
    'input[name="search_query"]',
    "input#search",
    'input[placeholder*="Search"]',
    'input[placeholder*="Buscar"]',
)
YOUTUBE_FIRST_VIDEO_SELECTORS: Tuple[str, ...] = ("ytd-video-renderer a#thumbnail",)
GMAIL_COMPOSE_SELECTORS: Tuple[str, ...] = (
    'div[role="button"]:has-text("Compose")',
    'div[role="button"]:has-text("Redactar")',
)
GMAIL_TO_SELECTORS: Tuple[str, ...] = (
    'input[name="to"]',
    'input[aria-label*="To"]',
    'input[aria-label*="Para"]',
    'input[aria-label*="Destinatarios"]',
)
GMAIL_SUBJECT_SELECTORS: Tuple[str, ...] = ('input[name="subjectbox"]',)
GMAIL_BODY_SELECTORS: Tuple[str, ...] = (
    'div[role="textbox"][aria-label*="Message"]',
    'div[role="textbox"][aria-label*="Cuerpo"]',
    'div[role="textbox"]',
)
GMAIL_SEND_SELECTORS: Tuple[str, ...] = (
    'div[role="button"]:has-text("Send")',
    'div[role="button"]:has-text("Enviar")',
)
WHATSAPP_SEARCH_SELECTORS: Tuple[str, ...] = (
    'div[contenteditable="true"][data-tab="3"]',
    'div[title*="Search"]',
    '[contenteditable="true"][aria-label*="Buscar"]',
    '[contenteditable="true"][aria-label*="Search"]',
)
WHATSAPP_RESULT_SELECTORS: Tuple[str, ...] = ('div[role="listitem"]',)
WHATSAPP_MESSAGE_SELECTORS: Tuple[str, ...] = (
    'div[contenteditable="true"][data-tab="10"]',
    'footer div[contenteditable="true"]',
)
SMART_SEARCH_FALLBACK_SELECTORS: Tuple[str, ...] = (
    "input[type='search']",
    "input[name*='search']",
    "input[name*='query']",
    "input[name*='q']",
    "input[placeholder*='Search']",
    "input[placeholder*='Buscar']",
    "input[aria-label*='Search']",
    "input[aria-label*='Buscar']",
    "[role='searchbox']",
    "[role='combobox']",
    "input[id*='search']",
    "input[class*='search']",
    "textarea[name*='search']",
)

SEARCH_ROLES = frozenset({"searchbox", "textbox", "combobox"})
AGENT_ACTIONS = frozenset({"ask_user", "done"})
FATAL_CODES = frozenset(
    {
        ErrorCode.HALTED,
        ErrorCode.NO_EXTENSION,
        ErrorCode.DOMAIN_NOT_ALLOWED,
        ErrorCode.CONFIRMATION_REQUIRED,
        ErrorCode.PASSWORD_FIELD,
    }
)

Handler = Callable[[Any, Optional[PageState], Optional[str]], Awaitable[Optional["StepOutcome"]]]


@dataclass
class StepOutcome:
    ok: bool = True
    detail: str = ""
    data: Any = None


class ActionExecutor:
    def __init__(
        self,
        driver: BrowserDriver,
        *,
        find_timeout_ms: int = FIND_TIMEOUT_MS,
        video_timeout_ms: int = VIDEO_TIMEOUT_MS,
        screenshot_dir: Optional[str] = None,
    ):
        self.driver = driver
        self.find_timeout_ms = find_timeout_ms
        self.video_timeout_ms = video_timeout_ms
        self.screenshot_dir = screenshot_dir
        self._handlers: Dict[str, Handler] = {
            "goto": self._goto,
            "wait": self._wait,
            "click": self._click,
            "type_text": self._type_text,
            "press_key": self._press_key,
            "screenshot": self._screenshot,
            "scroll": self._scroll,
            "smart_search": self._smart_search,
            "smart_click": self._smart_click,
            "analyze_page": self._analyze_page,
            "new_tab": self._new_tab,
            "switch_tab": self._switch_tab,
            "close_tab": self._close_tab,
            "search_youtube": self._search_youtube,
            "click_first_video": self._click_first_video,
            "open_gmail": self._open_gmail,
            "compose_email": self._compose_email,
            "send_email": self._send_email,
            "open_whatsapp": self._open_whatsapp,
            "search_contact": self._search_contact,
            "send_whatsapp_message": self._send_whatsapp_message,
            "select_option": self._select_option,
            "read_text": self._read_text,
        }

    @property
    def current_tab_name(self) -> str:
        return self.driver.current_tab

    async def execute(
        self,
        step: StepBase,
        state: Optional[PageState] = None,
        *,
        confirmation: Optional[str] = None,
    ) -> StepOutcome:
        action = getattr(step, "action", None)
        if action in AGENT_ACTIONS:
            raise ValueError(f"{action} is handled by the agent, not by the action executor")
        handler = self._handlers.get(action) if isinstance(action, str) else None
        if handler is None:
            raise ValueError(f"Unknown action: {action!r}")
        log.debug("executing %s", action)
        outcome = await handler(step, state, confirmation)
        return outcome if isinstance(outcome, StepOutcome) else StepOutcome()

    async def find_first_visible(
        self,
        selectors: Sequence[str],
        timeout_ms: Optional[int] = None,
        *,
        what: str = "",
    ) -> Target:
        budget = self.find_timeout_ms if timeout_ms is None else timeout_ms
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(budget, 0) / 1000
        while True:
            for selector in selectors:
                target = Target(selector=selector)
                try:
                    if await self.driver.is_visible(target):
                        return target
                except DriverError as exc:
                    if exc.code in FATAL_CODES:
                        raise
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(FIND_POLL_MS / 1000, remaining))
        message = f"Element not found: {what}" if what else "Element not found"
        raise DriverError(ErrorCode.NOT_FOUND, message, {"selectors": list(selectors)})

    @staticmethod
    def _gate(step: StepBase, confirmation: Optional[str]) -> Dict[str, Any]:
        return {
            "confirmation": confirmation,
            "llm_consequential": bool(step.consequential),
            "explanation": step.explanation or "",
        }

    @staticmethod
    def _optional_target(step: Any, state: Optional[PageState]) -> Optional[Target]:
        if getattr(step, "index", None) is not None and state is None:
            raise ValueError(
                f"{step.action} uses element [{step.index}] but there is no current page state: observe the page first"
            )
        return step.to_target(state.snapshot_id if state is not None else None)

    def _target(self, step: Any, state: Optional[PageState]) -> Target:
        target = self._optional_target(step, state)
        if target is None:
            raise ValueError(f"{step.action} needs an index, a selector or a text")
        return target

    async def _confirm_batch(
        self,
        items: Sequence[Tuple[str, Optional[Target], Dict[str, Any]]],
        *,
        force_confirm: bool = False,
        explanation: str = "",
    ) -> Optional[str]:
        batch = getattr(self.driver, "confirm_batch", None)
        if batch is None:
            return None
        return await batch(items, force_confirm=force_confirm, explanation=explanation)

    async def _goto(self, step: Any, state: Optional[PageState], confirmation: Optional[str]) -> None:
        await self.driver.goto(step.value)
        await self.driver.wait(GOTO_SETTLE_MS)

    async def _wait(self, step: Any, state: Optional[PageState], confirmation: Optional[str]) -> None:
        await self.driver.wait(step.ms)

    async def _click(self, step: Any, state: Optional[PageState], confirmation: Optional[str]) -> None:
        await self.driver.click(self._target(step, state), **self._gate(step, confirmation))

    async def _type_text(self, step: Any, state: Optional[PageState], confirmation: Optional[str]) -> None:
        await self.driver.type_text(self._target(step, state), step.value, **self._gate(step, confirmation))

    async def _press_key(self, step: Any, state: Optional[PageState], confirmation: Optional[str]) -> None:
        await self.driver.press_key(step.value, self._optional_target(step, state), **self._gate(step, confirmation))

    async def _select_option(
        self, step: Any, state: Optional[PageState], confirmation: Optional[str]
    ) -> StepOutcome:
        selected = await self.driver.select_option(self._target(step, state), step.value, **self._gate(step, confirmation))
        return StepOutcome(detail=selected if isinstance(selected, str) else "")

    async def _read_text(self, step: Any, state: Optional[PageState], confirmation: Optional[str]) -> StepOutcome:
        text = await self.driver.read_text(self._optional_target(step, state))
        return StepOutcome(detail=(text or "")[:READ_TEXT_LIMIT])

    async def _scroll(self, step: Any, state: Optional[PageState], confirmation: Optional[str]) -> None:
        await self.driver.scroll(step.value, step.ms or SCROLL_DEFAULT_PIXELS)

    async def _screenshot(self, step: Any, state: Optional[PageState], confirmation: Optional[str]) -> StepOutcome:
        data = await self.driver.screenshot()
        name = f"screenshot_{datetime.now().strftime('%H%M%S')}.png"
        path = Path(self.screenshot_dir) / name if self.screenshot_dir else Path(name)
        await asyncio.to_thread(path.write_bytes, data)
        return StepOutcome(detail=str(path))

    async def _analyze_page(self, step: Any, state: Optional[PageState], confirmation: Optional[str]) -> StepOutcome:
        fresh = await self.driver.get_page_state()
        parts = [
            f"URL: {redact(fresh.url)}",
            f"TITLE: {redact(fresh.title)}",
            f"ELEMENTS: {len(fresh.elements)}",
        ]
        if fresh.has_captcha:
            parts.append("CAPTCHA: yes")
        if fresh.has_password:
            parts.append("PASSWORD FIELD: yes")
        return StepOutcome(detail=" | ".join(parts), data=fresh)

    @staticmethod
    def _search_element(state: Optional[PageState]) -> Optional[Element]:
        if state is None:
            return None
        found = [
            el
            for el in state.elements
            if el.has(FLAG_SEARCH_FORM)
            and el.role.lower() in SEARCH_ROLES
            and el.tag.lower() != "select"
            and not el.has(FLAG_DISABLED)
            and not el.has(FLAG_PASSWORD)
        ]
        found.sort(key=lambda el: not el.in_viewport)
        return found[0] if found else None

    async def _smart_search(self, step: Any, state: Optional[PageState], confirmation: Optional[str]) -> None:
        element = self._search_element(state)
        if element is not None and state is not None:
            target = Target(index=element.index, snapshot_id=state.snapshot_id)
        else:
            target = await self.find_first_visible(SMART_SEARCH_FALLBACK_SELECTORS, what="a search box on this page")
        gate = self._gate(step, confirmation)
        await self.driver.type_text(target, step.value, **gate)
        await self.driver.press_key("Enter", target, **gate)

    async def _smart_click(self, step: Any, state: Optional[PageState], confirmation: Optional[str]) -> None:
        await self.driver.click(Target(text=step.value), **self._gate(step, confirmation))

    @staticmethod
    def _tab_name(step: Any) -> Optional[str]:
        name = getattr(step, "tab_name", None) or getattr(step, "value", None) or ""
        return name.strip() or None

    async def _new_tab(self, step: Any, state: Optional[PageState], confirmation: Optional[str]) -> StepOutcome:
        created = await self.driver.new_tab(self._tab_name(step))
        return StepOutcome(detail=created if isinstance(created, str) else "")

    def _require_known_tab(self, name: str) -> None:
        tabs = self.driver.list_tabs()
        if name not in tabs:
            raise DriverError(
                ErrorCode.TAB_NOT_FOUND,
                f"Tab '{name}' not found. Open tabs: {', '.join(tabs) or 'none'}",
                {"tabs": list(tabs)},
            )

    async def _switch_tab(self, step: Any, state: Optional[PageState], confirmation: Optional[str]) -> StepOutcome:
        name = self._tab_name(step)
        if name is None:
            raise ValueError("switch_tab needs a tab name")
        self._require_known_tab(name)
        await self.driver.switch_tab(name)
        return StepOutcome(detail=name)

    async def _close_tab(self, step: Any, state: Optional[PageState], confirmation: Optional[str]) -> StepOutcome:
        name = self._tab_name(step)
        if name is not None:
            self._require_known_tab(name)
        closed = await self.driver.close_tab(name)
        if not closed:
            label = name or self.driver.current_tab
            return StepOutcome(
                ok=False,
                detail=f"The tab '{label}' was not closed: the main tab cannot be closed, switch to another tab first",
            )
        return StepOutcome(detail=name or "")

    async def _dismiss_popups(self, step: Any, confirmation: Optional[str]) -> None:
        gate = self._gate(step, confirmation)
        for selector in POPUP_SELECTORS:
            target = Target(selector=selector)
            try:
                if not await self.driver.is_visible(target):
                    continue
                await self.driver.click(target, **gate)
            except DriverError as exc:
                if exc.code in FATAL_CODES:
                    raise
                log.debug("popup click skipped (%s)", exc.code)
                continue
            break

    async def _ensure_youtube(self) -> None:
        host = normalize_host(await self.driver.current_url())
        if host == "youtube.com" or host.endswith(".youtube.com"):
            return
        await self.driver.goto(YOUTUBE_URL)
        await self.driver.wait(GOTO_SETTLE_MS)

    async def _search_youtube(self, step: Any, state: Optional[PageState], confirmation: Optional[str]) -> None:
        await self._ensure_youtube()
        await self._dismiss_popups(step, confirmation)
        search = await self.find_first_visible(YOUTUBE_SEARCH_SELECTORS, what="the YouTube search box")
        gate = self._gate(step, confirmation)
        await self.driver.type_text(search, step.value, **gate)
        await self.driver.press_key("Enter", search, **gate)
        await self.driver.wait(SEARCH_SETTLE_MS)

    async def _click_first_video(self, step: Any, state: Optional[PageState], confirmation: Optional[str]) -> None:
        video = await self.find_first_visible(
            YOUTUBE_FIRST_VIDEO_SELECTORS,
            self.video_timeout_ms,
            what="a video in the YouTube results (is this a YouTube search results page?)",
        )
        await self.driver.click(video, **self._gate(step, confirmation))

    async def _open_gmail(self, step: Any, state: Optional[PageState], confirmation: Optional[str]) -> None:
        await self.driver.goto(GMAIL_URL)
        await self.driver.wait(GMAIL_SETTLE_MS)

    async def _compose_email(self, step: Any, state: Optional[PageState], confirmation: Optional[str]) -> None:
        gate = self._gate(step, confirmation)
        compose = await self.find_first_visible(GMAIL_COMPOSE_SELECTORS, what="the Gmail Compose button (is Gmail open?)")
        await self.driver.click(compose, **gate)
        await self.driver.wait(COMPOSE_SETTLE_MS)
        fields: List[Tuple[Target, str]] = [
            (await self.find_first_visible(GMAIL_TO_SELECTORS, what="the Gmail recipient field"), step.to)
        ]
        if step.subject:
            fields.append(
                (await self.find_first_visible(GMAIL_SUBJECT_SELECTORS, what="the Gmail subject field"), step.subject)
            )
        if step.body:
            fields.append((await self.find_first_visible(GMAIL_BODY_SELECTORS, what="the Gmail message body"), step.body))
        batch_id = await self._confirm_batch(
            [("type", target, {"text": text}) for target, text in fields],
            explanation=step.explanation or "",
        )
        gate["confirmation"] = batch_id if batch_id is not None else confirmation
        for target, text in fields:
            await self.driver.type_text(target, text, **gate)

    async def _send_email(self, step: Any, state: Optional[PageState], confirmation: Optional[str]) -> None:
        send = await self.find_first_visible(GMAIL_SEND_SELECTORS, what="the Gmail Send button (is a message open?)")
        await self.driver.click(send, force_confirm=True, **self._gate(step, confirmation))
        await self.driver.wait(SEND_SETTLE_MS)

    async def _open_whatsapp(self, step: Any, state: Optional[PageState], confirmation: Optional[str]) -> None:
        await self.driver.goto(WHATSAPP_URL)
        await self.driver.wait(WHATSAPP_SETTLE_MS)

    async def _search_contact(self, step: Any, state: Optional[PageState], confirmation: Optional[str]) -> None:
        gate = self._gate(step, confirmation)
        search = await self.find_first_visible(
            WHATSAPP_SEARCH_SELECTORS, what="the WhatsApp search box (is WhatsApp Web open and linked?)"
        )
        await self.driver.type_text(search, step.value, **gate)
        await self.driver.wait(SEARCH_SETTLE_MS)
        result = await self.find_first_visible(WHATSAPP_RESULT_SELECTORS, what="a WhatsApp chat in the search results")
        await self.driver.click(result, **gate)

    async def _send_whatsapp_message(
        self, step: Any, state: Optional[PageState], confirmation: Optional[str]
    ) -> None:
        box = await self.find_first_visible(
            WHATSAPP_MESSAGE_SELECTORS, what="the WhatsApp message box (is a chat open?)"
        )
        explanation = step.explanation or ""
        batch_id = await self._confirm_batch(
            [("type", box, {"text": step.body}), ("press", box, {"key": "Enter"})],
            force_confirm=True,
            explanation=explanation,
        )
        gate = self._gate(step, confirmation)
        gate["confirmation"] = batch_id if batch_id is not None else confirmation
        await self.driver.type_text(box, step.body, force_confirm=True, **gate)
        await self.driver.press_key("Enter", box, force_confirm=True, **gate)
