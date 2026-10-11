import asyncio
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol

from babas.drivers.base import DriverError, ErrorCode
from babas.safety import redact, title_es

log = logging.getLogger("babas.channels")

CONFIRM_TIMEOUT_S = 300
ASK_TIMEOUT_S = 600
ALLOW_TIMEOUT_S = 300
UI_LOG_TIMEOUT_S = 10.0
VOICE_CONFIRM_TIMEOUT_S = 120

_YES = frozenset({"y", "yes", "s", "si", "sí"})
_LOG_KINDS = frozenset({"assistant", "step", "result", "error", "info"})
_ASK_KINDS = frozenset({"text", "handoff"})
_RUN_STATES = frozenset({"running", "done", "failed", "stopped"})


@dataclass
class ConfirmRequest:
    actions: List[Dict[str, Any]]
    reasons: List[str]
    explanation: str = ""
    lines: List[str] = field(default_factory=list)


@dataclass
class ConfirmResult:
    approved: bool
    confirmation_id: Optional[str] = None
    reason: str = ""


class UserChannel(Protocol):
    async def confirm(self, req: ConfirmRequest) -> ConfirmResult: ...

    async def ask(self, question: str, kind: str = "text") -> Optional[str]: ...

    async def allow_site(self, host: str, url: str) -> bool: ...

    async def log(self, kind: str, text: str, ok: Optional[bool] = None) -> None: ...

    async def run_state(self, state: str, summary: str = "") -> None: ...


class _Bridge(Protocol):
    async def request(self, type: str, payload: Optional[Dict[str, Any]] = None, timeout: float = 30.0) -> Dict[str, Any]: ...


async def _read_line(prompt: str) -> str:
    # A daemon thread, not asyncio.to_thread: a pool worker blocked in input() keeps the process alive after Ctrl+C.
    loop = asyncio.get_running_loop()
    future: "asyncio.Future[str]" = loop.create_future()

    def deliver(line: Optional[str], error: Optional[Exception]) -> None:
        if future.done():
            return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(line or "")

    def read() -> None:
        line: Optional[str] = None
        error: Optional[Exception] = None
        try:
            line = input(prompt)
        except Exception as exc:
            error = exc
        try:
            loop.call_soon_threadsafe(deliver, line, error)
        except RuntimeError:
            pass

    threading.Thread(target=read, name="babas-console-input", daemon=True).start()
    return await future


def _say(text: str = "") -> None:
    print(text, flush=True)


class ConsoleChannel:
    def __init__(self, voice_gui: Any = None, ainput: Optional[Callable[[str], Awaitable[str]]] = None):
        self.voice_gui = voice_gui
        self._ainput = ainput or _read_line

    async def _prompt(self, prompt: str) -> Optional[str]:
        try:
            return await self._ainput(prompt)
        except Exception as exc:
            log.debug("console input ended: %s", type(exc).__name__)
            return None

    async def _voice_yes(self, text: str) -> bool:
        try:
            answer = await asyncio.to_thread(self.voice_gui.show_plan_and_confirm, text, VOICE_CONFIRM_TIMEOUT_S)
        except Exception as exc:
            log.warning("voice confirmation failed: %s", type(exc).__name__)
            return False
        return answer is True

    async def confirm(self, req: ConfirmRequest) -> ConfirmResult:
        explanation = redact(req.explanation) if req.explanation else ""
        if self.voice_gui is not None:
            text = "\n".join([title_es(req.reasons), *req.lines] + ([f"BABAS dice: {explanation}"] if explanation else []))
            approved = await self._voice_yes(text)
            return ConfirmResult(True) if approved else ConfirmResult(False, None, "denied")
        _say()
        _say(f"== {title_es(req.reasons)} ==")
        for line in req.lines:
            _say(f"  - {line}")
        if explanation:
            _say(f"  BABAS dice: {explanation}")
        answer = await self._prompt("Continue? (y/N): ")
        if answer is None:
            return ConfirmResult(False, None, "no_ui")
        if answer.strip().lower() in _YES:
            return ConfirmResult(True)
        return ConfirmResult(False, None, "denied")

    async def ask(self, question: str, kind: str = "text") -> Optional[str]:
        _say(redact(question))
        handoff = kind == "handoff"
        answer = await self._prompt("Press Enter when you are done… " if handoff else "> ")
        if answer is None:
            return None
        if handoff:
            return answer.strip() or "listo"
        return answer.strip()

    async def allow_site(self, host: str, url: str) -> bool:
        if self.voice_gui is not None:
            return await self._voice_yes(redact(f"BABAS quiere usar el sitio {host}. ¿Permitir?"))
        answer = await self._prompt(redact(f"Allow BABAS to use {host}? (y/N): "))
        return answer is not None and answer.strip().lower() in _YES

    async def log(self, kind: str, text: str, ok: Optional[bool] = None) -> None:
        safe = redact(text)
        if kind == "assistant":
            _say(f"BABAS: {safe}")
        elif kind == "step":
            marker = "[ok]" if ok is True else "[failed]" if ok is False else "-"
            _say(f"  {marker} {safe}")
        elif kind == "result":
            _say(f"\n=> {safe}")
        elif kind == "error":
            _say(f"Error: {safe}")
        else:
            _say(safe)

    async def run_state(self, state: str, summary: str = "") -> None:
        log.debug("run state: %s", state)


class DenyChannel:
    async def confirm(self, req: ConfirmRequest) -> ConfirmResult:
        return ConfirmResult(False, None, "no_ui")

    async def ask(self, question: str, kind: str = "text") -> Optional[str]:
        return None

    async def allow_site(self, host: str, url: str) -> bool:
        return False

    async def log(self, kind: str, text: str, ok: Optional[bool] = None) -> None:
        log.log(logging.WARNING if kind == "error" else logging.INFO, "[%s] %s", kind, redact(text))

    async def run_state(self, state: str, summary: str = "") -> None:
        log.info("run state: %s %s", state, redact(summary))


class PanelChannel:
    def __init__(
        self,
        bridge: _Bridge,
        *,
        confirm_timeout: int = CONFIRM_TIMEOUT_S,
        ask_timeout: int = ASK_TIMEOUT_S,
        allow_timeout: int = ALLOW_TIMEOUT_S,
    ):
        self.bridge = bridge
        self.confirm_timeout = confirm_timeout
        self.ask_timeout = ask_timeout
        self.allow_timeout = allow_timeout

    async def confirm(self, req: ConfirmRequest) -> ConfirmResult:
        payload = {
            "actions": req.actions,
            "reasons": list(req.reasons),
            "explanation": redact(req.explanation or ""),
            "timeout_s": self.confirm_timeout,
        }
        try:
            answer = await self.bridge.request("ui.confirm", payload, timeout=self.confirm_timeout + 5)
        except DriverError as exc:
            return ConfirmResult(False, None, exc.code)
        except Exception as exc:
            log.warning("ui.confirm failed: %s", type(exc).__name__)
            return ConfirmResult(False, None, ErrorCode.INTERNAL)
        if not isinstance(answer, dict):
            return ConfirmResult(False, None, "malformed")
        if answer.get("approved") is True:
            confirmation_id = answer.get("confirmation_id")
            if isinstance(confirmation_id, str) and confirmation_id:
                return ConfirmResult(True, confirmation_id)
            return ConfirmResult(False, None, "malformed")
        reason = answer.get("reason")
        return ConfirmResult(False, None, reason if isinstance(reason, str) and reason else "denied")

    async def ask(self, question: str, kind: str = "text") -> Optional[str]:
        payload = {
            "question": redact(question),
            "kind": kind if kind in _ASK_KINDS else "text",
            "timeout_s": self.ask_timeout,
        }
        try:
            answer = await self.bridge.request("ui.ask", payload, timeout=self.ask_timeout + 5)
        except DriverError:
            return None
        except Exception as exc:
            log.warning("ui.ask failed: %s", type(exc).__name__)
            return None
        value = answer.get("answer") if isinstance(answer, dict) else None
        return value if isinstance(value, str) else None

    async def allow_site(self, host: str, url: str) -> bool:
        payload = {"host": host, "url": redact(url), "timeout_s": self.allow_timeout}
        try:
            answer = await self.bridge.request("ui.allow_site", payload, timeout=self.allow_timeout + 5)
        except DriverError:
            return False
        except Exception as exc:
            log.warning("ui.allow_site failed: %s", type(exc).__name__)
            return False
        return isinstance(answer, dict) and answer.get("approved") is True

    async def log(self, kind: str, text: str, ok: Optional[bool] = None) -> None:
        payload: Dict[str, Any] = {"kind": kind if kind in _LOG_KINDS else "info", "text": redact(text)}
        if ok is not None:
            payload["ok"] = bool(ok)
        try:
            await self.bridge.request("ui.log", payload, timeout=UI_LOG_TIMEOUT_S)
        except Exception as exc:
            log.debug("ui.log not delivered: %s", type(exc).__name__)

    async def run_state(self, state: str, summary: str = "") -> None:
        payload: Dict[str, Any] = {"state": state if state in _RUN_STATES else "failed"}
        if summary:
            payload["summary"] = redact(summary)
        try:
            await self.bridge.request("ui.run_state", payload, timeout=UI_LOG_TIMEOUT_S)
        except Exception as exc:
            log.debug("ui.run_state not delivered: %s", type(exc).__name__)
