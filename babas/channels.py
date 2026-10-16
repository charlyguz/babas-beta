import asyncio
import logging
import sys
import threading
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol

from babas import ui
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
            line = input(ui.for_input(prompt))
        except Exception as exc:
            error = exc
        try:
            loop.call_soon_threadsafe(deliver, line, error)
        except RuntimeError:
            pass

    threading.Thread(target=read, name="babas-console-input", daemon=True).start()
    return await future


def _say(text: str = "") -> None:
    ui.emit(text)


class ConsoleChannel:
    def __init__(
        self,
        voice_gui: Any = None,
        ainput: Optional[Callable[[str], Awaitable[str]]] = None,
        term: Optional[ui.Term] = None,
    ):
        self.voice_gui = voice_gui
        self._ainput = ainput or _read_line
        self._term = term

    @property
    def term(self) -> ui.Term:
        return self._term if self._term is not None else ui.detect(sys.stdout)

    async def _prompt(self, prompt: str) -> Optional[str]:
        try:
            with ui.suspended():
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
        term = self.term
        bullet = term.glyph("bullet")
        body: List[str] = [f"{bullet} {line}" for line in req.lines]
        if explanation:
            body.extend(["", f"BABAS dice: {explanation}"] if body else [f"BABAS dice: {explanation}"])
        _say()
        _say(ui.card(term, title_es(req.reasons), body, kind="confirm"))
        answer = await self._prompt(ui.question(term, "Continue? (y/N): "))
        if answer is None:
            return ConfirmResult(False, None, "no_ui")
        if answer.strip().lower() in _YES:
            return ConfirmResult(True)
        return ConfirmResult(False, None, "denied")

    async def ask(self, question: str, kind: str = "text") -> Optional[str]:
        term = self.term
        handoff = kind == "handoff"
        _say()
        _say(ui.card(term, "Necesito su ayuda" if handoff else "BABAS pregunta", [redact(question)], kind="handoff" if handoff else "question"))
        prompt = ui.question(term, f"Press Enter when you are done{term.glyph('ellipsis')} ") if handoff else ui.prompt(term)
        answer = await self._prompt(prompt)
        if answer is None:
            return None
        if handoff:
            return answer.strip() or "listo"
        return answer.strip()

    async def allow_site(self, host: str, url: str) -> bool:
        if self.voice_gui is not None:
            return await self._voice_yes(redact(f"BABAS quiere usar el sitio {host}. ¿Permitir?"))
        term = self.term
        _say()
        _say(ui.card(term, "Permiso para un sitio", [redact(f"BABAS quiere usar el sitio {host}.")], kind="warn"))
        answer = await self._prompt(ui.question(term, redact(f"Allow BABAS to use {host}? (y/N): ")))
        return answer is not None and answer.strip().lower() in _YES

    async def log(self, kind: str, text: str, ok: Optional[bool] = None) -> None:
        safe = redact(text)
        term = self.term
        if kind == "assistant":
            _say(ui.speech(term, safe))
        elif kind == "step":
            _say(ui.step(term, "success" if ok is True else "failure" if ok is False else "info", safe))
        elif kind == "result":
            _say()
            _say(ui.answer_panel(term, safe))
        elif kind == "error":
            _say()
            _say(ui.error_panel(term, safe))
        else:
            _say(ui.step(term, "info", safe))

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
