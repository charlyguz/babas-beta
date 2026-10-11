import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Tuple, Union

import pytest
from helpers import FakeLLM, make_element, make_state
from websockets.asyncio.client import ClientConnection, connect

import babas.session as session_module
from babas.__main__ import main as babas_main
from babas.agent import DENIED_ANSWER, Agent
from babas.bridge.protocol import PINNED_EXTENSION_ID, PROTOCOL_VERSION, new_id
from babas.bridge.server import BridgeServer, default_pairing_file
from babas.devrepl import run_serve
from babas.drivers.base import BrowserDriver, DriverError, ErrorCode, PageState
from babas.llm import LLMError
from babas.safety import SafetyPolicy
from babas.session import BUSY_TEXT, DISCONNECTED_TEXT, FAILED_TEXT, MISSING_KEY_TEXT, Session

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
    session = make_session(bridge, llm, language="es", max_steps=7)
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
    assert [(item["language"], item["max_steps"]) for item in agents] == [("es", 7)]
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


async def test_missing_key_when_the_factory_fails(bridge: BridgeServer, ext: FakeExtension) -> None:
    def factory() -> Any:
        raise LLMError("OPENAI_API_KEY is not set. Put it in .env or the environment.")

    session = make_session(bridge, factory)
    await ext.start_task("busca recetas")
    await ext.until_state("failed")
    await settled(session)

    assert MISSING_KEY_TEXT == "Falta configurar el modelo de IA (OPENAI_API_KEY)."
    assert ("error", MISSING_KEY_TEXT) in ext.logs()
    assert ext.run_states()[-1] == ("failed", MISSING_KEY_TEXT)


async def test_missing_key_on_the_first_model_call_is_not_retried(bridge: BridgeServer, ext: FakeExtension) -> None:
    llm = FakeLLM([LLMError("OPENAI_API_KEY is not set. Put it in .env or the environment.")] * 10)
    session = make_session(bridge, llm)
    await ext.start_task("busca recetas")
    await ext.until_state("failed")
    await settled(session)

    assert len(llm.calls) == 1
    assert ("error", MISSING_KEY_TEXT) in ext.logs()
    assert ext.run_states() == [("running", ""), ("failed", MISSING_KEY_TEXT)]


async def test_other_model_errors_do_not_look_like_a_missing_key(bridge: BridgeServer, ext: FakeExtension) -> None:
    llm = FakeLLM([LLMError("The LLM provider rejected the API key (HTTP 401).")] * 10)
    session = make_session(bridge, llm)
    await ext.start_task("busca recetas")
    await ext.until_state("failed")
    await settled(session)

    assert all(text != MISSING_KEY_TEXT for _, text in ext.logs())
    assert ext.run_states()[-1][0] == "failed"


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


@pytest.fixture
def babas_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "babas-home"
    monkeypatch.setenv("BABAS_HOME", str(home))
    return home


def test_pair_reports_the_state_without_the_token(babas_home: Path, capsys: pytest.CaptureFixture) -> None:
    path = default_pairing_file()
    assert path == babas_home / "pairing.json"

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
    path = default_pairing_file()
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
    path = default_pairing_file()
    path.mkdir(parents=True)
    with pytest.raises(SystemExit) as info:
        babas_main(["pair", "--reset"])
    assert info.value.code == 1
    assert "Could not remove the pairing file" in capsys.readouterr().out


async def start_serve(capsys: pytest.CaptureFixture, repl: bool = False) -> Tuple["asyncio.Task[None]", int, str]:
    task = asyncio.create_task(run_serve("127.0.0.1", 0, repl))
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


async def test_serve_pairs_and_runs_side_panel_tasks(
    babas_home: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    task, port, out = await start_serve(capsys)
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
        assert "Note: OPENAI_API_KEY is not set" in out + later
    finally:
        await stop_serve(task)


async def test_serve_repl_does_not_start_a_session(babas_home: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    import babas.devrepl as devrepl

    blocked = asyncio.Event()

    async def fake_repl(driver: Any, bridge: Any) -> None:
        await blocked.wait()

    monkeypatch.setattr(devrepl, "_run_repl", fake_repl)
    task, port, out = await start_serve(capsys, repl=True)
    try:
        async with FakeExtension(RemoteBridge(port)) as extension:
            await extension.start_task("hola")
            await asyncio.sleep(0.2)
            assert extension.requests == []
    finally:
        await stop_serve(task)


@pytest.mark.parametrize("content", ["not json", "[]", "{}", '{"token": ""}'])
async def test_serve_refuses_a_corrupt_pairing_file(babas_home: Path, capsys: pytest.CaptureFixture, content: str) -> None:
    path = babas_home / "pairing.json"
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    with pytest.raises(SystemExit) as info:
        await run_serve("127.0.0.1", 0, False)
    assert info.value.code == 1
    out = capsys.readouterr().out
    assert str(path) in out and "pair --reset" in out
    assert path.read_text(encoding="utf-8") == content


async def test_serve_refuses_an_uncreatable_pairing_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a folder", encoding="utf-8")
    monkeypatch.setenv("BABAS_HOME", str(blocker / "home"))
    with pytest.raises(SystemExit) as info:
        await run_serve("127.0.0.1", 0, False)
    assert info.value.code == 1
    assert "Cannot create the pairing folder" in capsys.readouterr().out
