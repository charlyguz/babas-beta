import asyncio
from abc import ABC, abstractmethod
from typing import Callable, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

PROMPT_LABEL_CHARS = 80
PROMPT_HREF_CHARS = 120
PROMPT_URL_CHARS = 300
PROMPT_OPTIONS_SHOWN = 10
_PROMPT_TYPES = frozenset({"email", "tel", "search", "number", "date", "url"})
_VALUE_ROLES = frozenset({"textbox", "searchbox", "combobox", "spinbutton", "slider", "listbox"})
_VALUE_TAGS = frozenset({"input", "textarea", "select"})
_CLICK_ROLES = frozenset({"button", "link"})

FLAG_SUBMIT = "submit"
FLAG_SEARCH_FORM = "search_form"
FLAG_CONSEQUENTIAL = "consequential"
FLAG_PASSWORD = "password"
FLAG_PII = "pii"
FLAG_PAYMENT = "payment"
FLAG_CAPTCHA = "captcha"
FLAG_DISABLED = "disabled"


class ErrorCode:
    STALE_SNAPSHOT = "STALE_SNAPSHOT"
    NOT_FOUND = "NOT_FOUND"
    NOT_VISIBLE = "NOT_VISIBLE"
    OBSCURED = "OBSCURED"
    DISABLED = "DISABLED"
    NOT_EDITABLE = "NOT_EDITABLE"
    PASSWORD_FIELD = "PASSWORD_FIELD"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    DOMAIN_NOT_ALLOWED = "DOMAIN_NOT_ALLOWED"
    HALTED = "HALTED"
    TIMEOUT = "TIMEOUT"
    NO_EXTENSION = "NO_EXTENSION"
    TAB_NOT_FOUND = "TAB_NOT_FOUND"
    UNSCRIPTABLE = "UNSCRIPTABLE"
    NAVIGATION_FAILED = "NAVIGATION_FAILED"
    BAD_REQUEST = "BAD_REQUEST"
    UNSUPPORTED = "UNSUPPORTED"
    INTERNAL = "INTERNAL"


class DriverError(Exception):
    def __init__(self, code: str, message: str = "", data: Optional[dict] = None):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message
        self.data = data or {}


class Target(BaseModel):
    index: Optional[int] = None
    selector: Optional[str] = None
    text: Optional[str] = None
    snapshot_id: Optional[str] = None

    @model_validator(mode="after")
    def _one_locator(self):
        if self.index is None and not self.selector and not self.text:
            raise ValueError("target needs an index, a selector or a text")
        return self

    def describe(self) -> str:
        if self.index is not None:
            return f"[{self.index}]"
        if self.selector:
            return self.selector
        return f"text={self.text!r}"


class Element(BaseModel):
    index: int
    tag: str
    role: str
    label: str = ""
    text: str = ""
    type: Optional[str] = None
    value: Optional[str] = None
    href: Optional[str] = None
    options: Optional[List[str]] = None
    checked: Optional[bool] = None
    flags: List[str] = Field(default_factory=list)
    in_viewport: bool = True

    def has(self, flag: str) -> bool:
        return flag in self.flags


def clip_text(text: Optional[str], limit: int) -> str:
    flat = " ".join((text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[: max(limit - 1, 0)].rstrip() + "…"


def _quoted(text: str) -> str:
    return '"' + text.replace('"', "'") + '"'


def _prompt_line(el: Element, redact: Callable[[str], str]) -> str:
    role = el.role or el.tag
    kind = (el.type or "").lower()
    if kind in _PROMPT_TYPES and not (role == "searchbox" and kind == "search"):
        role = f"{role}({kind})"
    parts = [f"[{el.index}]", role]
    label = clip_text(el.label or el.text, PROMPT_LABEL_CHARS)
    if label:
        parts.append(_quoted(label))
    if el.checked is not None:
        parts.append("(checked)" if el.checked else "(unchecked)")
    if el.flags:
        parts.append("[" + ", ".join(el.flags) + "]")
    if el.has(FLAG_PASSWORD) or kind == "password":
        parts.append("= (hidden)")
    elif el.checked is None and (
        el.role in _VALUE_ROLES or (el.tag in _VALUE_TAGS and el.role not in _CLICK_ROLES)
    ):
        parts.append("= " + _quoted(clip_text(redact(el.value or ""), PROMPT_LABEL_CHARS)))
    if el.options and not el.has(FLAG_PASSWORD):
        shown = [clip_text(option, PROMPT_LABEL_CHARS) for option in el.options[:PROMPT_OPTIONS_SHOWN]]
        hidden = len(el.options) - len(shown)
        parts.append("options: " + " | ".join(shown) + (f" (+{hidden} more)" if hidden > 0 else ""))
    if el.href:
        parts.append("-> " + clip_text(el.href, PROMPT_HREF_CHARS))
    return " ".join(parts)


def _page_excerpt(text: str, limit: int) -> str:
    lines = [" ".join(line.split()) for line in (text or "").splitlines()]
    joined = "\n".join(line for line in lines if line)
    if not joined:
        return "(empty)"
    if len(joined) <= limit:
        return joined
    return joined[: max(limit - 1, 0)].rstrip() + "…"


class PageState(BaseModel):
    snapshot_id: str
    url: str
    title: str = ""
    elements: List[Element] = Field(default_factory=list)
    text: str = ""
    has_captcha: bool = False
    has_password: bool = False
    truncated: bool = False
    tab: Optional[str] = None

    def element(self, index: int) -> Optional[Element]:
        for el in self.elements:
            if el.index == index:
                return el
        return None

    def to_prompt(self, max_elements: int = 150, max_text: int = 1500) -> str:
        # Lazy import: babas.safety imports this module.
        from babas.safety import redact

        limit = max(max_elements, 0)
        if len(self.elements) > limit:
            ordered = [el for el in self.elements if el.in_viewport] + [el for el in self.elements if not el.in_viewport]
            shown = ordered[:limit]
        else:
            shown = list(self.elements)
        header = (
            f"URL: {clip_text(self.url, PROMPT_URL_CHARS)} | TITLE: {clip_text(self.title, PROMPT_LABEL_CHARS)}"
            f" | CAPTCHA: {'yes' if self.has_captcha else 'no'}"
            f" | PASSWORD FIELD: {'yes' if self.has_password else 'no'}"
        )
        lines = [header, 'ELEMENTS (index role "label" [flags] = value):']
        if not self.elements:
            lines.append("(none)")
        lines.extend(_prompt_line(el, redact) for el in shown)
        omitted = len(self.elements) - len(shown)
        if omitted > 0:
            lines.append(f"… ({omitted} more not shown)")
        elif self.truncated:
            lines.append("… (the page has more elements than were captured)")
        lines.append(f"PAGE TEXT: {_page_excerpt(self.text, max(max_text, 1))}")
        return "\n".join(lines)


ScrollDirection = Literal["up", "down"]


class BrowserDriver(ABC):
    name = "base"

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    @abstractmethod
    async def goto(self, url: str) -> None: ...

    @abstractmethod
    async def current_url(self) -> str: ...

    @abstractmethod
    async def get_page_state(self, highlight: bool = False) -> PageState: ...

    @abstractmethod
    async def describe(self, target: Target) -> Element: ...

    @abstractmethod
    async def is_visible(self, target: Target) -> bool: ...

    @abstractmethod
    async def click(self, target: Target, *, confirmation: Optional[str] = None) -> None: ...

    @abstractmethod
    async def type_text(self, target: Target, text: str, *, confirmation: Optional[str] = None) -> None: ...

    @abstractmethod
    async def press_key(self, key: str, target: Optional[Target] = None, *, confirmation: Optional[str] = None) -> None: ...

    @abstractmethod
    async def select_option(self, target: Target, value: str, *, confirmation: Optional[str] = None) -> str: ...

    @abstractmethod
    async def scroll(self, direction: ScrollDirection = "down", amount: int = 500) -> None: ...

    @abstractmethod
    async def read_text(self, target: Optional[Target] = None) -> str: ...

    @abstractmethod
    async def screenshot(self) -> bytes: ...

    @abstractmethod
    async def highlight(self, mode: Literal["all", "off"] = "all") -> None: ...

    @abstractmethod
    async def new_tab(self, name: Optional[str] = None, url: Optional[str] = None) -> str: ...

    @abstractmethod
    async def switch_tab(self, name: str) -> None: ...

    @abstractmethod
    async def close_tab(self, name: Optional[str] = None) -> bool: ...

    @abstractmethod
    def list_tabs(self) -> List[str]: ...

    @property
    @abstractmethod
    def current_tab(self) -> str: ...

    async def wait(self, ms: int) -> None:
        await asyncio.sleep(max(ms, 0) / 1000)
