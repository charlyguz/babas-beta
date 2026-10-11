import json
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional, Sequence, Tuple, TypeVar

from babas.channels import ConfirmRequest, UserChannel
from babas.drivers.base import (
    BrowserDriver,
    DriverError,
    Element,
    ErrorCode,
    PageState,
    ScrollDirection,
    Target,
)
from babas.drivers.extension_driver import act_payload
from babas.safety import (
    HANDOFF_TEXTS,
    Decision,
    SafetyPolicy,
    describe_action_es,
    is_web_address,
    normalize_host,
)

log = logging.getLogger("babas.gate")

T = TypeVar("T")

APPROVAL_TTL_S = 120.0
FOCUS_TARGET_SELECTOR = ":focus"

BatchItem = Tuple[str, Optional[Target], Dict[str, Any]]


class ActionDenied(Exception):
    def __init__(self, reason: str = "denied", detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class HandoffRequired(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


def _handoff(reason: str) -> HandoffRequired:
    return HandoffRequired(reason, HANDOFF_TEXTS.get(reason, HANDOFF_TEXTS["password_field"]))


def _digest(payload: Dict[str, Any]) -> str:
    body = {key: value for key, value in payload.items() if key != "confirmation_id"}
    return json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class _Approvals:
    # Remembers what the user approved so an id-less (console) or batch approval is not asked again for the same action.
    def __init__(self, ttl: float = APPROVAL_TTL_S):
        self._ttl = ttl
        self._entries: Dict[str, Tuple[float, Dict[str, int]]] = {}

    def _prune(self) -> None:
        now = time.monotonic()
        for key in [key for key, (expires, _) in self._entries.items() if expires <= now]:
            del self._entries[key]

    def grant(self, confirmation_id: Optional[str], payloads: Sequence[Dict[str, Any]]) -> None:
        self._prune()
        key = confirmation_id or ""
        _, counts = self._entries.get(key, (0.0, {}))
        for payload in payloads:
            digest = _digest(payload)
            counts[digest] = counts.get(digest, 0) + 1
        self._entries[key] = (time.monotonic() + self._ttl, counts)

    def consume(self, confirmation_id: Optional[str], payload: Dict[str, Any]) -> bool:
        self._prune()
        entry = self._entries.get(confirmation_id or "")
        if entry is None:
            return False
        counts = entry[1]
        digest = _digest(payload)
        if counts.get(digest, 0) <= 0:
            return False
        counts[digest] -= 1
        return True


def _unique(items: Sequence[str]) -> List[str]:
    out: List[str] = []
    for item in items:
        if item not in out:
            out.append(item)
    return out


class SafeDriver(BrowserDriver):
    def __init__(self, inner: BrowserDriver, policy: SafetyPolicy, channel: UserChannel):
        self.inner = inner
        self.policy = policy
        self.channel = channel
        self.name = inner.name
        self._approvals = _Approvals()

    async def start(self) -> None:
        await self.inner.start()

    async def close(self) -> None:
        await self.inner.close()

    async def _call(self, call: Callable[[], Awaitable[T]]) -> T:
        try:
            return await call()
        except DriverError as exc:
            if exc.code != ErrorCode.DOMAIN_NOT_ALLOWED:
                raise
            host, url = self._site_of(exc)
            if not host:
                raise
            await self._request_site(host, url)
        return await call()

    @staticmethod
    def _site_of(exc: DriverError) -> Tuple[str, str]:
        raw_url = exc.data.get("url")
        url = raw_url if isinstance(raw_url, str) else ""
        raw_host = exc.data.get("host")
        host = normalize_host(raw_host) if isinstance(raw_host, str) and raw_host else normalize_host(url)
        return host, url or (f"https://{host}/" if host else "")

    async def _request_site(self, host: str, url: str) -> None:
        try:
            approved = await self.channel.allow_site(host, url)
        except Exception as exc:
            log.warning("site permission channel failed: %s", type(exc).__name__)
            approved = False
        if approved is not True:
            raise ActionDenied("site", host)
        self.policy.approve_domain(host)

    async def _ensure_site(self, url: str) -> None:
        if self.policy.domain_allowed(url):
            return
        host = normalize_host(url)
        if not host:
            raise ActionDenied("site", "invalid address")
        await self._request_site(host, url)

    @staticmethod
    def _require_web_url(url: str) -> None:
        if not is_web_address(url) and url.strip().lower() != "about:blank":
            raise DriverError(ErrorCode.BAD_REQUEST, "Only http and https addresses can be opened")

    async def _host(self) -> str:
        try:
            return normalize_host(await self._call(self.inner.current_url))
        except DriverError:
            return ""

    async def _element_for(self, target: Optional[Target]) -> Optional[Element]:
        if target is None:
            try:
                return await self._call(lambda: self.inner.describe(Target(selector=FOCUS_TARGET_SELECTOR)))
            except DriverError:
                return None
        return await self._call(lambda: self.inner.describe(target))

    async def _ask_confirm(
        self, actions: List[Dict[str, Any]], reasons: Sequence[str], explanation: str, lines: List[str]
    ) -> Optional[str]:
        request = ConfirmRequest(actions=actions, reasons=list(reasons), explanation=explanation or "", lines=lines)
        try:
            result = await self.channel.confirm(request)
        except Exception as exc:
            log.warning("confirmation channel failed: %s", type(exc).__name__)
            raise ActionDenied("denied", type(exc).__name__) from None
        if getattr(result, "approved", False) is not True:
            reason = str(getattr(result, "reason", "") or "")
            raise ActionDenied("no_ui" if reason == "no_ui" else "denied", reason)
        confirmation_id = getattr(result, "confirmation_id", None)
        return confirmation_id if isinstance(confirmation_id, str) and confirmation_id else None

    async def _gated(
        self,
        op: Literal["click", "type", "select", "press"],
        target: Optional[Target],
        invoke: Callable[[Optional[str]], Awaitable[T]],
        *,
        text: Optional[str] = None,
        value: Optional[str] = None,
        key: Optional[str] = None,
        confirmation: Optional[str] = None,
        force_confirm: bool = False,
        llm_consequential: bool = False,
        explanation: str = "",
    ) -> T:
        element = await self._element_for(target)
        decision = self.policy.evaluate(
            op,
            element,
            value=text if text is not None else value,
            key=key,
            force_confirm=force_confirm,
            llm_consequential=llm_consequential,
        )
        if decision.kind == "handoff":
            raise _handoff(decision.reasons[0])
        payload = act_payload(op, target, text=text, key=key, value=value)

        async def ask(reasons: Sequence[str]) -> Optional[str]:
            line = describe_action_es(op, element, text if text is not None else value, key, await self._host())
            return await self._ask_confirm([payload], reasons, explanation, [line])

        approved_id = confirmation
        if decision.kind == "confirm" and not self._approvals.consume(confirmation, payload):
            approved_id = await ask(decision.reasons)
        try:
            return await self._call(lambda: invoke(approved_id))
        except DriverError as exc:
            if exc.code == ErrorCode.PASSWORD_FIELD:
                raise _handoff("password_field") from None
            if exc.code != ErrorCode.CONFIRMATION_REQUIRED:
                raise
            refused = exc
        approved_id = await ask(self._refused_reasons(refused, decision))
        try:
            return await self._call(lambda: invoke(approved_id))
        except DriverError as exc:
            if exc.code == ErrorCode.PASSWORD_FIELD:
                raise _handoff("password_field") from None
            raise

    @staticmethod
    def _refused_reasons(exc: DriverError, decision: Decision) -> List[str]:
        raw = exc.data.get("reasons")
        reasons = [item for item in raw if isinstance(item, str)] if isinstance(raw, list) else []
        return reasons or list(decision.reasons) or ["consequential"]

    async def goto(self, url: str, **_ignored: Any) -> None:
        self._require_web_url(url)
        await self._ensure_site(url)
        await self._call(lambda: self.inner.goto(url))

    async def current_url(self) -> str:
        return await self._call(self.inner.current_url)

    async def get_page_state(self, highlight: bool = False) -> PageState:
        url = await self._call(self.inner.current_url)
        await self._ensure_site(url)
        state = await self._call(lambda: self.inner.get_page_state(highlight))
        if state.url != url:
            await self._ensure_site(state.url)
        return state

    async def describe(self, target: Target) -> Element:
        return await self._call(lambda: self.inner.describe(target))

    async def is_visible(self, target: Target) -> bool:
        return await self._call(lambda: self.inner.is_visible(target))

    async def click(
        self,
        target: Target,
        *,
        confirmation: Optional[str] = None,
        force_confirm: bool = False,
        llm_consequential: bool = False,
        explanation: str = "",
    ) -> None:
        await self._gated(
            "click",
            target,
            lambda cid: self.inner.click(target, confirmation=cid),
            confirmation=confirmation,
            force_confirm=force_confirm,
            llm_consequential=llm_consequential,
            explanation=explanation,
        )

    async def type_text(
        self,
        target: Target,
        text: str,
        *,
        confirmation: Optional[str] = None,
        force_confirm: bool = False,
        llm_consequential: bool = False,
        explanation: str = "",
    ) -> None:
        await self._gated(
            "type",
            target,
            lambda cid: self.inner.type_text(target, text, confirmation=cid),
            text=text,
            confirmation=confirmation,
            force_confirm=force_confirm,
            llm_consequential=llm_consequential,
            explanation=explanation,
        )

    async def press_key(
        self,
        key: str,
        target: Optional[Target] = None,
        *,
        confirmation: Optional[str] = None,
        force_confirm: bool = False,
        llm_consequential: bool = False,
        explanation: str = "",
    ) -> None:
        await self._gated(
            "press",
            target,
            lambda cid: self.inner.press_key(key, target, confirmation=cid),
            key=key,
            confirmation=confirmation,
            force_confirm=force_confirm,
            llm_consequential=llm_consequential,
            explanation=explanation,
        )

    async def select_option(
        self,
        target: Target,
        value: str,
        *,
        confirmation: Optional[str] = None,
        force_confirm: bool = False,
        llm_consequential: bool = False,
        explanation: str = "",
    ) -> str:
        return await self._gated(
            "select",
            target,
            lambda cid: self.inner.select_option(target, value, confirmation=cid),
            value=value,
            confirmation=confirmation,
            force_confirm=force_confirm,
            llm_consequential=llm_consequential,
            explanation=explanation,
        )

    async def confirm_batch(
        self,
        items: Sequence[BatchItem],
        *,
        force_confirm: bool = False,
        explanation: str = "",
    ) -> Optional[str]:
        gated: List[Tuple[str, Optional[Target], Dict[str, Any], Optional[Element], List[str], Dict[str, Any]]] = []
        for op, target, kwargs in items:
            fields = dict(kwargs or {})
            element = await self._element_for(target)
            typed = fields.get("text") if fields.get("text") is not None else fields.get("value")
            decision = self.policy.evaluate(
                op,  # type: ignore[arg-type]
                element,
                value=typed,
                key=fields.get("key"),
                force_confirm=force_confirm,
            )
            if decision.kind == "handoff":
                raise _handoff(decision.reasons[0])
            if decision.kind == "confirm":
                payload = act_payload(op, target, text=fields.get("text"), key=fields.get("key"), value=fields.get("value"))
                gated.append((op, target, fields, element, decision.reasons, payload))
        if not gated:
            return None
        host = await self._host()
        lines = [
            describe_action_es(
                op,
                element,
                fields.get("text") if fields.get("text") is not None else fields.get("value"),
                fields.get("key"),
                host,
            )
            for op, _, fields, element, _, _ in gated
        ]
        reasons = _unique([reason for entry in gated for reason in entry[4]])
        payloads = [entry[5] for entry in gated]
        confirmation_id = await self._ask_confirm(payloads, reasons, explanation, lines)
        self._approvals.grant(confirmation_id, payloads)
        return confirmation_id

    async def scroll(self, direction: ScrollDirection = "down", amount: int = 500) -> None:
        await self._call(lambda: self.inner.scroll(direction, amount))

    async def read_text(self, target: Optional[Target] = None) -> str:
        return await self._call(lambda: self.inner.read_text(target))

    async def screenshot(self) -> bytes:
        return await self._call(self.inner.screenshot)

    async def highlight(self, mode: Literal["all", "off"] = "all") -> None:
        await self._call(lambda: self.inner.highlight(mode))

    async def new_tab(self, name: Optional[str] = None, url: Optional[str] = None, **_ignored: Any) -> str:
        if url:
            self._require_web_url(url)
            await self._ensure_site(url)
        return await self._call(lambda: self.inner.new_tab(name, url))

    async def switch_tab(self, name: str, **_ignored: Any) -> None:
        await self._call(lambda: self.inner.switch_tab(name))

    async def close_tab(self, name: Optional[str] = None, **_ignored: Any) -> bool:
        return await self._call(lambda: self.inner.close_tab(name))

    def list_tabs(self) -> List[str]:
        return self.inner.list_tabs()

    @property
    def current_tab(self) -> str:
        return self.inner.current_tab

    async def wait(self, ms: int) -> None:
        await self.inner.wait(ms)
