import asyncio
import json
import logging
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from helpers import FakeBridge, FakeDriver, FakeLLM, RecordingChannel, make_element, make_state
from mcp import Client

from babas.bridge.server import BridgeServer
from babas.config import Settings
from babas.drivers.base import DriverError, ErrorCode, PageState
from babas.drivers.extension_driver import ExtensionDriver
from babas.gate import SafeDriver
from babas.llm import LLMConfigError
from babas.logs import setup_logging
from babas.mcp_server import McpRuntime, _transport_security, build_mcp_server, build_runtime, serve_http
from babas.safety import SafetyPolicy

CURP = "GOMC800101HDFRRN09"
MASKED_CURP = "GO**************09"
SETTINGS = Settings(max_steps=5, language="es")
WAIT = 3.0


def page_state() -> PageState:
    return make_state(
        make_element(0, "textbox", "Buscar", tag="input", flags=["search_form"]),
        make_element(1, "textbox", "CURP", tag="input", flags=["pii"], value=CURP),
        make_element(2, "textbox", "Contraseña", tag="input", type="password", flags=["password"], value="secreto"),
        make_element(3, "link", "Inicio"),
        make_element(4, "button", "Pagar", flags=["consequential"]),
    )


class EventBridge(FakeBridge):
    def __init__(self) -> None:
        super().__init__()
        self.handlers: Dict[str, List[Any]] = {}

    def on(self, event_type: str, handler: Any) -> None:
        self.handlers.setdefault(event_type, []).append(handler)

    async def fire(self, event_type: str, payload: Optional[Dict[str, Any]] = None) -> None:
        for handler in self.handlers.get(event_type, []):
            await handler(payload or {})


class BlockingLLM:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.cancelled = False
        self.calls = 0

    async def complete_json(self, system: str, user: str, schema: Optional[dict] = None) -> dict:
        self.calls += 1
        self.entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return {}


def make_rig(
    *,
    state: Optional[PageState] = None,
    channel: Optional[RecordingChannel] = None,
    llm: Any = None,
    busy: Any = None,
    panel_tasks: bool = True,
    **driver_kwargs: Any,
) -> SimpleNamespace:
    driver = FakeDriver(state if state is not None else page_state(), **driver_kwargs)
    chan = channel if channel is not None else RecordingChannel(mint_ids=True)
    model = llm if llm is not None else FakeLLM()
    policy = SafetyPolicy()
    runtime = McpRuntime(SETTINGS, lambda: SafeDriver(driver, policy, chan), model, chan, busy=busy)
    bridge = EventBridge()
    runtime.attach(bridge, panel_tasks=panel_tasks)
    return SimpleNamespace(driver=driver, channel=chan, llm=model, runtime=runtime, bridge=bridge, server=build_mcp_server(runtime))


def text(result: Any) -> str:
    return result.content[0].text


def act_args(**action: Any) -> Dict[str, Any]:
    return {"action": action}


async def test_exactly_three_tools_with_general_instructions():
    rig = make_rig()
    async with Client(rig.server) as client:
        tools = (await client.list_tools()).tools
        instructions = client.instructions or ""
    assert sorted(tool.name for tool in tools) == ["act", "get_page_state", "run_task"]
    assert all(tool.description for tool in tools)
    assert "real Chrome" in instructions and "side panel" in instructions and "CAPTCHA" in instructions
    assert "cannot skip" in instructions


async def test_act_schema_has_discriminator_and_23_actions():
    rig = make_rig()
    async with Client(rig.server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    action = tools["act"].input_schema["properties"]["action"]
    assert action["discriminator"]["propertyName"] == "action"
    assert len(action["oneOf"]) == 23
    assert "done" not in action["discriminator"]["mapping"]
    assert tools["act"].input_schema["required"] == ["action"]
    assert tools["run_task"].input_schema["required"] == ["task"]


async def test_get_page_state_returns_elements_masked_values_and_prompt():
    rig = make_rig()
    async with Client(rig.server) as client:
        res = await client.call_tool("get_page_state", {})
    data = res.structured_content
    assert not res.is_error
    assert data["snapshot_id"] == "s1" and data["url"].startswith("https://")
    assert [el["index"] for el in data["elements"]] == [0, 1, 2, 3, 4]
    assert data["elements_total"] == 5 and data["elements_omitted"] == 0
    by_index = {el["index"]: el for el in data["elements"]}
    assert by_index[1]["value"] == MASKED_CURP
    assert by_index[2]["value"] is None
    assert CURP not in json.dumps(data) and "secreto" not in json.dumps(data)
    assert data["prompt"].startswith("URL: ") and "ELEMENTS" in data["prompt"]
    assert rig.runtime.last_state is not None and rig.runtime.last_state.snapshot_id == "s1"


async def test_get_page_state_caps_elements_and_says_so():
    many = make_state(*[make_element(i, "link", f"Enlace {i}", in_viewport=i < 40) for i in range(200)])
    rig = make_rig(state=many)
    async with Client(rig.server) as client:
        res = await client.call_tool("get_page_state", {})
    data = res.structured_content
    assert len(data["elements"]) == 150
    assert data["elements_total"] == 200 and data["elements_omitted"] == 50
    assert [el["index"] for el in data["elements"]] == list(range(150))
    assert "50 more not shown" in data["prompt"]
    assert len(rig.runtime.last_state.elements) == 200


async def test_get_page_state_on_a_site_outside_the_list_waits_for_the_user():
    state = make_state(make_element(0, "link", "Inicio"), url="https://example.org/")
    allowed = make_rig(state=state, channel=RecordingChannel(allow=True, mint_ids=True))
    async with Client(allowed.server) as client:
        res = await client.call_tool("get_page_state", {})
    assert not res.is_error
    assert allowed.channel.allows[0][0] == "example.org"

    refused = make_rig(state=state, channel=RecordingChannel(allow=False))
    async with Client(refused.server) as client:
        res = await client.call_tool("get_page_state", {})
    assert res.is_error and "declined" in text(res)
    assert refused.runtime.last_state is None


async def test_act_typing_pii_waits_for_the_approval_then_succeeds():
    rig = make_rig(channel=RecordingChannel(delay=0.2, mint_ids=True))
    async with Client(rig.server) as client:
        await client.call_tool("get_page_state", {})
        started = time.monotonic()
        res = await client.call_tool("act", act_args(action="type_text", index=1, value=CURP))
        elapsed = time.monotonic() - started
    assert not res.is_error
    assert elapsed >= 0.15
    assert res.structured_content["ok"] is True
    assert res.structured_content["url"] == rig.driver.url
    assert len(rig.channel.confirms) == 1 and "pii_field" in rig.channel.confirms[0].reasons
    ((args, kwargs),) = rig.driver.calls_of("type_text")
    assert args[1] == CURP and kwargs["confirmation"] == "conf-1"


async def test_act_denial_is_an_error_that_says_declined():
    rig = make_rig(channel=RecordingChannel(approve=False))
    async with Client(rig.server) as client:
        await client.call_tool("get_page_state", {})
        res = await client.call_tool("act", act_args(action="type_text", index=1, value=CURP))
    assert res.is_error
    assert "The user declined this action in BABAS" in text(res)
    assert rig.driver.calls_of("type_text") == []


async def test_act_password_field_asks_for_a_handoff():
    rig = make_rig()
    async with Client(rig.server) as client:
        await client.call_tool("get_page_state", {})
        res = await client.call_tool("act", act_args(action="type_text", index=2, value="mi clave"))
    assert res.is_error and "Hand-off needed" in text(res) and "contraseñas" in text(res)
    assert rig.driver.calls_of("type_text") == []
    assert any(kind == "info" and "contraseñas" in line for kind, line, _ in rig.channel.logs)
    assert "mi clave" not in text(res)


async def test_act_with_an_index_needs_a_page_state_first():
    rig = make_rig()
    async with Client(rig.server) as client:
        res = await client.call_tool("act", act_args(action="click", index=3))
        assert res.is_error and "get_page_state" in text(res)
        res = await client.call_tool("act", act_args(action="goto", value="https://www.gob.mx/tramites"))
        assert not res.is_error
        await client.call_tool("get_page_state", {})
        res = await client.call_tool("act", act_args(action="scroll", value="down"))
        assert not res.is_error and rig.runtime.last_state is not None
        res = await client.call_tool("act", act_args(action="click", index=3))
        assert not res.is_error
        assert rig.runtime.last_state is None
        res = await client.call_tool("act", act_args(action="click", index=3))
        assert res.is_error and "get_page_state" in text(res)
    assert len(rig.driver.calls_of("click")) == 1
    assert len(rig.driver.calls_of("get_page_state")) == 1


async def test_act_reports_the_url_and_title_the_extension_sees():
    bridge = FakeBridge({"tab.info": {"tab_id": 1, "url": "https://www.gob.mx/tramites", "title": "Trámites"}})
    channel = RecordingChannel(mint_ids=True)
    policy = SafetyPolicy()
    runtime = McpRuntime(SETTINGS, lambda: SafeDriver(ExtensionDriver(bridge), policy, channel), FakeLLM(), channel)
    async with Client(build_mcp_server(runtime)) as client:
        res = await client.call_tool("act", act_args(action="scroll", value="down"))
    assert not res.is_error
    assert res.structured_content == {"ok": True, "detail": "", "url": "https://www.gob.mx/tramites", "title": "Trámites"}
    assert bridge.requests_of("page.act")


async def test_act_stale_snapshot_tells_the_client_to_read_the_page_again():
    stale = DriverError(ErrorCode.STALE_SNAPSHOT, "The page changed")
    rig = make_rig(errors={"click": stale})
    async with Client(rig.server) as client:
        await client.call_tool("get_page_state", {})
        res = await client.call_tool("act", act_args(action="click", index=3))
    assert res.is_error
    assert "STALE_SNAPSHOT" in text(res) and "get_page_state again" in text(res)
    assert rig.runtime.last_state is None


async def test_act_errors_never_echo_typed_personal_data():
    rig = make_rig()
    async with Client(rig.server) as client:
        res = await client.call_tool("act", act_args(action="smart_click", value=CURP))
    assert res.is_error and "NOT_FOUND" in text(res)
    assert CURP not in text(res)


async def test_act_result_detail_is_redacted():
    rig = make_rig(text=f"Su CURP es {CURP}.")
    async with Client(rig.server) as client:
        res = await client.call_tool("act", act_args(action="read_text"))
    assert not res.is_error
    assert MASKED_CURP in res.structured_content["detail"] and CURP not in res.structured_content["detail"]


async def test_invalid_act_arguments_are_errors_and_touch_nothing():
    rig = make_rig()
    bad = [
        {"action": {"action": "nope"}},
        {"action": {"action": "click"}},
        {"action": {"action": "click", "index": "abc"}},
        {"action": {"action": "goto", "value": "ftp://example.com"}},
        {"action": {"action": "done", "value": "x"}},
        {},
    ]
    async with Client(rig.server) as client:
        for arguments in bad:
            res = await client.call_tool("act", arguments)
            assert res.is_error, arguments
        res = await client.call_tool("run_task", {})
        assert res.is_error
    assert rig.driver.calls == []


async def test_run_task_returns_done_with_the_answer_and_steps():
    plan = {"steps": [{"action": "scroll", "value": "down"}, {"action": "done", "value": "Listo, ya lo hice."}]}
    rig = make_rig(llm=FakeLLM([plan]))
    progress: List[Any] = []

    async def on_progress(value: float, total: Optional[float], message: Optional[str]) -> None:
        progress.append((value, total, message))

    async with Client(rig.server) as client:
        await client.call_tool("get_page_state", {})
        assert rig.runtime.last_state is not None
        res = await client.call_tool("run_task", {"task": "baja la página"}, progress_callback=on_progress)
    assert not res.is_error
    assert rig.runtime.last_state is None
    assert res.structured_content == {"status": "done", "answer": "Listo, ya lo hice.", "steps": 1}
    assert [state for state, _ in rig.channel.states][0] == "running"
    assert rig.channel.states[-1] == ("done", "Listo, ya lo hice.")
    assert progress and progress[-1][0] == 1 and progress[-1][1] == SETTINGS.max_steps
    assert rig.driver.calls_of("scroll")


async def test_run_task_goes_through_the_same_gate():
    plan = {"steps": [{"action": "type_text", "index": 1, "value": CURP}, {"action": "done", "value": "Listo."}]}
    rig = make_rig(llm=FakeLLM([plan]), channel=RecordingChannel(approve=False))
    async with Client(rig.server) as client:
        res = await client.call_tool("run_task", {"task": "escribe mi curp"})
    assert not res.is_error
    assert res.structured_content["status"] == "stopped"
    assert len(rig.channel.confirms) == 1
    assert rig.driver.calls_of("type_text") == []


async def test_run_task_rejects_blank_tasks():
    rig = make_rig()
    async with Client(rig.server) as client:
        res = await client.call_tool("run_task", {"task": "   "})
    assert res.is_error and "empty" in text(res)
    assert rig.llm.calls == []


async def test_tools_are_refused_while_the_side_panel_session_is_busy():
    rig = make_rig(busy=lambda: True)
    async with Client(rig.server) as client:
        results = [
            await client.call_tool("run_task", {"task": "busca recetas"}),
            await client.call_tool("get_page_state", {}),
            await client.call_tool("act", act_args(action="scroll", value="down")),
        ]
    for res in results:
        assert res.is_error and "side panel" in text(res)
    assert rig.driver.calls == [] and rig.llm.calls == []


async def test_missing_model_configuration_is_a_tool_error_naming_the_variable():
    message = "No API key is set: put BABAS_LLM_API_KEY (or OPENAI_API_KEY) in .env or the environment."
    rig = make_rig(llm=FakeLLM([LLMConfigError(message)]))
    async with Client(rig.server) as client:
        res = await client.call_tool("run_task", {"task": "busca recetas"})
    assert res.is_error and "BABAS_LLM_API_KEY" in text(res)
    assert len(rig.llm.calls) == 1
    assert rig.channel.states[-1][0] == "failed"
    assert not rig.runtime.lock.locked()


async def test_build_runtime_shares_site_approvals_between_calls():
    page = {"snapshot_id": "s1", "url": "https://example.org/", "title": "Ejemplo", "elements": []}
    bridge = FakeBridge(
        {
            "tab.info": {"tab_id": 1, "url": "https://example.org/", "title": "Ejemplo"},
            "page.state": page,
            "ui.allow_site": {"approved": True},
        }
    )
    runtime = build_runtime(SETTINGS, bridge, FakeLLM())
    async with Client(build_mcp_server(runtime)) as client:
        first = await client.call_tool("get_page_state", {})
        second = await client.call_tool("get_page_state", {})
    assert not first.is_error and not second.is_error
    assert len(bridge.requests_of("ui.allow_site")) == 1


async def test_driver_failure_is_a_tool_error_with_its_code():
    bridge = BridgeServer("127.0.0.1", 0)
    runtime = build_runtime(SETTINGS, bridge, FakeLLM())
    async with Client(build_mcp_server(runtime)) as client:
        state = await client.call_tool("get_page_state", {})
        task = await client.call_tool("run_task", {"task": "busca recetas"})
    assert state.is_error and "NO_EXTENSION" in text(state)
    assert task.is_error and "NO_EXTENSION" in text(task)
    assert not runtime.lock.locked()


async def test_panel_stop_cancels_the_run_and_returns_stopped():
    llm = BlockingLLM()
    rig = make_rig(llm=llm)
    async with Client(rig.server) as client:
        call = asyncio.create_task(client.call_tool("run_task", {"task": "espera"}))
        await asyncio.wait_for(llm.entered.wait(), WAIT)
        assert rig.runtime.active
        await rig.bridge.fire("run.stop")
        res = await asyncio.wait_for(call, WAIT)
    assert not res.is_error
    assert res.structured_content["status"] == "stopped" and res.structured_content["steps"] == 0
    assert res.structured_content["answer"]
    assert llm.cancelled
    assert rig.channel.states[-1][0] == "stopped"
    assert not rig.runtime.lock.locked()


async def test_panel_stop_with_nothing_running_does_nothing():
    rig = make_rig()
    await rig.bridge.fire("run.stop")
    assert rig.channel.states == [] and not rig.runtime.lock.locked()


async def test_cancelling_the_mcp_request_cancels_the_inner_run():
    llm = BlockingLLM()
    rig = make_rig(llm=llm)
    call = asyncio.create_task(rig.runtime.run_task("espera"))
    await asyncio.wait_for(llm.entered.wait(), WAIT)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert llm.cancelled
    assert not rig.runtime.lock.locked()
    await asyncio.sleep(0)
    assert not rig.runtime._runs
    assert rig.channel.states[-1][0] == "stopped"


async def test_one_browser_call_at_a_time():
    llm = BlockingLLM()
    rig = make_rig(llm=llm)
    async with Client(rig.server) as client:
        call = asyncio.create_task(client.call_tool("run_task", {"task": "espera"}))
        await asyncio.wait_for(llm.entered.wait(), WAIT)
        observed = len(rig.driver.calls_of("get_page_state"))
        waiting = asyncio.create_task(client.call_tool("get_page_state", {}))
        await asyncio.sleep(0.1)
        assert not waiting.done() and len(rig.driver.calls_of("get_page_state")) == observed
        await rig.bridge.fire("run.stop")
        await asyncio.wait_for(call, WAIT)
        res = await asyncio.wait_for(waiting, WAIT)
    assert not res.is_error and len(rig.driver.calls_of("get_page_state")) == observed + 1


async def test_panel_tasks_are_answered_only_when_the_runtime_owns_the_panel():
    owner = make_rig(panel_tasks=True)
    await owner.bridge.fire("task.start", {"text": "busca recetas"})
    assert any(kind == "info" and "MCP" in line for kind, line, _ in owner.channel.logs)
    assert owner.channel.states[-1][0] == "failed"

    shared = make_rig(panel_tasks=False)
    assert "task.start" not in shared.bridge.handlers
    assert "run.stop" in shared.bridge.handlers
    await shared.bridge.fire("task.start", {"text": "busca recetas"})
    assert shared.channel.logs == [] and shared.channel.states == []


async def test_nothing_is_written_to_stdout(capsys):
    plan = {"steps": [{"action": "done", "value": "Listo."}]}
    rig = make_rig(llm=FakeLLM([plan]), channel=RecordingChannel(approve=False))
    async with Client(rig.server) as client:
        await client.call_tool("get_page_state", {})
        await client.call_tool("act", act_args(action="type_text", index=1, value=CURP))
        await client.call_tool("act", act_args(action="type_text", index=2, value="x"))
        await client.call_tool("act", act_args(action="nope"))
        await client.call_tool("run_task", {"task": "busca recetas"})
    assert capsys.readouterr().out == ""


@pytest.fixture
def babas_logging():
    logger = logging.getLogger("babas")
    handlers, level = list(logger.handlers), logger.level
    yield setup_logging
    logger.handlers[:] = handlers
    logger.setLevel(level)


async def test_a_logged_curp_is_masked(capsys, babas_logging):
    babas_logging("DEBUG")
    rig = make_rig()
    async with Client(rig.server) as client:
        await client.call_tool("get_page_state", {})
        await client.call_tool("act", act_args(action="type_text", index=1, value=CURP))
        await client.call_tool("act", act_args(action="smart_click", value=CURP))
        await client.call_tool("act", act_args(action="type_text", index=2, value="contraseña-secreta-123"))
    logging.getLogger("babas.mcp_server").info("direct %s", CURP)
    captured = capsys.readouterr()
    assert CURP not in captured.err and CURP not in captured.out
    assert "contraseña-secreta-123" not in captured.err
    assert "act type_text" in captured.err
    assert MASKED_CURP in captured.err


def test_http_endpoint_only_trusts_loopback_hosts_and_origins():
    security = _transport_security(8766)
    assert security.enable_dns_rebinding_protection
    assert security.allowed_hosts == ["127.0.0.1:8766", "localhost:8766", "[::1]:8766"]
    assert security.allowed_origins == ["http://127.0.0.1:8766", "http://localhost:8766", "http://[::1]:8766"]


async def test_http_endpoint_refuses_to_listen_beyond_loopback():
    rig = make_rig()
    for host in ("0.0.0.0", "192.168.1.20", ""):
        with pytest.raises(ValueError):
            await serve_http(rig.runtime, host, 8766)
