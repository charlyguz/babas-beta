import asyncio
from abc import ABC, abstractmethod
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

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
