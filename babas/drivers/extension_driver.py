import base64
import binascii
import logging
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import ValidationError

from babas.bridge.server import BridgeServer
from babas.drivers.base import (
    BrowserDriver,
    DriverError,
    Element,
    ErrorCode,
    PageState,
    ScrollDirection,
    Target,
)

log = logging.getLogger("babas.extension_driver")

DEFAULT_TIMEOUT = 30.0
GOTO_TIMEOUT = 60.0
SCREENSHOT_TIMEOUT = 20.0
MAIN_TAB = "main"


def _target_payload(target: Target) -> Dict[str, Any]:
    return target.model_dump(exclude_none=True)


def act_payload(
    op: str,
    target: Optional[Union[Target, Dict[str, Any]]] = None,
    *,
    text: Optional[str] = None,
    key: Optional[str] = None,
    value: Optional[str] = None,
    **extra: Any,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {"op": op}
    if target is not None:
        body["target"] = _target_payload(target) if isinstance(target, Target) else dict(target)
    for name, item in (("text", text), ("key", key), ("value", value)):
        if item is not None:
            body[name] = item
    body.update({name: item for name, item in extra.items() if item is not None})
    return body


def _field(payload: Dict[str, Any], key: str, kind: type) -> Any:
    value = payload.get(key)
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise DriverError(ErrorCode.INTERNAL, f"The extension answered without a valid '{key}'")
    return value


class ExtensionDriver(BrowserDriver):
    name = "extension"

    def __init__(self, bridge: BridgeServer):
        self.bridge = bridge
        self._tabs: Dict[str, int] = {}
        self._current = MAIN_TAB
        self._counter = 0

    async def start(self) -> None:
        info = await self.bridge.request("tab.info", timeout=DEFAULT_TIMEOUT)
        self._tabs = {MAIN_TAB: _field(info, "tab_id", int)}
        self._current = MAIN_TAB

    async def goto(self, url: str) -> None:
        await self.bridge.request("tab.goto", {"url": url}, timeout=GOTO_TIMEOUT)

    async def current_url(self) -> str:
        info = await self.bridge.request("tab.info", timeout=DEFAULT_TIMEOUT)
        return _field(info, "url", str)

    async def get_page_state(self, highlight: bool = False) -> PageState:
        payload = await self.bridge.request(
            "page.state",
            {"highlight": highlight, "max_elements": 250, "max_text": 2000},
            timeout=DEFAULT_TIMEOUT,
        )
        try:
            state = PageState.model_validate(payload)
        except ValidationError as exc:
            raise DriverError(ErrorCode.INTERNAL, f"The extension sent an invalid page state: {exc}") from None
        state.tab = self.current_tab
        return state

    async def describe(self, target: Target) -> Element:
        payload = await self.bridge.request("page.describe", {"target": _target_payload(target)}, timeout=DEFAULT_TIMEOUT)
        try:
            return Element.model_validate(payload.get("element"))
        except ValidationError as exc:
            raise DriverError(ErrorCode.INTERNAL, f"The extension sent an invalid element: {exc}") from None

    async def is_visible(self, target: Target) -> bool:
        payload = await self.bridge.request("page.exists", {"target": _target_payload(target)}, timeout=DEFAULT_TIMEOUT)
        return payload.get("visible") is True

    async def _act(
        self,
        op: str,
        target: Optional[Target] = None,
        *,
        confirmation: Optional[str] = None,
        text: Optional[str] = None,
        key: Optional[str] = None,
        value: Optional[str] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        body = act_payload(op, target, text=text, key=key, value=value, confirmation_id=confirmation or None, **extra)
        payload = await self.bridge.request("page.act", body, timeout=DEFAULT_TIMEOUT)
        result = payload.get("result")
        return result if isinstance(result, dict) else {}

    async def click(self, target: Target, *, confirmation: Optional[str] = None) -> None:
        await self._act("click", target, confirmation=confirmation)

    async def type_text(self, target: Target, text: str, *, confirmation: Optional[str] = None) -> None:
        await self._act("type", target, confirmation=confirmation, text=text)

    async def press_key(self, key: str, target: Optional[Target] = None, *, confirmation: Optional[str] = None) -> None:
        await self._act("press", target, confirmation=confirmation, key=key)

    async def select_option(self, target: Target, value: str, *, confirmation: Optional[str] = None) -> str:
        result = await self._act("select", target, confirmation=confirmation, value=value)
        return _field(result, "selected", str)

    async def scroll(self, direction: ScrollDirection = "down", amount: int = 500) -> None:
        await self._act("scroll", direction=direction, amount=amount)

    async def read_text(self, target: Optional[Target] = None) -> str:
        result = await self._act("read", target)
        return _field(result, "text", str)

    async def screenshot(self) -> bytes:
        payload = await self.bridge.request("tab.screenshot", timeout=SCREENSHOT_TIMEOUT)
        encoded = _field(payload, "png_base64", str)
        try:
            return base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise DriverError(ErrorCode.INTERNAL, "The extension sent an invalid screenshot") from None

    async def highlight(self, mode: Literal["all", "off"] = "all") -> None:
        await self._act("highlight", mode=mode)

    def _next_tab_name(self) -> str:
        while True:
            self._counter += 1
            name = f"tab_{self._counter}"
            if name not in self._tabs:
                return name

    async def new_tab(self, name: Optional[str] = None, url: Optional[str] = None) -> str:
        if name is not None and name in self._tabs:
            raise DriverError(ErrorCode.BAD_REQUEST, f"A tab named '{name}' already exists")
        tab_name = name or self._next_tab_name()
        payload = await self.bridge.request(
            "tab.new",
            {"url": url} if url else {},
            timeout=GOTO_TIMEOUT if url else DEFAULT_TIMEOUT,
        )
        self._tabs[tab_name] = _field(payload, "tab_id", int)
        self._current = tab_name
        return tab_name

    async def switch_tab(self, name: str) -> None:
        tab_id = self._tabs.get(name)
        if tab_id is None:
            raise DriverError(ErrorCode.TAB_NOT_FOUND, f"Tab '{name}' not found")
        try:
            await self.bridge.request("tab.switch", {"tab_id": tab_id}, timeout=DEFAULT_TIMEOUT)
        except DriverError as exc:
            if exc.code == ErrorCode.TAB_NOT_FOUND and name != MAIN_TAB:
                self._tabs.pop(name, None)
            raise
        self._current = name

    async def close_tab(self, name: Optional[str] = None) -> bool:
        tab_name = name or self._current
        tab_id = self._tabs.get(tab_name)
        if tab_name == MAIN_TAB or tab_id is None:
            return False
        try:
            await self.bridge.request("tab.close", {"tab_id": tab_id}, timeout=DEFAULT_TIMEOUT)
        except DriverError as exc:
            if exc.code != ErrorCode.TAB_NOT_FOUND:
                raise
        del self._tabs[tab_name]
        if self._current == tab_name:
            self._current = MAIN_TAB
            main_id = self._tabs.get(MAIN_TAB)
            if main_id is not None:
                try:
                    await self.bridge.request("tab.switch", {"tab_id": main_id}, timeout=DEFAULT_TIMEOUT)
                except DriverError as exc:
                    if exc.code != ErrorCode.TAB_NOT_FOUND:
                        raise
                    log.warning("main tab is gone; the extension keeps working on its active tab")
        return True

    def list_tabs(self) -> List[str]:
        return list(self._tabs)

    @property
    def current_tab(self) -> str:
        return self._current
