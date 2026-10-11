import asyncio
import copy
import inspect
import unicodedata
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple, Union

from babas.channels import ConfirmRequest, ConfirmResult
from babas.drivers.base import (
    BrowserDriver,
    DriverError,
    Element,
    ErrorCode,
    PageState,
    ScrollDirection,
    Target,
)

DONE_PLAN: Dict[str, Any] = {"steps": [{"action": "done", "value": "Listo."}]}
PNG_STUB = b"\x89PNG\r\n\x1a\nfake"
DEFAULT_URL = "https://www.gob.mx/"


def _fold(text: str) -> str:
    stripped = unicodedata.normalize("NFD", text or "")
    return " ".join("".join(ch for ch in stripped if not unicodedata.combining(ch)).lower().split())


def make_element(
    index: int,
    role: str = "button",
    label: str = "",
    *,
    tag: Optional[str] = None,
    flags: Sequence[str] = (),
    **fields: Any,
) -> Element:
    defaults_by_role = {"link": "a", "button": "button", "textbox": "input", "searchbox": "input", "combobox": "select"}
    return Element(
        index=index,
        tag=tag or defaults_by_role.get(role, "div"),
        role=role,
        label=label,
        flags=list(flags),
        **fields,
    )


def make_state(*elements: Element, url: str = DEFAULT_URL, snapshot_id: str = "s1", **fields: Any) -> PageState:
    return PageState(snapshot_id=snapshot_id, url=url, elements=list(elements), **fields)


class FakeLLM:
    def __init__(self, responses: Sequence[Any] = (), delay: float = 0.0):
        self.responses: List[Any] = list(responses)
        self.delay = delay
        self.calls: List[Tuple[str, str]] = []
        self.schemas: List[Optional[dict]] = []

    def push(self, *responses: Any) -> None:
        self.responses.extend(responses)

    async def complete_json(self, system: str, user: str, schema: Optional[dict] = None) -> dict:
        await asyncio.sleep(self.delay)
        self.calls.append((system, user))
        self.schemas.append(schema)
        if not self.responses:
            return copy.deepcopy(DONE_PLAN)
        response = self.responses.pop(0)
        if isinstance(response, type) and issubclass(response, BaseException):
            raise response()
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            response = response(system, user)
            if inspect.isawaitable(response):
                response = await response
        if isinstance(response, str):
            from babas.llm import extract_json

            return extract_json(response)
        return copy.deepcopy(response)


class FakeDriver(BrowserDriver):
    """Calls are recorded as (method, args, kwargs) in a normalized form: positional-or-keyword
    parameters with their defaults applied go to args, keyword-only ones (confirmation=...) to kwargs."""

    name = "fake"

    def __init__(
        self,
        states: Union[None, PageState, Sequence[PageState]] = None,
        *,
        selectors: Optional[Dict[str, Element]] = None,
        errors: Optional[Dict[str, Union[Exception, Sequence[Exception]]]] = None,
        url: Optional[str] = None,
        text: str = "",
        delay: float = 0.0,
    ):
        if states is None:
            states = [make_state()]
        elif isinstance(states, PageState):
            states = [states]
        self.states: List[PageState] = list(states) or [make_state()]
        self.selectors: Dict[str, Element] = dict(selectors or {})
        self.errors: Dict[str, List[Exception]] = {}
        for method, error in (errors or {}).items():
            self.errors[method] = [error] if isinstance(error, BaseException) else list(error)
        self.url: str = url if url is not None else self.states[0].url
        self.text = text
        self.delay = delay
        self.calls: List[Tuple[str, tuple, dict]] = []
        self._served = -1
        self._tab_names: List[str] = ["main"]
        self._current = "main"
        self._tab_counter = 0

    def inject(self, method: str, *errors: Exception) -> None:
        self.errors.setdefault(method, []).extend(errors)

    def calls_of(self, method: str) -> List[Tuple[tuple, dict]]:
        return [(args, kwargs) for name, args, kwargs in self.calls if name == method]

    @property
    def current_state(self) -> PageState:
        return self.states[min(max(self._served, 0), len(self.states) - 1)]

    async def _enter(self, method: str, args: tuple = (), kwargs: Optional[dict] = None) -> None:
        self.calls.append((method, args, dict(kwargs or {})))
        if self.delay:
            await asyncio.sleep(self.delay)
        queue = self.errors.get(method)
        if queue:
            raise queue.pop(0)

    def _resolve(self, target: Target) -> Element:
        if target.index is not None:
            found = self.current_state.element(target.index)
        elif target.selector:
            found = self.selectors.get(target.selector)
        else:
            wanted = _fold(target.text or "")
            found = next((el for el in self.current_state.elements if _fold(el.label) == wanted), None)
        if found is None:
            raise DriverError(ErrorCode.NOT_FOUND, f"No element matches {target.describe()}")
        return found.model_copy(deep=True)

    async def start(self) -> None:
        await self._enter("start")

    async def close(self) -> None:
        await self._enter("close")

    async def goto(self, url: str) -> None:
        await self._enter("goto", (url,))
        self.url = url

    async def current_url(self) -> str:
        await self._enter("current_url")
        return self.url

    async def get_page_state(self, highlight: bool = False) -> PageState:
        await self._enter("get_page_state", (highlight,))
        self._served = min(self._served + 1, len(self.states) - 1)
        state = self.states[self._served].model_copy(deep=True)
        state.tab = self.current_tab
        return state

    async def describe(self, target: Target) -> Element:
        await self._enter("describe", (target,))
        return self._resolve(target)

    async def is_visible(self, target: Target) -> bool:
        await self._enter("is_visible", (target,))
        try:
            self._resolve(target)
        except DriverError:
            return False
        return True

    async def click(self, target: Target, *, confirmation: Optional[str] = None, **extra: Any) -> None:
        await self._enter("click", (target,), {"confirmation": confirmation, **extra})

    async def type_text(self, target: Target, text: str, *, confirmation: Optional[str] = None, **extra: Any) -> None:
        await self._enter("type_text", (target, text), {"confirmation": confirmation, **extra})

    async def press_key(
        self, key: str, target: Optional[Target] = None, *, confirmation: Optional[str] = None, **extra: Any
    ) -> None:
        await self._enter("press_key", (key, target), {"confirmation": confirmation, **extra})

    async def select_option(self, target: Target, value: str, *, confirmation: Optional[str] = None, **extra: Any) -> str:
        await self._enter("select_option", (target, value), {"confirmation": confirmation, **extra})
        return value

    async def scroll(self, direction: ScrollDirection = "down", amount: int = 500) -> None:
        await self._enter("scroll", (direction, amount))

    async def read_text(self, target: Optional[Target] = None) -> str:
        await self._enter("read_text", (target,))
        return self.text

    async def screenshot(self) -> bytes:
        await self._enter("screenshot")
        return PNG_STUB

    async def highlight(self, mode: Literal["all", "off"] = "all") -> None:
        await self._enter("highlight", (mode,))

    async def new_tab(self, name: Optional[str] = None, url: Optional[str] = None) -> str:
        await self._enter("new_tab", (name, url))
        if name is None:
            self._tab_counter += 1
            name = f"tab_{self._tab_counter}"
        if name not in self._tab_names:
            self._tab_names.append(name)
        self._current = name
        if url:
            self.url = url
        return name

    async def switch_tab(self, name: str) -> None:
        await self._enter("switch_tab", (name,))
        if name not in self._tab_names:
            raise DriverError(ErrorCode.TAB_NOT_FOUND, f"Tab '{name}' not found")
        self._current = name

    async def close_tab(self, name: Optional[str] = None) -> bool:
        await self._enter("close_tab", (name,))
        target = name or self._current
        if target == "main" or target not in self._tab_names:
            return False
        self._tab_names.remove(target)
        if self._current == target:
            self._current = "main"
        return True

    def list_tabs(self) -> List[str]:
        return list(self._tab_names)

    @property
    def current_tab(self) -> str:
        return self._current

    async def wait(self, ms: int) -> None:
        await self._enter("wait", (ms,))


Setting = Any


def _next_setting(setting: Setting, *args: Any) -> Any:
    if callable(setting):
        return setting(*args)
    if isinstance(setting, list):
        return setting.pop(0) if len(setting) > 1 else setting[0]
    return setting


class RecordingChannel:
    """approve, answer and allow accept a value, a list (consumed in order, the last one repeats) or a callable.
    Set mint_ids to behave like the extension (a fresh confirmation id per approval) or confirmation_id for a fixed one."""

    def __init__(
        self,
        approve: Setting = True,
        answer: Setting = "ok",
        allow: Setting = True,
        delay: float = 0.0,
        *,
        confirmation_id: Optional[str] = None,
        mint_ids: bool = False,
        confirm_error: Optional[BaseException] = None,
        deny_reason: str = "denied",
    ):
        self.approve = approve if not isinstance(approve, (list, tuple)) else list(approve)
        self.answer = answer if not isinstance(answer, (list, tuple)) else list(answer)
        self.allow = allow if not isinstance(allow, (list, tuple)) else list(allow)
        self.delay = delay
        self.confirmation_id = confirmation_id
        self.mint_ids = mint_ids
        self.confirm_error = confirm_error
        self.deny_reason = deny_reason
        self.calls: List[Tuple[str, tuple]] = []
        self.confirms: List[ConfirmRequest] = []
        self.asks: List[Tuple[str, str]] = []
        self.allows: List[Tuple[str, str]] = []
        self.logs: List[Tuple[str, str, Optional[bool]]] = []
        self.states: List[Tuple[str, str]] = []
        self.issued_ids: List[str] = []

    def calls_of(self, name: str) -> List[tuple]:
        return [args for method, args in self.calls if method == name]

    async def confirm(self, req: ConfirmRequest) -> ConfirmResult:
        self.calls.append(("confirm", (req,)))
        self.confirms.append(req)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.confirm_error is not None:
            raise self.confirm_error
        if not _next_setting(self.approve, req):
            return ConfirmResult(False, None, self.deny_reason)
        confirmation_id = self.confirmation_id
        if self.mint_ids:
            confirmation_id = f"conf-{len(self.issued_ids) + 1}"
        if confirmation_id is not None:
            self.issued_ids.append(confirmation_id)
        return ConfirmResult(True, confirmation_id)

    async def ask(self, question: str, kind: str = "text") -> Optional[str]:
        self.calls.append(("ask", (question, kind)))
        self.asks.append((question, kind))
        if self.delay:
            await asyncio.sleep(self.delay)
        return _next_setting(self.answer, question, kind)

    async def allow_site(self, host: str, url: str) -> bool:
        self.calls.append(("allow_site", (host, url)))
        self.allows.append((host, url))
        if self.delay:
            await asyncio.sleep(self.delay)
        return bool(_next_setting(self.allow, host, url))

    async def log(self, kind: str, text: str, ok: Optional[bool] = None) -> None:
        self.calls.append(("log", (kind, text, ok)))
        self.logs.append((kind, text, ok))

    async def run_state(self, state: str, summary: str = "") -> None:
        self.calls.append(("run_state", (state, summary)))
        self.states.append((state, summary))


class FakeBridge:
    """Stands in for BridgeServer.request. responses maps a request type to a dict, an Exception or a callable(payload)."""

    def __init__(self, responses: Optional[Dict[str, Any]] = None):
        self.responses: Dict[str, Any] = dict(responses or {})
        self.requests: List[Tuple[str, Dict[str, Any], float]] = []

    def requests_of(self, type: str) -> List[Tuple[Dict[str, Any], float]]:
        return [(payload, timeout) for kind, payload, timeout in self.requests if kind == type]

    async def request(self, type: str, payload: Optional[Dict[str, Any]] = None, timeout: float = 30.0) -> Dict[str, Any]:
        self.requests.append((type, copy.deepcopy(payload or {}), timeout))
        response = self.responses.get(type, {})
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            response = response(payload or {})
            if inspect.isawaitable(response):
                response = await response
        return response
