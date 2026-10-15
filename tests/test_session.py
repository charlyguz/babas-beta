import asyncio
import base64
import json
import logging
import re
import sys
import time
import types
from dataclasses import replace
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Tuple, Union

import pytest
from helpers import EventBridge, FakeDriver, FakeLLM, FakeSTT, make_element, make_state
from websockets.asyncio.client import ClientConnection, connect

import babas.session as session_module
from babas.__main__ import main as babas_main
from babas.agent import DENIED_ANSWER, FAILED_ANSWER, Agent
from babas.bridge.protocol import PINNED_EXTENSION_ID, PROTOCOL_VERSION, new_id
from babas.bridge.server import BridgeServer
from babas.cli import preflight_problem
from babas.config import Settings
from babas.devrepl import _Session as ReplSession
from babas.devrepl import run_serve
from babas.drivers.base import BrowserDriver, DriverError, ErrorCode, PageState
from babas.llm import LLMConfigError, LLMError
from babas.safety import SafetyPolicy
from babas.session import BUSY_TEXT, DISCONNECTED_TEXT, FAILED_TEXT, MISSING_KEY_TEXT, Session
from babas.stt import (
    BAD_AUDIO_TEXT,
    EMPTY_TEXT,
    ERROR_CODES,
    NOT_CONFIGURED_TEXT,
    PROVIDER_TEXT,
    STT_BUSY_TEXT,
    STTConfigError,
    STTError,
)

ORIGIN = f"chrome-extension://{PINNED_EXTENSION_ID}"
WAIT = 3.0
URL = "https://www.gob.mx/"
CURP = "GOMC800101HDFRRN09"
TOKEN_RE = re.compile(r"ws://127\.0\.0\.1:(\d+)")

Handler = Callable[[Dict[str, Any]], Union[Dict[str, Any], Awaitable[Dict[str, Any]]]]


def default_state() -> PageState:
    return make_state(
        make_element(0, "textbox", "Buscar", tag="input"),
        make_element(1, "link", "Inicio"),
        make_element(2, "button", "Pagar", flags=["consequential"]),
        url=URL,
    )


class RemoteBridge:
    def __init__(self, port: int):
        self.url = f"ws://127.0.0.1:{port}"

    async def wait_connected(self, timeout: float) -> bool:
        return True


class FakeExtension:
    def __init__(self, bridge: Any, state: Optional[PageState] = None):
        self.bridge = bridge
        self.state = state or default_state()
        self.requests: List[Tuple[str, Dict[str, Any]]] = []
        self.handlers: Dict[str, Handler] = {}
        self.minted = 0
        self._connection: Any = None
        self._ws: Optional[ClientConnection] = None
        self._reader: Optional["asyncio.Task[None]"] = None
        self._answers: set = set()

    async def __aenter__(self) -> "FakeExtension":
        self._connection = connect(self.bridge.url, origin=ORIGIN, proxy=None, open_timeout=WAIT)
        self._ws = await self._connection.__aenter__()
        hello = {"ext_version": "0.2.0", "protocol": PROTOCOL_VERSION}
        await self._ws.send(json.dumps({"v": 1, "id": new_id(), "type": "hello", "payload": hello}))
        welcome = json.loads(await asyncio.wait_for(self._ws.recv(), WAIT))
        assert welcome["type"] == "welcome"
        self.welcome: Dict[str, Any] = welcome["payload"]
        self._reader = asyncio.create_task(self._serve())
        assert await self.bridge.wait_connected(WAIT)
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        assert self._reader is not None
        self._reader.cancel()
        for task in list(self._answers):
            task.cancel()
        await asyncio.gather(self._reader, *self._answers, return_exceptions=True)
        await self._connection.__aexit__(*exc_info)

    async def _serve(self) -> None:
        assert self._ws is not None
        async for raw in self._ws:
            message = json.loads(raw)
            if message.get("type") == "result":
                continue
            task = asyncio.create_task(self._answer(message))
            self._answers.add(task)
            task.add_done_callback(self._answers.discard)

    async def _answer(self, message: Dict[str, Any]) -> None:
        assert self._ws is not None
        kind, payload = message["type"], message.get("payload") or {}
        self.requests.append((kind, payload))
        handler = self.handlers.get(kind) or getattr(self, "_default_" + kind.replace(".", "_"), None)
        try:
            result = handler(payload) if handler is not None else {}
            if asyncio.iscoroutine(result):
                result = await result
            frame: Dict[str, Any] = {"v": 1, "id": message["id"], "type": "result", "ok": True, "payload": result}
        except DriverError as exc:
            error = {"code": exc.code, "message": exc.message, "data": exc.data}
            frame = {"v": 1, "id": message["id"], "type": "result", "ok": False, "error": error}
        await self._ws.send(json.dumps(frame))

    def _default_tab_info(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"tab_id": 7, "url": URL, "title": "Gob"}

    def _default_page_state(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.state.model_dump(mode="json")

    def _default_page_describe(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        element = self.state.element(payload["target"]["index"])
        assert element is not None
        return {"element": element.model_dump(mode="json")}

    def _default_page_act(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"result": {}, "tab_id": 7, "url": URL, "title": "Gob"}

    def _default_ui_confirm(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self.minted += 1
        return {"approved": True, "confirmation_id": f"conf-{self.minted}"}

    def _default_ui_ask(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"answer": "listo"}

    def _default_ui_allow_site(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"approved": True}

    async def send_event(self, kind: str, payload: Dict[str, Any]) -> None:
        assert self._ws is not None
        await self._ws.send(json.dumps({"v": 1, "id": new_id(), "type": kind, "payload": payload}))

    async def start_task(self, text: str) -> None:
        await self.send_event("task.start", {"text": text})

    async def stop(self) -> None:
        await self.send_event("run.stop", {})

    def of(self, kind: str) -> List[Dict[str, Any]]:
        return [payload for name, payload in self.requests if name == kind]

    def run_states(self) -> List[Tuple[str, str]]:
        return [(item["state"], item.get("summary", "")) for item in self.of("ui.run_state")]

    def logs(self) -> List[Tuple[str, str]]:
        return [(item["kind"], item["text"]) for item in self.of("ui.log")]

    async def until(self, check: Callable[[], bool], what: str, timeout: float = WAIT) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if check():
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"timed out waiting for {what}; requests: {[name for name, _ in self.requests]}")

    async def until_state(self, state: str, timeout: float = WAIT) -> None:
        await self.until(lambda: any(item[0] == state for item in self.run_states()), f"run_state {state}", timeout)


class DriverExecutor:
    # Stands in for babas.actions: just enough to push click and wait steps through the real gate.
    def __init__(self, driver: BrowserDriver):
        self.driver = driver

    async def execute(self, step: Any, state: Optional[PageState], *, confirmation: Optional[str] = None) -> None:
        if step.action == "click":
            target = step.to_target(state.snapshot_id if state else None)
            assert target is not None
            await self.driver.click(target, confirmation=confirmation)
        elif step.action == "wait":
            await asyncio.sleep(step.ms / 1000)
        else:
            raise ValueError(step.action)


def click_plan(index: int, explanation: str = "Voy a abrir el enlace.") -> Dict[str, Any]:
    return {"steps": [{"action": "click", "index": index, "explanation": explanation}]}


def done_plan(text: str = "Listo.", success: bool = True) -> Dict[str, Any]:
    return {"steps": [{"action": "done", "value": text, "success": success}]}


@pytest.fixture(autouse=True)
def agents(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    built: List[Dict[str, Any]] = []

    def build(driver: Any, llm: Any, channel: Any, **kwargs: Any) -> Agent:
        built.append(dict(kwargs))
        return Agent(driver, llm, channel, executor=DriverExecutor(driver), **kwargs)

    monkeypatch.setattr(session_module, "Agent", build)
    return built


@pytest.fixture(autouse=True)
def isolated_logging():
    logger = logging.getLogger("babas")
    handlers, level = list(logger.handlers), logger.level
    yield
    logger.handlers[:] = handlers
    logger.setLevel(level)


@pytest.fixture
async def bridge() -> AsyncIterator[BridgeServer]:
    server = BridgeServer(port=0)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture
async def ext(bridge: BridgeServer) -> AsyncIterator[FakeExtension]:
    async with FakeExtension(bridge) as extension:
        yield extension


def make_session(bridge: BridgeServer, llm: Any, **kwargs: Any) -> Session:
    factory = (lambda: llm) if hasattr(llm, "complete_json") else llm
    return Session(bridge, factory, SafetyPolicy(), **kwargs)


async def settled(session: Session, timeout: float = WAIT) -> None:
    deadline = time.monotonic() + timeout
    while session.running and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert not session.running


async def test_task_runs_to_done(bridge: BridgeServer, ext: FakeExtension, agents: List[Dict[str, Any]]) -> None:
    llm = FakeLLM([click_plan(1), done_plan("Encontré lo que buscaba.")])
    session = make_session(bridge, llm, language="es", max_steps=7, max_retries=1)
    assert not session.running

    await ext.start_task("abre el sitio de gobierno")
    await ext.until_state("done")
    await settled(session)

    assert ext.run_states() == [("running", ""), ("done", "Encontré lo que buscaba.")]
    assert ("result", "Encontré lo que buscaba.") in ext.logs()
    assert ("assistant", "Voy a abrir el enlace.") in ext.logs()
    steps = [item for item in ext.of("ui.log") if item["kind"] == "step"]
    assert len(steps) == 1 and steps[0]["ok"] is True
    clicks = [item for item in ext.of("page.act") if item["op"] == "click"]
    assert len(clicks) == 1 and clicks[0]["target"]["index"] == 1
    assert ext.of("ui.confirm") == []
    assert len(llm.calls) == 2
    assert [(item["language"], item["max_steps"], item["max_retries"]) for item in agents] == [("es", 7, 1)]
    assert "abre el sitio de gobierno" in llm.calls[0][1]


async def test_failed_status_is_forwarded_with_the_answer(bridge: BridgeServer, ext: FakeExtension) -> None:
    make_session(bridge, FakeLLM([done_plan("No encontré nada.", success=False)]))
    await ext.start_task("busca algo")
    await ext.until_state("failed")
    assert ext.run_states()[-1] == ("failed", "No encontré nada.")


async def test_second_task_while_running_gets_the_busy_message(bridge: BridgeServer, ext: FakeExtension) -> None:
    llm = FakeLLM(delay=30)
    session = make_session(bridge, llm)

    await ext.start_task("primera tarea")
    await ext.until_state("running")
    await ext.until(lambda: bool(ext.of("page.state")), "the first run to wait on the model")
    await ext.start_task("segunda tarea")
    await ext.until(lambda: ("info", BUSY_TEXT) in ext.logs(), "the busy message")

    assert BUSY_TEXT == "Ya estoy trabajando en otra tarea. Presione «Detener» si quiere empezar otra."
    assert session.running
    assert ext.run_states() == [("running", "")]

    await ext.stop()
    await ext.until_state("stopped")
    await settled(session)

    llm.delay = 0
    await ext.start_task("tercera tarea")
    await ext.until_state("done")
    assert [state for state, _ in ext.run_states()] == ["running", "stopped", "running", "done"]


async def test_run_stop_cancels_quickly_and_reports_stopped(bridge: BridgeServer, ext: FakeExtension) -> None:
    session = make_session(bridge, FakeLLM(delay=30))
    await ext.start_task("tarea larga")
    await ext.until_state("running")
    started = time.monotonic()

    await ext.stop()
    await ext.until_state("stopped", timeout=2.0)
    await settled(session, timeout=2.0)

    assert time.monotonic() - started < 2.0
    assert ext.run_states() == [("running", ""), ("stopped", "")]


async def test_stop_while_a_confirmation_is_pending(bridge: BridgeServer, ext: FakeExtension) -> None:
    release = asyncio.Event()

    async def hold(payload: Dict[str, Any]) -> Dict[str, Any]:
        await release.wait()
        return {"approved": True, "confirmation_id": "late"}

    ext.handlers["ui.confirm"] = hold
    session = make_session(bridge, FakeLLM([click_plan(2, "Voy a pagar.")]))

    await ext.start_task("paga la cuenta")
    await ext.until(lambda: bool(ext.of("ui.confirm")), "the confirmation request")
    await ext.stop()
    await ext.until_state("stopped")
    await settled(session)

    assert [item for item in ext.of("page.act") if item["op"] == "click"] == []
    assert ext.run_states()[-1] == ("stopped", "")


async def test_approved_confirmation_runs_the_action_with_its_id(bridge: BridgeServer, ext: FakeExtension) -> None:
    make_session(bridge, FakeLLM([click_plan(2, "Voy a pagar."), done_plan("Pagado.")]))

    await ext.start_task("paga la cuenta")
    await ext.until_state("done")

    confirms = ext.of("ui.confirm")
    assert len(confirms) == 1
    assert confirms[0]["actions"][0]["op"] == "click" and "consequential" in confirms[0]["reasons"]
    clicks = [item for item in ext.of("page.act") if item["op"] == "click"]
    assert [item.get("confirmation_id") for item in clicks] == ["conf-1"]
    assert ext.run_states()[-1] == ("done", "Pagado.")


async def test_denied_confirmation_ends_the_run_as_stopped(bridge: BridgeServer, ext: FakeExtension) -> None:
    ext.handlers["ui.confirm"] = lambda payload: {"approved": False, "reason": "denied"}
    llm = FakeLLM([click_plan(2, "Voy a pagar."), done_plan("No debería llegar aquí.")])
    session = make_session(bridge, llm)

    await ext.start_task("paga la cuenta")
    await ext.until_state("stopped")
    await settled(session)

    assert ext.run_states() == [("running", ""), ("stopped", DENIED_ANSWER)]
    assert [item for item in ext.of("page.act") if item["op"] == "click"] == []
    assert len(llm.calls) == 1


MISSING_KEY_REASON = "No API key is set: put BABAS_LLM_API_KEY (or OPENAI_API_KEY) in .env or the environment."


async def test_missing_key_when_the_factory_fails(
    bridge: BridgeServer, ext: FakeExtension, caplog: pytest.LogCaptureFixture
) -> None:
    def factory() -> Any:
        raise LLMConfigError(MISSING_KEY_REASON)

    session = make_session(bridge, factory)
    with caplog.at_level(logging.WARNING, logger="babas"):
        await ext.start_task("busca recetas")
        await ext.until_state("failed")
        await settled(session)

    assert MISSING_KEY_TEXT == (
        "Falta configurar el modelo de IA. Pida a quien instaló BABAS que revise BABAS_LLM_API_KEY y BABAS_LLM_MODEL."
    )
    assert ("error", MISSING_KEY_TEXT) in ext.logs()
    assert ext.run_states()[-1] == ("failed", MISSING_KEY_TEXT)
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1 and "BABAS_LLM_API_KEY" in warnings[0].getMessage()


async def test_missing_key_on_the_first_model_call_is_not_retried(
    bridge: BridgeServer, ext: FakeExtension, caplog: pytest.LogCaptureFixture
) -> None:
    llm = FakeLLM([LLMConfigError(MISSING_KEY_REASON)] * 10)
    session = make_session(bridge, llm)
    with caplog.at_level(logging.WARNING, logger="babas"):
        await ext.start_task("busca recetas")
        await ext.until_state("failed")
        await settled(session)

    assert len(llm.calls) == 1
    assert ("error", MISSING_KEY_TEXT) in ext.logs()
    assert ext.run_states() == [("running", ""), ("failed", MISSING_KEY_TEXT)]
    assert any(record.levelno == logging.WARNING and "not configured" in record.getMessage() for record in caplog.records)


async def test_a_missing_model_or_package_gets_the_same_message(bridge: BridgeServer, ext: FakeExtension) -> None:
    session = make_session(bridge, FakeLLM([LLMConfigError("BABAS_LLM_MODEL is not set: the Anthropic provider needs a model id.")]))
    await ext.start_task("busca recetas")
    await ext.until_state("failed")
    await settled(session)
    assert ext.run_states()[-1] == ("failed", MISSING_KEY_TEXT)


async def test_the_configuration_reason_is_logged_without_personal_data(
    bridge: BridgeServer, ext: FakeExtension, caplog: pytest.LogCaptureFixture
) -> None:
    session = make_session(bridge, FakeLLM([LLMConfigError(f"cannot use the model for {CURP}")]))
    with caplog.at_level(logging.DEBUG, logger="babas"):
        await ext.start_task("busca recetas")
        await ext.until_state("failed")
        await settled(session)
    assert "not configured" in caplog.text
    assert CURP not in caplog.text
    assert CURP not in json.dumps([payload for kind, payload in ext.requests if kind.startswith("ui.")])


async def test_other_model_errors_do_not_look_like_a_missing_key(bridge: BridgeServer, ext: FakeExtension) -> None:
    llm = FakeLLM([LLMError("The LLM provider rejected the API key (HTTP 401).")] * 10)
    session = make_session(bridge, llm)
    await ext.start_task("busca recetas")
    await ext.until_state("failed")
    await settled(session)

    assert all(text != MISSING_KEY_TEXT for _, text in ext.logs())
    assert ext.run_states()[-1][0] == "failed"


async def test_a_plain_model_error_that_names_a_variable_is_not_a_configuration_problem(
    bridge: BridgeServer, ext: FakeExtension
) -> None:
    session = make_session(bridge, FakeLLM([LLMError("OPENAI_API_KEY is not set. Put it in .env.")] * 10))
    await ext.start_task("busca recetas")
    await ext.until_state("failed")
    await settled(session)
    assert all(text != MISSING_KEY_TEXT for _, text in ext.logs())
    assert ext.run_states()[-1] == ("failed", FAILED_ANSWER)


async def test_model_exception_becomes_failed_and_the_session_survives(bridge: BridgeServer, ext: FakeExtension) -> None:
    llm = FakeLLM([RuntimeError("boom")] * 3)
    session = make_session(bridge, llm)

    await ext.start_task("primera")
    await ext.until_state("failed")
    await settled(session)
    assert any(kind == "error" for kind, _ in ext.logs())

    await ext.start_task("segunda")
    await ext.until(lambda: [state for state, _ in ext.run_states()].count("done") == 1, "the second run to finish")
    assert [state for state, _ in ext.run_states()] == ["running", "failed", "running", "done"]


async def test_factory_exception_becomes_failed_without_leaking_personal_data(
    bridge: BridgeServer, ext: FakeExtension, caplog: pytest.LogCaptureFixture
) -> None:
    calls: List[int] = []

    def factory() -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError(f"cannot build the model for {CURP}")
        return FakeLLM()

    session = make_session(bridge, factory)
    with caplog.at_level(logging.DEBUG, logger="babas"):
        await ext.start_task(f"consulta mi CURP {CURP}")
        await ext.until_state("failed")
        await settled(session)

        assert ("error", FAILED_TEXT) in ext.logs()
        assert ext.run_states()[-1] == ("failed", FAILED_TEXT)
        assert "RuntimeError" in caplog.text

        await ext.start_task("otra vez")
        await ext.until_state("done")

    assert CURP not in caplog.text
    assert CURP not in json.dumps([payload for kind, payload in ext.requests if kind.startswith("ui.")])


async def test_task_text_is_never_logged(bridge: BridgeServer, ext: FakeExtension, caplog: pytest.LogCaptureFixture) -> None:
    make_session(bridge, FakeLLM())
    with caplog.at_level(logging.DEBUG, logger="babas"):
        await ext.start_task(f"mi CURP es {CURP} y mi correo es ana@example.com")
        await ext.until_state("done")
    assert CURP not in caplog.text
    assert "ana@example.com" not in caplog.text
    assert "task started" in caplog.text


async def test_halted_before_the_run_starts_is_reported_as_stopped(bridge: BridgeServer, ext: FakeExtension) -> None:
    def halted(payload: Dict[str, Any]) -> Dict[str, Any]:
        raise DriverError(ErrorCode.HALTED, "The user pressed Stop.")

    ext.handlers["tab.info"] = halted
    session = make_session(bridge, FakeLLM())
    await ext.start_task("hola")
    await ext.until_state("stopped")
    await settled(session)
    assert ext.run_states() == [("stopped", "")]


async def test_driver_failure_at_start_is_reported_in_spanish(bridge: BridgeServer, ext: FakeExtension) -> None:
    def broken(payload: Dict[str, Any]) -> Dict[str, Any]:
        raise DriverError(ErrorCode.INTERNAL, "the tab vanished")

    ext.handlers["tab.info"] = broken
    session = make_session(bridge, FakeLLM())
    await ext.start_task("hola")
    await ext.until_state("failed")
    await settled(session)
    assert ("error", FAILED_TEXT) in ext.logs()
    assert ext.run_states() == [("failed", FAILED_TEXT)]


async def test_lost_connection_message(bridge: BridgeServer, ext: FakeExtension) -> None:
    def gone(payload: Dict[str, Any]) -> Dict[str, Any]:
        raise DriverError(ErrorCode.NO_EXTENSION, "The extension disconnected")

    ext.handlers["tab.info"] = gone
    session = make_session(bridge, FakeLLM())
    await ext.start_task("hola")
    await ext.until_state("failed")
    await settled(session)
    assert ext.run_states() == [("failed", DISCONNECTED_TEXT)]
    assert ("error", DISCONNECTED_TEXT) in ext.logs()


async def test_invalid_task_payloads_are_ignored(bridge: BridgeServer, ext: FakeExtension) -> None:
    session = make_session(bridge, FakeLLM())
    for payload in ({}, {"text": ""}, {"text": "   "}, {"text": 5}, {"text": None}, {"text": ["x"]}, {"other": "x"}):
        await ext.send_event("task.start", payload)
    await asyncio.sleep(0.2)
    assert ext.requests == []
    assert not session.running

    await ext.start_task("  hola  ")
    await ext.until_state("done")
    assert ext.run_states() == [("running", ""), ("done", "Listo.")]


async def test_run_stop_without_a_run_is_harmless(bridge: BridgeServer, ext: FakeExtension) -> None:
    session = make_session(bridge, FakeLLM())
    await ext.stop()
    await asyncio.sleep(0.1)
    assert ext.requests == []

    await ext.start_task("hola")
    await ext.until_state("done")
    await settled(session)
    await ext.stop()
    await asyncio.sleep(0.1)
    assert [state for state, _ in ext.run_states()] == ["running", "done"]


async def test_session_stop_cancels_and_waits(bridge: BridgeServer, ext: FakeExtension) -> None:
    session = make_session(bridge, FakeLLM(delay=30))
    await session.stop()

    await ext.start_task("tarea larga")
    await ext.until_state("running")
    assert session.running

    await asyncio.wait_for(session.stop(), 2.0)
    assert not session.running
    await ext.until_state("stopped")


async def test_finished_run_clears_the_active_task(bridge: BridgeServer, ext: FakeExtension) -> None:
    session = make_session(bridge, FakeLLM())
    for number in range(3):
        await ext.start_task(f"tarea {number}")
        await ext.until(lambda n=number: [s for s, _ in ext.run_states()].count("done") == n + 1, f"run {number}")
        await settled(session)
    assert ext.logs().count(("info", BUSY_TEXT)) == 0
    assert [state for state, _ in ext.run_states()] == ["running", "done"] * 3


async def test_every_run_gets_a_new_model_client(bridge: BridgeServer, ext: FakeExtension) -> None:
    built: List[FakeLLM] = []

    def factory() -> FakeLLM:
        built.append(FakeLLM())
        return built[-1]

    session = make_session(bridge, factory)
    for number in range(2):
        await ext.start_task(f"tarea {number}")
        await ext.until(lambda n=number: [s for s, _ in ext.run_states()].count("done") == n + 1, f"run {number}")
        await settled(session)
    assert len(built) == 2 and built[0] is not built[1]


class ClosableLLM(FakeLLM):
    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


async def test_the_model_client_is_closed_after_every_run(bridge: BridgeServer, ext: FakeExtension) -> None:
    built: List[ClosableLLM] = []

    def factory() -> ClosableLLM:
        built.append(ClosableLLM())
        return built[-1]

    session = make_session(bridge, factory)
    await ext.start_task("tarea uno")
    await ext.until_state("done")
    await settled(session)
    assert [client.closed for client in built] == [1]

    slow = ClosableLLM(delay=30)
    session.llm_factory = lambda: slow
    await ext.start_task("tarea larga")
    await ext.until(lambda: [state for state, _ in ext.run_states()].count("running") == 2, "the second run")
    await ext.stop()
    await ext.until_state("stopped")
    await settled(session)
    assert slow.closed == 1


async def test_a_failing_close_does_not_break_the_run(bridge: BridgeServer, ext: FakeExtension) -> None:
    class BrokenClose(FakeLLM):
        async def close(self) -> None:
            raise RuntimeError("cannot close")

    make_session(bridge, BrokenClose())
    await ext.start_task("hola")
    await ext.until_state("done")
    assert ext.run_states() == [("running", ""), ("done", "Listo.")]


async def test_another_controller_blocks_a_new_panel_task(bridge: BridgeServer, ext: FakeExtension) -> None:
    other = {"active": True}
    llm = FakeLLM()
    session = make_session(bridge, llm, busy=lambda: other["active"])

    await ext.start_task("desde el panel")
    await ext.until(lambda: ("info", BUSY_TEXT) in ext.logs(), "the busy message")
    assert not session.running and llm.calls == [] and ext.run_states() == []

    other["active"] = False
    await ext.start_task("otra vez")
    await ext.until_state("done")


async def test_a_broken_busy_check_does_not_block_the_panel(bridge: BridgeServer, ext: FakeExtension) -> None:
    def broken() -> bool:
        raise RuntimeError("boom")

    make_session(bridge, FakeLLM(), busy=broken)
    await ext.start_task("hola")
    await ext.until_state("done")


@pytest.fixture
def babas_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "babas-home"
    monkeypatch.setenv("BABAS_HOME", str(home))
    return home


def serve_settings(home: Path, **env: str) -> Settings:
    return replace(Settings.from_env({"BABAS_HOME": str(home), **env}), bridge_port=0)


def test_pair_reports_the_state_without_the_token(babas_home: Path, capsys: pytest.CaptureFixture) -> None:
    path = babas_home / "pairing.json"

    babas_main(["pair"])
    out = capsys.readouterr().out
    assert "No pairing file" in out and str(path) in out

    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"token": "SECRET-TOKEN-VALUE"}), encoding="utf-8")
    babas_main(["pair"])
    out = capsys.readouterr().out
    assert "exists" in out and str(path) in out
    assert "SECRET-TOKEN-VALUE" not in out
    assert path.exists()


def test_pair_reset_removes_the_file(babas_home: Path, capsys: pytest.CaptureFixture) -> None:
    path = babas_home / "pairing.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"token": "SECRET-TOKEN-VALUE"}), encoding="utf-8")

    babas_main(["pair", "--reset"])
    out = capsys.readouterr().out
    assert out.strip() == "Pairing removed. The extension will pair again the next time it connects."
    assert "SECRET-TOKEN-VALUE" not in out
    assert not path.exists()

    babas_main(["pair", "--reset"])
    assert capsys.readouterr().out.strip() == "No pairing file."


def test_pair_reset_failure_exits_with_an_error(babas_home: Path, capsys: pytest.CaptureFixture) -> None:
    path = babas_home / "pairing.json"
    path.mkdir(parents=True)
    with pytest.raises(SystemExit) as info:
        babas_main(["pair", "--reset"])
    assert info.value.code == 1
    assert "Could not remove the pairing file" in capsys.readouterr().out


@pytest.mark.parametrize(
    "variable, value",
    [("BABAS_MAX_STEPS", "abc"), ("BABAS_LLM_PROVIDER", "gemini"), ("BABAS_BRIDGE_PORT", "99999")],
)
@pytest.mark.parametrize("command", [["pair"], ["mcp"], ["serve"]])
def test_an_invalid_environment_value_exits_2_naming_the_variable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, command: List[str], variable: str, value: str
) -> None:
    monkeypatch.setenv(variable, value)
    with pytest.raises(SystemExit) as info:
        babas_main(command)
    assert info.value.code == 2
    captured = capsys.readouterr()
    assert variable in captured.err and captured.out == ""


def test_mcp_http_cannot_be_combined_with_repl(capsys: pytest.CaptureFixture) -> None:
    with pytest.raises(SystemExit) as info:
        babas_main(["serve", "--repl", "--mcp-http"])
    assert info.value.code == 2
    captured = capsys.readouterr()
    assert "--mcp-http cannot be combined with --repl" in captured.err and captured.out == ""


@pytest.mark.parametrize("port", ["abc", "-1", "70000"])
def test_serve_rejects_an_invalid_port_flag(capsys: pytest.CaptureFixture, port: str) -> None:
    with pytest.raises(SystemExit) as info:
        babas_main(["serve", "--port", port])
    assert info.value.code == 2
    assert "port" in capsys.readouterr().err


def test_serve_flags_override_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    import babas.devrepl as devrepl

    calls: List[Tuple[Settings, bool, bool]] = []

    async def fake_run_serve(settings: Settings, repl: bool = False, mcp_http: bool = False) -> None:
        calls.append((settings, repl, mcp_http))

    monkeypatch.setattr(devrepl, "run_serve", fake_run_serve)
    monkeypatch.setenv("BABAS_BRIDGE_PORT", "9001")
    monkeypatch.setenv("BABAS_BRIDGE_HOST", "127.0.0.2")
    monkeypatch.setenv("BABAS_MCP_HTTP_PORT", "9002")

    babas_main(["serve"])
    babas_main(["serve", "--port", "9003", "--host", "localhost", "--mcp-http"])
    babas_main(["serve", "--repl"])

    (first, first_repl, first_mcp), (second, second_repl, second_mcp), (third, third_repl, _) = calls
    assert (first.bridge_host, first.bridge_port, first.mcp_http_port) == ("127.0.0.2", 9001, 9002)
    assert (first_repl, first_mcp) == (False, False)
    assert (second.bridge_host, second.bridge_port, second.mcp_http_port) == ("localhost", 9003, 9002)
    assert (second_repl, second_mcp) == (False, True)
    assert third_repl is True


def stub_mcp_module(monkeypatch: pytest.MonkeyPatch, **members: Any) -> types.ModuleType:
    module = types.ModuleType("babas.mcp_server")
    for name, value in members.items():
        setattr(module, name, value)
    monkeypatch.setitem(sys.modules, "babas.mcp_server", module)
    return module


def hide_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [name for name in sys.modules if name == "mcp" or name.startswith("mcp.") or name == "babas.mcp_server"]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "mcp", None)


def test_mcp_command_runs_the_stdio_server_with_the_settings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    seen: List[Settings] = []

    async def run_stdio(settings: Settings) -> None:
        seen.append(settings)

    stub_mcp_module(monkeypatch, run_stdio=run_stdio)
    monkeypatch.setenv("BABAS_MAX_STEPS", "11")
    babas_main(["mcp"])
    assert [settings.max_steps for settings in seen] == [11]
    captured = capsys.readouterr()
    assert captured.out == ""


def test_mcp_command_ends_quietly_on_ctrl_c(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    async def run_stdio(settings: Settings) -> None:
        raise KeyboardInterrupt

    stub_mcp_module(monkeypatch, run_stdio=run_stdio)
    babas_main(["mcp"])
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_mcp_command_explains_a_missing_extra_found_late(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    async def run_stdio(settings: Settings) -> None:
        raise ModuleNotFoundError("No module named 'mcp'", name="mcp")

    stub_mcp_module(monkeypatch, run_stdio=run_stdio)
    with pytest.raises(SystemExit) as info:
        babas_main(["mcp"])
    assert info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "Install the MCP extra: pip install 'babas[mcp]'" in captured.err
    assert "Traceback" not in captured.err


def test_mcp_command_explains_a_missing_extra_when_the_package_is_hidden(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    hide_mcp(monkeypatch)
    with pytest.raises(SystemExit) as info:
        babas_main(["mcp"])
    assert info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "Install the MCP extra: pip install 'babas[mcp]'" in captured.err
    assert "Traceback" not in captured.err


def test_mcp_command_reports_a_busy_bridge_port(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    import errno

    async def run_stdio(settings: Settings) -> None:
        raise OSError(errno.EADDRINUSE, "address already in use")

    stub_mcp_module(monkeypatch, run_stdio=run_stdio)
    monkeypatch.setenv("BABAS_BRIDGE_PORT", "8799")
    with pytest.raises(SystemExit) as info:
        babas_main(["mcp"])
    assert info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "Port 8799 is in use" in captured.err and "serve --mcp-http" in captured.err


async def start_serve(
    capsys: pytest.CaptureFixture, settings: Settings, repl: bool = False, mcp_http: bool = False
) -> Tuple["asyncio.Task[None]", int, str]:
    task = asyncio.create_task(run_serve(settings, repl, mcp_http))
    out = ""
    deadline = time.monotonic() + WAIT
    while time.monotonic() < deadline and not task.done():
        out += capsys.readouterr().out
        found = TOKEN_RE.search(out)
        if found:
            return task, int(found.group(1)), out
        await asyncio.sleep(0.02)
    if task.done():
        task.result()
    raise AssertionError(f"serve did not announce its port: {out!r}")


async def stop_serve(task: "asyncio.Task[None]") -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_serve_pairs_and_runs_side_panel_tasks(babas_home: Path, capsys: pytest.CaptureFixture) -> None:
    task, port, out = await start_serve(capsys, serve_settings(babas_home))
    try:
        assert f"BABAS bridge on ws://127.0.0.1:{port}" in out
        assert str(babas_home / "pairing.json") in out
        async with FakeExtension(RemoteBridge(port)) as extension:
            token = extension.welcome["token"]
            assert json.loads((babas_home / "pairing.json").read_text(encoding="utf-8")) == {"token": token}
            await extension.start_task("busca recetas")
            await extension.until_state("failed")
            assert ("error", MISSING_KEY_TEXT) in extension.logs()
            assert extension.run_states()[-1] == ("failed", MISSING_KEY_TEXT)
        later = capsys.readouterr().out
        assert token not in out + later
        assert "Note: BABAS_LLM_API_KEY (or OPENAI_API_KEY) is not set" in out + later
    finally:
        await stop_serve(task)


@pytest.mark.parametrize(
    "env, expected",
    [
        ({"BABAS_LLM_BASE_URL": "http://127.0.0.1:9/v1"}, None),
        ({"OPENAI_API_KEY": "sk-test-key"}, None),
        ({"BABAS_LLM_API_KEY": "key", "BABAS_LLM_PROVIDER": "anthropic"}, "BABAS_LLM_MODEL"),
        ({"BABAS_LLM_PROVIDER": "anthropic", "BABAS_LLM_MODEL": "some-model"}, "BABAS_LLM_API_KEY (or ANTHROPIC_API_KEY)"),
    ],
)
async def test_serve_warns_only_about_what_is_really_missing(
    babas_home: Path, capsys: pytest.CaptureFixture, env: Dict[str, str], expected: Optional[str]
) -> None:
    task, _port, out = await start_serve(capsys, serve_settings(babas_home, **env))
    try:
        if expected is None:
            assert "Note:" not in out
        else:
            note = next(line for line in out.splitlines() if line.startswith("Note:"))
            assert expected in note
        assert "sk-test-key" not in out
    finally:
        await stop_serve(task)


async def test_serve_repl_does_not_start_a_session(babas_home: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    import babas.devrepl as devrepl

    blocked = asyncio.Event()

    async def fake_repl(driver: Any, bridge: Any) -> None:
        await blocked.wait()

    monkeypatch.setattr(devrepl, "_run_repl", fake_repl)
    task, port, out = await start_serve(capsys, serve_settings(babas_home), repl=True)
    try:
        async with FakeExtension(RemoteBridge(port)) as extension:
            await extension.start_task("hola")
            await asyncio.sleep(0.2)
            assert extension.requests == []
    finally:
        await stop_serve(task)


async def test_serve_uses_the_origins_and_the_pairing_file_from_the_settings(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    other = "chrome-extension://" + "a" * 32
    home = tmp_path / "other-home"
    task, port, out = await start_serve(capsys, serve_settings(home, BABAS_EXTENSION_ORIGINS=other))
    try:
        assert str(home / "pairing.json") in out
        with pytest.raises(Exception):
            async with FakeExtension(RemoteBridge(port)):
                pass
        assert not (home / "pairing.json").exists()
    finally:
        await stop_serve(task)


async def test_repl_output_masks_personal_data(capsys: pytest.CaptureFixture) -> None:
    state = make_state(
        make_element(0, "textbox", f"CURP {CURP}", tag="input", value=CURP),
        url=f"{URL}?curp={CURP}",
        title=f"Datos de {CURP}",
    )
    repl = ReplSession(FakeDriver(state, text=f"Su CURP es {CURP}"))
    await repl.run("ls")
    await repl.run("read")
    await repl.run("read 0")
    out = capsys.readouterr().out
    assert "[0] textbox" in out and "Su CURP es" in out
    assert CURP not in out


@pytest.mark.parametrize(
    "env, expected",
    [
        ({}, "BABAS_LLM_API_KEY (or OPENAI_API_KEY)"),
        ({"BABAS_LLM_PROVIDER": "anthropic", "BABAS_LLM_MODEL": "m"}, "BABAS_LLM_API_KEY (or ANTHROPIC_API_KEY)"),
        ({"BABAS_LLM_PROVIDER": "anthropic", "BABAS_LLM_API_KEY": "k"}, "BABAS_LLM_MODEL"),
        ({"OPENAI_API_KEY": "k"}, None),
        ({"BABAS_LLM_BASE_URL": "http://localhost:11434/v1"}, None),
    ],
)
def test_cli_preflight_names_the_variables_of_the_provider(env: Dict[str, str], expected: Optional[str]) -> None:
    problem = preflight_problem(Settings.from_env(env))
    if expected is None:
        assert problem is None
    else:
        assert problem is not None and problem.startswith("Error:") and expected in problem


class StubRuntime:
    def __init__(self, bridge: Any, busy: Any):
        self.bridge = bridge
        self.busy = busy
        self.lock = asyncio.Lock()
        self.attached: List[bool] = []
        self.llm_closed = 0
        self.llm = types.SimpleNamespace(close=self._close_llm)

    async def _close_llm(self) -> None:
        self.llm_closed += 1

    def attach(self, bridge: Any, *, panel_tasks: bool) -> None:
        assert bridge is self.bridge
        self.attached.append(panel_tasks)


async def test_serve_mcp_http_shares_the_bridge_and_stops_in_order(
    babas_home: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtimes: List[StubRuntime] = []
    served: List[Tuple[str, int]] = []
    events: List[str] = []

    def build_runtime(settings: Settings, bridge: Any, llm: Any = None, *, busy: Any = None) -> StubRuntime:
        runtimes.append(StubRuntime(bridge, busy))
        return runtimes[-1]

    async def serve_http(runtime: StubRuntime, host: str, port: int) -> None:
        served.append((host, port))
        try:
            await asyncio.Event().wait()
        finally:
            events.append("mcp stopped")

    stub_mcp_module(monkeypatch, build_runtime=build_runtime, serve_http=serve_http)
    settings = replace(serve_settings(babas_home), mcp_http_port=18766)
    task, port, out = await start_serve(capsys, settings, mcp_http=True)
    stopped_bridge = False
    try:
        deadline = time.monotonic() + WAIT
        while "http://127.0.0.1:18766/mcp" not in out and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
            out += capsys.readouterr().out
        assert "MCP over HTTP: http://127.0.0.1:18766/mcp" in out
        assert served == [("127.0.0.1", 18766)]
        (runtime,) = runtimes
        assert runtime.attached == [False]
        assert runtime.busy() is False

        async with FakeExtension(RemoteBridge(port)) as extension:
            await runtime.lock.acquire()
            await extension.start_task("desde el panel")
            await extension.until(lambda: ("info", BUSY_TEXT) in extension.logs(), "the busy message")
            runtime.lock.release()

            await extension.start_task("segunda")
            await extension.until_state("failed")
            assert extension.run_states()[-1] == ("failed", MISSING_KEY_TEXT)
        await stop_serve(task)
        stopped_bridge = True
        assert events == ["mcp stopped"]
        assert runtime.llm_closed == 1
    finally:
        if not stopped_bridge:
            await stop_serve(task)


async def test_serve_mcp_http_without_the_extra_exits_before_listening(
    babas_home: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    hide_mcp(monkeypatch)
    with pytest.raises(SystemExit) as info:
        await run_serve(serve_settings(babas_home), False, True)
    assert info.value.code == 1
    captured = capsys.readouterr()
    assert "Install the MCP extra: pip install 'babas[mcp]'" in captured.err
    assert "BABAS bridge on" not in captured.out


async def test_serve_mcp_http_failure_to_listen_exits_with_a_message(
    babas_home: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def build_runtime(settings: Settings, bridge: Any, llm: Any = None, *, busy: Any = None) -> StubRuntime:
        return StubRuntime(bridge, busy)

    async def serve_http(runtime: StubRuntime, host: str, port: int) -> None:
        raise OSError(98, "MCP HTTP port 18766 is not available (address already in use)")

    stub_mcp_module(monkeypatch, build_runtime=build_runtime, serve_http=serve_http)
    with pytest.raises(SystemExit) as info:
        await run_serve(serve_settings(babas_home), False, True)
    assert info.value.code == 1
    assert "Could not start the MCP HTTP endpoint" in capsys.readouterr().out


async def test_serve_refuses_repl_together_with_mcp_http(babas_home: Path) -> None:
    with pytest.raises(SystemExit) as info:
        await run_serve(serve_settings(babas_home), True, True)
    assert info.value.code == 2


@pytest.mark.parametrize("content", ["not json", "[]", "{}", '{"token": ""}'])
async def test_serve_refuses_a_corrupt_pairing_file(babas_home: Path, capsys: pytest.CaptureFixture, content: str) -> None:
    path = babas_home / "pairing.json"
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    with pytest.raises(SystemExit) as info:
        await run_serve(serve_settings(babas_home), False)
    assert info.value.code == 1
    out = capsys.readouterr().out
    assert str(path) in out and "pair --reset" in out
    assert path.read_text(encoding="utf-8") == content


async def test_serve_refuses_an_uncreatable_pairing_location(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a folder", encoding="utf-8")
    with pytest.raises(SystemExit) as info:
        await run_serve(serve_settings(blocker / "home"), False)
    assert info.value.code == 1
    assert "Cannot create the pairing folder" in capsys.readouterr().out


VOICE_ID = "voice-1"
WAV_BYTES = b"RIFF\x24\x00\x00\x00WAVEfmt " + bytes(range(200)) + b"\r\n--\r\n"
SPOKEN = "mi CURP es GOMC800101HDFRRN09 y quiero mi acta"
MAX_WAV = 3 * 1024 * 1024


def voice_payload(audio: bytes = WAV_BYTES, **changes: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "request_id": VOICE_ID,
        "audio_base64": base64.b64encode(audio).decode("ascii"),
        "mime": "audio/wav",
        "duration_ms": 1500,
    }
    for key, value in changes.items():
        if value is DROP:
            payload.pop(key, None)
        else:
            payload[key] = value
    return payload


DROP = object()


def transcripts(bridge: EventBridge) -> List[Dict[str, Any]]:
    return [payload for payload, _timeout in bridge.requests_of("ui.transcript")]


def error_of(payload: Dict[str, Any]) -> Tuple[str, str]:
    assert set(payload) == {"request_id", "error"}
    return payload["error"]["code"], payload["error"]["message"]


def voice_rig(stt: Any = None, **kwargs: Any) -> Tuple[EventBridge, Session, FakeSTT]:
    fake = stt if stt is not None else FakeSTT("hola")
    bridge = EventBridge()
    kwargs.setdefault("language", "es")
    session = Session(bridge, lambda: FakeLLM(), SafetyPolicy(), stt_factory=lambda: fake, **kwargs)
    return bridge, session, fake


async def test_dictation_returns_the_text_through_ui_transcript() -> None:
    bridge, _session, fake = voice_rig(FakeSTT("  quiero ver videos de cocina \n"))
    await bridge.fire("stt.transcribe", voice_payload())

    assert transcripts(bridge) == [{"request_id": VOICE_ID, "text": "quiero ver videos de cocina"}]
    assert [timeout for _payload, timeout in bridge.requests_of("ui.transcript")] == [10.0]
    assert fake.calls == [(len(WAV_BYTES), "audio/wav", "es")]
    assert [kind for kind, _payload, _timeout in bridge.requests] == ["ui.transcript"]


async def test_the_interface_language_is_passed_to_the_transcriber() -> None:
    bridge, _session, fake = voice_rig(language="en")
    await bridge.fire("stt.transcribe", voice_payload())
    assert fake.calls[0][2] == "en"


async def test_long_text_is_clipped_to_4000_characters() -> None:
    bridge, _session, _fake = voice_rig(FakeSTT("palabra " * 1000))
    await bridge.fire("stt.transcribe", voice_payload())
    (reply,) = transcripts(bridge)
    assert set(reply) == {"request_id", "text"} and len(reply["text"]) <= 4000


@pytest.mark.parametrize("result, expected", [("   ", "empty"), ("", "empty"), (None, "provider"), (7, "provider"), (["x"], "provider")])
async def test_unusable_transcripts_become_errors(result: Any, expected: str) -> None:
    bridge, _session, _fake = voice_rig(FakeSTT(result))
    await bridge.fire("stt.transcribe", voice_payload())
    (reply,) = transcripts(bridge)
    assert error_of(reply)[0] == expected


@pytest.mark.parametrize("code", ERROR_CODES)
async def test_transcriber_errors_are_forwarded_with_their_code_and_message(code: str) -> None:
    bridge, _session, _fake = voice_rig(FakeSTT(STTError(code, "Mensaje seguro para la persona.")))
    await bridge.fire("stt.transcribe", voice_payload())
    assert transcripts(bridge) == [{"request_id": VOICE_ID, "error": {"code": code, "message": "Mensaje seguro para la persona."}}]


async def test_an_unknown_error_code_is_reported_as_a_provider_error() -> None:
    bridge, _session, _fake = voice_rig(FakeSTT(STTError("exploded", "Algo salió mal.")))
    await bridge.fire("stt.transcribe", voice_payload())
    assert error_of(transcripts(bridge)[0]) == ("provider", "Algo salió mal.")


async def test_error_messages_are_masked_and_bounded() -> None:
    bridge, _session, _fake = voice_rig(FakeSTT(STTError("provider", f"{CURP} " + "x" * 2000)))
    await bridge.fire("stt.transcribe", voice_payload())
    code, message = error_of(transcripts(bridge)[0])
    assert code == "provider" and CURP not in message and len(message) <= 500


async def test_unexpected_exceptions_become_a_generic_provider_error(caplog: pytest.LogCaptureFixture) -> None:
    bridge, _session, _fake = voice_rig(FakeSTT(RuntimeError(f"boom {SPOKEN}")))
    with caplog.at_level(logging.DEBUG, logger="babas"):
        await bridge.fire("stt.transcribe", voice_payload())
    assert error_of(transcripts(bridge)[0]) == ("provider", PROVIDER_TEXT)
    assert "RuntimeError" in caplog.text and CURP not in caplog.text and CURP not in json.dumps(transcripts(bridge))


BAD_PAYLOADS = {
    "no audio": {"audio_base64": DROP},
    "audio none": {"audio_base64": None},
    "audio number": {"audio_base64": 12},
    "audio list": {"audio_base64": ["QUJD"]},
    "audio bytes": {"audio_base64": b"QUJD"},
    "audio empty": {"audio_base64": ""},
    "audio blanks": {"audio_base64": "    "},
    "audio not base64": {"audio_base64": "@@@@"},
    "audio bad padding": {"audio_base64": "QUJDRA"},
    "audio newline": {"audio_base64": "QUJD\nREVG"},
    "audio space": {"audio_base64": "QUJD REVG"},
    "audio url safe": {"audio_base64": "_-_-"},
    "audio after padding": {"audio_base64": "QUJD=QUJD"},
    "audio non ascii": {"audio_base64": "QUJDé"},
    "audio too many chars": {"audio_base64": "A" * (4 * ((MAX_WAV + 2) // 3) + 4)},
    "no mime": {"mime": DROP},
    "mime none": {"mime": None},
    "mime number": {"mime": 5},
    "mime webm": {"mime": "audio/webm"},
    "mime upper": {"mime": "AUDIO/WAV"},
    "mime params": {"mime": "audio/wav; codecs=1"},
    "mime x-wav": {"mime": "audio/x-wav"},
    "no duration": {"duration_ms": DROP},
    "duration none": {"duration_ms": None},
    "duration zero": {"duration_ms": 0},
    "duration negative": {"duration_ms": -5},
    "duration too long": {"duration_ms": 30001},
    "duration huge": {"duration_ms": 10**12},
    "duration float": {"duration_ms": 1500.5},
    "duration whole float": {"duration_ms": 1500.0},
    "duration text": {"duration_ms": "1500"},
    "duration bool": {"duration_ms": True},
}


@pytest.mark.parametrize("changes", BAD_PAYLOADS.values(), ids=list(BAD_PAYLOADS))
async def test_a_bad_payload_gets_bad_audio_and_never_reaches_the_transcriber(changes: Dict[str, Any]) -> None:
    bridge, _session, fake = voice_rig()
    await bridge.fire("stt.transcribe", voice_payload(**changes))
    assert [error_of(reply) for reply in transcripts(bridge)] == [("bad_audio", BAD_AUDIO_TEXT)]
    assert transcripts(bridge)[0]["request_id"] == VOICE_ID
    assert fake.calls == []


async def test_audio_over_three_mebibytes_is_refused_and_exactly_three_is_accepted() -> None:
    bridge, _session, fake = voice_rig()
    await bridge.fire("stt.transcribe", voice_payload(b"\x01" * (MAX_WAV + 1)))
    assert error_of(transcripts(bridge)[0])[0] == "bad_audio" and fake.calls == []

    await bridge.fire("stt.transcribe", voice_payload(b"\x01" * MAX_WAV, request_id="voice-2"))
    assert transcripts(bridge)[1] == {"request_id": "voice-2", "text": "hola"}
    assert fake.calls == [(MAX_WAV, "audio/wav", "es")]


@pytest.mark.parametrize("duration", [1, 30000])
async def test_the_duration_bounds_are_inclusive(duration: int) -> None:
    bridge, _session, fake = voice_rig()
    await bridge.fire("stt.transcribe", voice_payload(duration_ms=duration))
    assert transcripts(bridge) == [{"request_id": VOICE_ID, "text": "hola"}] and len(fake.calls) == 1


@pytest.mark.parametrize(
    "payload",
    [
        voice_payload(request_id=DROP),
        voice_payload(request_id=""),
        voice_payload(request_id=5),
        voice_payload(request_id=None),
        voice_payload(request_id=["a"]),
        voice_payload(request_id="x" * 129),
        {},
        "texto",
        ["x"],
        7,
    ],
    ids=["missing", "empty", "number", "none", "list", "too long", "empty payload", "string", "array", "integer"],
)
async def test_a_request_without_a_usable_id_is_ignored_without_raising(payload: Any) -> None:
    bridge, _session, fake = voice_rig()
    await bridge.fire("stt.transcribe", payload)
    assert bridge.requests == [] and fake.calls == []


async def test_the_longest_request_id_is_answered_with_that_id() -> None:
    bridge, _session, _fake = voice_rig()
    await bridge.fire("stt.transcribe", voice_payload(request_id="r" * 128))
    assert transcripts(bridge) == [{"request_id": "r" * 128, "text": "hola"}]


async def test_a_second_request_gets_busy_while_one_is_running() -> None:
    gate = asyncio.Event()
    bridge, _session, fake = voice_rig(FakeSTT("primera", gate=gate))
    first = asyncio.create_task(bridge.fire("stt.transcribe", voice_payload(request_id="voice-a")))
    await asyncio.wait_for(fake.started.wait(), WAIT)

    await bridge.fire("stt.transcribe", voice_payload(request_id="voice-b"))
    assert [(item["request_id"], error_of(item)) for item in transcripts(bridge)] == [("voice-b", ("busy", STT_BUSY_TEXT))]
    assert len(fake.calls) == 1 and not first.done()

    gate.set()
    await asyncio.wait_for(first, WAIT)
    assert transcripts(bridge)[1] == {"request_id": "voice-a", "text": "primera"}

    await bridge.fire("stt.transcribe", voice_payload(request_id="voice-c"))
    assert transcripts(bridge)[2] == {"request_id": "voice-c", "text": "primera"}


async def test_a_failed_transcription_frees_the_slot() -> None:
    fake = FakeSTT(lambda audio, mime, language: "bien")
    bridge, _session, _ = voice_rig(fake)
    fake.result = STTError("provider", "Falló.")
    await bridge.fire("stt.transcribe", voice_payload(request_id="voice-a"))
    fake.result = "bien"
    await bridge.fire("stt.transcribe", voice_payload(request_id="voice-b"))
    assert [item["request_id"] for item in transcripts(bridge)] == ["voice-a", "voice-b"]
    assert error_of(transcripts(bridge)[0]) == ("provider", "Falló.") and transcripts(bridge)[1]["text"] == "bien"


async def test_a_session_without_a_speech_backend_answers_not_configured() -> None:
    bridge = EventBridge()
    Session(bridge, lambda: FakeLLM(), SafetyPolicy())
    await bridge.fire("stt.transcribe", voice_payload())
    assert [error_of(reply) for reply in transcripts(bridge)] == [("not_configured", NOT_CONFIGURED_TEXT)]


async def test_a_factory_configuration_error_is_forwarded_and_retried_next_time() -> None:
    built: List[FakeSTT] = []

    def factory() -> FakeSTT:
        if not built:
            built.append(FakeSTT("listo"))
            raise STTConfigError("Falta algo en la configuración.")
        return built[0]

    bridge = EventBridge()
    Session(bridge, lambda: FakeLLM(), SafetyPolicy(), stt_factory=factory)
    await bridge.fire("stt.transcribe", voice_payload(request_id="voice-a"))
    await bridge.fire("stt.transcribe", voice_payload(request_id="voice-b"))
    assert error_of(transcripts(bridge)[0]) == ("not_configured", "Falta algo en la configuración.")
    assert transcripts(bridge)[1] == {"request_id": "voice-b", "text": "listo"}


async def test_a_crashing_factory_is_a_provider_error_without_its_text(caplog: pytest.LogCaptureFixture) -> None:
    def factory() -> FakeSTT:
        raise RuntimeError(f"cannot build for {SPOKEN}")

    bridge = EventBridge()
    Session(bridge, lambda: FakeLLM(), SafetyPolicy(), stt_factory=factory)
    with caplog.at_level(logging.DEBUG, logger="babas"):
        await bridge.fire("stt.transcribe", voice_payload())
    assert error_of(transcripts(bridge)[0]) == ("provider", PROVIDER_TEXT)
    assert "RuntimeError" in caplog.text and CURP not in caplog.text and CURP not in json.dumps(transcripts(bridge))


async def test_the_transcriber_is_reused_and_closed_when_the_session_stops() -> None:
    built: List[FakeSTT] = []

    def factory() -> FakeSTT:
        built.append(FakeSTT("hola"))
        return built[-1]

    bridge = EventBridge()
    session = Session(bridge, lambda: FakeLLM(), SafetyPolicy(), stt_factory=factory)
    await bridge.fire("stt.transcribe", voice_payload(request_id="voice-a"))
    await bridge.fire("stt.transcribe", voice_payload(request_id="voice-b"))
    assert len(built) == 1 and built[0].closed == 0 and len(built[0].calls) == 2

    await session.stop()
    assert built[0].closed == 1
    await session.stop()
    assert built[0].closed == 1

    await bridge.fire("stt.transcribe", voice_payload(request_id="voice-c"))
    assert len(built) == 2 and transcripts(bridge)[-1] == {"request_id": "voice-c", "text": "hola"}


async def test_stop_cancels_a_running_transcription_without_answering_it() -> None:
    gate = asyncio.Event()
    bridge, session, fake = voice_rig(FakeSTT("nunca", gate=gate))
    task = asyncio.create_task(bridge.fire("stt.transcribe", voice_payload()))
    await asyncio.wait_for(fake.started.wait(), WAIT)

    await session.stop()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fake.closed == 1 and transcripts(bridge) == []

    fresh = FakeSTT("otra")
    session.stt_factory = lambda: fresh
    await bridge.fire("stt.transcribe", voice_payload(request_id="voice-b"))
    assert transcripts(bridge) == [{"request_id": "voice-b", "text": "otra"}]


@pytest.mark.parametrize(
    "failure",
    [DriverError(ErrorCode.NO_EXTENSION, "The extension disconnected"), DriverError(ErrorCode.TIMEOUT, "slow"), RuntimeError("weird"), ValueError("bad")],
)
async def test_a_reply_that_cannot_be_delivered_is_swallowed(failure: Exception, caplog: pytest.LogCaptureFixture) -> None:
    bridge, _session, fake = voice_rig()
    bridge.responses["ui.transcript"] = failure
    with caplog.at_level(logging.DEBUG, logger="babas"):
        await bridge.fire("stt.transcribe", voice_payload())
        await bridge.fire("stt.transcribe", voice_payload(request_id="voice-b"))
        await bridge.fire("stt.transcribe", voice_payload(audio_base64="@@", request_id="voice-c"))
    assert len(bridge.requests_of("ui.transcript")) == 3 and len(fake.calls) == 2
    assert "could not deliver the transcript" in caplog.text
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


async def test_nothing_private_is_logged_for_voice_requests(caplog: pytest.LogCaptureFixture) -> None:
    gate = asyncio.Event()
    fake = FakeSTT(SPOKEN, gate=gate)
    bridge, _session, _ = voice_rig(fake)
    encoded = base64.b64encode(WAV_BYTES).decode("ascii")
    with caplog.at_level(logging.DEBUG):
        first = asyncio.create_task(bridge.fire("stt.transcribe", voice_payload(request_id="voice-a")))
        await asyncio.wait_for(fake.started.wait(), WAIT)
        await bridge.fire("stt.transcribe", voice_payload(request_id="voice-b"))
        await bridge.fire("stt.transcribe", voice_payload(audio_base64="@@@@", request_id="voice-c"))
        gate.set()
        await first
        fake.result = STTError("provider", f"fallo con {SPOKEN}")
        await bridge.fire("stt.transcribe", voice_payload(request_id="voice-d"))
        fake.result = RuntimeError(SPOKEN)
        await bridge.fire("stt.transcribe", voice_payload(request_id="voice-e"))
    text = caplog.text
    assert "voice transcription started" in text and "voice transcription finished" in text
    assert SPOKEN not in text and CURP not in text and encoded not in text and encoded[:40] not in text and "RIFF" not in text


async def test_dictation_does_not_depend_on_the_run_state_or_other_controllers() -> None:
    bridge, session, fake = voice_rig(busy=lambda: True)
    await bridge.fire("stt.transcribe", voice_payload())
    assert transcripts(bridge) == [{"request_id": VOICE_ID, "text": "hola"}]
    assert not session.running and fake.calls


async def test_dictation_round_trip_over_the_real_bridge(bridge: BridgeServer, ext: FakeExtension) -> None:
    fake = FakeSTT("busca recetas de pozole")
    make_session(bridge, FakeLLM(), stt_factory=lambda: fake, language="es")

    await ext.send_event("stt.transcribe", voice_payload())
    await ext.until(lambda: bool(ext.of("ui.transcript")), "the transcript")
    assert ext.of("ui.transcript") == [{"request_id": VOICE_ID, "text": "busca recetas de pozole"}]
    assert fake.calls == [(len(WAV_BYTES), "audio/wav", "es")]

    await ext.send_event("stt.transcribe", voice_payload(mime="audio/webm", request_id="voice-2"))
    await ext.until(lambda: len(ext.of("ui.transcript")) == 2, "the error")
    assert error_of(ext.of("ui.transcript")[1]) == ("bad_audio", BAD_AUDIO_TEXT)
    assert ext.of("ui.run_state") == [] and ext.of("ui.log") == []


async def test_dictation_while_a_task_runs_leaves_the_task_alone(bridge: BridgeServer, ext: FakeExtension) -> None:
    llm = FakeLLM(delay=30)
    fake = FakeSTT("hola")
    session = make_session(bridge, llm, stt_factory=lambda: fake)

    await ext.start_task("tarea larga")
    await ext.until_state("running")
    await ext.until(lambda: bool(ext.of("page.state")), "the run to wait on the model")
    await ext.send_event("stt.transcribe", voice_payload())
    await ext.until(lambda: bool(ext.of("ui.transcript")), "the transcript")

    assert ext.of("ui.transcript") == [{"request_id": VOICE_ID, "text": "hola"}]
    assert session.running and ext.run_states() == [("running", "")]
    assert ("info", BUSY_TEXT) not in ext.logs()

    await ext.stop()
    await ext.until_state("stopped")
    await settled(session)
    await ext.send_event("stt.transcribe", voice_payload(request_id="voice-2"))
    await ext.until(lambda: len(ext.of("ui.transcript")) == 2, "the second transcript")


async def test_stop_and_new_tasks_do_not_disturb_a_transcription_in_progress(bridge: BridgeServer, ext: FakeExtension) -> None:
    gate = asyncio.Event()
    fake = FakeSTT("seguía escuchando", gate=gate)
    session = make_session(bridge, FakeLLM(delay=30), stt_factory=lambda: fake)

    await ext.send_event("stt.transcribe", voice_payload())
    await asyncio.wait_for(fake.started.wait(), WAIT)
    await ext.start_task("tarea larga")
    await ext.until_state("running")
    await ext.stop()
    await ext.until_state("stopped")
    await settled(session)
    assert ext.of("ui.transcript") == []

    await ext.send_event("stt.transcribe", voice_payload(request_id="voice-2"))
    await ext.until(lambda: bool(ext.of("ui.transcript")), "the busy answer")
    assert error_of(ext.of("ui.transcript")[0]) == ("busy", STT_BUSY_TEXT)

    gate.set()
    await ext.until(lambda: len(ext.of("ui.transcript")) == 2, "the first transcript")
    assert ext.of("ui.transcript")[1] == {"request_id": VOICE_ID, "text": "seguía escuchando"}
