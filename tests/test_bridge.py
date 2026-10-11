import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, Optional

import pytest
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from babas import __version__
from babas.bridge.protocol import PINNED_EXTENSION_ID, PROTOCOL_VERSION
from babas.bridge.server import BridgeServer
from babas.drivers.base import DriverError, ErrorCode

ORIGIN = f"chrome-extension://{PINNED_EXTENSION_ID}"
WAIT = 3.0


def frame(type: str, payload: Optional[Dict[str, Any]] = None, id: str = "c0ffee00c0ffee00c0ffee00c0ffee00") -> str:
    return json.dumps({"v": 1, "id": id, "type": type, "payload": payload or {}})


def hello_frame(protocol: int = PROTOCOL_VERSION, ext_version: str = "0.2.0") -> str:
    return frame("hello", {"ext_version": ext_version, "protocol": protocol})


def result_frame(id: str, payload: Optional[Dict[str, Any]] = None) -> str:
    return json.dumps({"v": 1, "id": id, "type": "result", "ok": True, "payload": payload or {}})


def error_frame(id: str, code: str, message: str, data: Optional[Dict[str, Any]] = None) -> str:
    error: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return json.dumps({"v": 1, "id": id, "type": "result", "ok": False, "error": error})


async def recv_json(ws: ClientConnection) -> Dict[str, Any]:
    return json.loads(await asyncio.wait_for(ws.recv(), WAIT))


def open_socket(bridge: BridgeServer, origin: str = ORIGIN):
    return connect(bridge.url, origin=origin, proxy=None, open_timeout=WAIT)


@asynccontextmanager
async def fake_extension(bridge: BridgeServer, ext_version: str = "0.2.0") -> AsyncIterator[ClientConnection]:
    async with open_socket(bridge) as ws:
        await ws.send(hello_frame(ext_version=ext_version))
        welcome = await recv_json(ws)
        assert welcome["type"] == "welcome"
        yield ws


async def expect_closed(ws: ClientConnection) -> int:
    with pytest.raises(ConnectionClosed) as info:
        await asyncio.wait_for(ws.recv(), WAIT)
    assert info.value.rcvd is not None
    return info.value.rcvd.code


async def answer_next(ws: ClientConnection, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    seen = await recv_json(ws)
    await ws.send(result_frame(seen["id"], payload))
    return seen


@pytest.fixture
async def bridge() -> AsyncIterator[BridgeServer]:
    server = BridgeServer(port=0)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


async def test_server_binds_an_ephemeral_port(bridge: BridgeServer) -> None:
    assert bridge.port != 0
    assert bridge.url == f"ws://127.0.0.1:{bridge.port}"
    assert bridge.connected is False
    assert bridge.extension_version is None
    assert await bridge.wait_connected(0.05) is False


async def test_hello_gets_welcome(bridge: BridgeServer) -> None:
    async with open_socket(bridge) as ws:
        await ws.send(hello_frame(ext_version="9.9.9"))
        welcome = await recv_json(ws)
        assert welcome["v"] == 1
        assert welcome["type"] == "welcome"
        assert welcome["id"]
        assert welcome["payload"]["server_version"] == __version__
        assert await bridge.wait_connected(WAIT) is True
        assert bridge.connected is True
        assert bridge.extension_version == "9.9.9"


async def test_request_response_round_trip(bridge: BridgeServer) -> None:
    async with fake_extension(bridge) as ws:
        assert await bridge.wait_connected(WAIT)
        responder = asyncio.create_task(answer_next(ws, {"url": "https://example.com", "tab_id": 7}))
        result = await bridge.request("tab.info", {"tab": "main"}, timeout=WAIT)
        seen = await responder
        assert seen["v"] == 1
        assert seen["type"] == "tab.info"
        assert seen["payload"] == {"tab": "main"}
        assert len(seen["id"]) > 0
        assert result == {"url": "https://example.com", "tab_id": 7}


async def test_request_without_payload_gets_empty_object(bridge: BridgeServer) -> None:
    async with fake_extension(bridge) as ws:
        assert await bridge.wait_connected(WAIT)
        responder = asyncio.create_task(answer_next(ws))
        assert await bridge.request("tab.info", timeout=WAIT) == {}
        assert (await responder)["payload"] == {}


async def test_concurrent_requests_are_matched_by_id(bridge: BridgeServer) -> None:
    async with fake_extension(bridge) as ws:
        assert await bridge.wait_connected(WAIT)
        first = asyncio.create_task(bridge.request("tab.info", {"n": 1}, timeout=WAIT))
        second = asyncio.create_task(bridge.request("tab.info", {"n": 2}, timeout=WAIT))
        received = [await recv_json(ws), await recv_json(ws)]
        for message in reversed(received):
            await ws.send(result_frame(message["id"], {"echo": message["payload"]["n"]}))
        assert await first == {"echo": 1}
        assert await second == {"echo": 2}


async def test_error_result_becomes_driver_error(bridge: BridgeServer) -> None:
    async with fake_extension(bridge) as ws:
        assert await bridge.wait_connected(WAIT)

        async def refuse() -> None:
            seen = await recv_json(ws)
            await ws.send(error_frame(seen["id"], ErrorCode.NOT_FOUND, "No such element", {"index": 3}))

        responder = asyncio.create_task(refuse())
        with pytest.raises(DriverError) as info:
            await bridge.request("page.act", {"op": "click"}, timeout=WAIT)
        await responder
        assert info.value.code == ErrorCode.NOT_FOUND
        assert info.value.message == "No such element"
        assert info.value.data == {"index": 3}


async def test_request_times_out_with_silent_client(bridge: BridgeServer) -> None:
    async with fake_extension(bridge) as ws:
        assert await bridge.wait_connected(WAIT)
        with pytest.raises(DriverError) as info:
            await bridge.request("page.state", timeout=0.2)
        assert info.value.code == ErrorCode.TIMEOUT
        assert (await recv_json(ws))["type"] == "page.state"
        assert bridge.connected is True


async def test_late_result_after_timeout_is_ignored(bridge: BridgeServer) -> None:
    async with fake_extension(bridge) as ws:
        assert await bridge.wait_connected(WAIT)
        with pytest.raises(DriverError) as info:
            await bridge.request("page.state", timeout=0.1)
        assert info.value.code == ErrorCode.TIMEOUT
        stale = await recv_json(ws)
        await ws.send(result_frame(stale["id"], {"late": True}))
        responder = asyncio.create_task(answer_next(ws, {"fresh": True}))
        assert await bridge.request("page.state", timeout=WAIT) == {"fresh": True}
        await responder


async def test_request_without_extension_raises_no_extension(bridge: BridgeServer) -> None:
    with pytest.raises(DriverError) as info:
        await bridge.request("tab.info", timeout=0.2)
    assert info.value.code == ErrorCode.NO_EXTENSION


@pytest.mark.parametrize("origin", ["https://evil.example", "chrome-extension://" + "a" * 32])
async def test_wrong_origin_is_rejected_at_handshake(bridge: BridgeServer, origin: str) -> None:
    with pytest.raises(InvalidStatus) as info:
        async with open_socket(bridge, origin=origin):
            pass
    assert info.value.response.status_code == 403
    assert bridge.connected is False


async def test_first_frame_must_be_hello(bridge: BridgeServer) -> None:
    async with open_socket(bridge) as ws:
        await ws.send(frame("page.state"))
        assert await expect_closed(ws) == 1008
    assert bridge.connected is False


async def test_garbage_first_frame_is_closed_with_1008(bridge: BridgeServer) -> None:
    async with open_socket(bridge) as ws:
        await ws.send("this is not json")
        assert await expect_closed(ws) == 1008
    assert bridge.connected is False


async def test_hello_with_unsupported_protocol_is_closed_with_1008(bridge: BridgeServer) -> None:
    async with open_socket(bridge) as ws:
        await ws.send(hello_frame(protocol=2))
        assert await expect_closed(ws) == 1008
    assert bridge.connected is False
    assert bridge.extension_version is None


async def test_second_client_replaces_first_and_fails_its_pending_request(bridge: BridgeServer) -> None:
    async with fake_extension(bridge, ext_version="1.0.0") as first:
        assert await bridge.wait_connected(WAIT)
        pending = asyncio.create_task(bridge.request("page.state", timeout=WAIT))
        assert (await recv_json(first))["type"] == "page.state"

        async with fake_extension(bridge, ext_version="2.0.0") as second:
            with pytest.raises(DriverError) as info:
                await asyncio.wait_for(pending, WAIT)
            assert info.value.code == ErrorCode.NO_EXTENSION
            assert await expect_closed(first) == 1000
            assert bridge.connected is True
            assert bridge.extension_version == "2.0.0"

            responder = asyncio.create_task(answer_next(second, {"who": "second"}))
            assert await bridge.request("tab.info", timeout=WAIT) == {"who": "second"}
            await responder


async def test_disconnect_fails_pending_request_and_clears_connection(bridge: BridgeServer) -> None:
    async with fake_extension(bridge) as ws:
        assert await bridge.wait_connected(WAIT)
        pending = asyncio.create_task(bridge.request("page.state", timeout=WAIT))
        assert (await recv_json(ws))["type"] == "page.state"
        await ws.close()
        with pytest.raises(DriverError) as info:
            await asyncio.wait_for(pending, WAIT)
        assert info.value.code == ErrorCode.NO_EXTENSION
    for _ in range(100):
        if not bridge.connected:
            break
        await asyncio.sleep(0.02)
    assert bridge.connected is False
    with pytest.raises(DriverError) as info:
        await bridge.request("tab.info", timeout=0.2)
    assert info.value.code == ErrorCode.NO_EXTENSION


async def test_event_reaches_registered_handler(bridge: BridgeServer) -> None:
    received: "asyncio.Future[Dict[str, Any]]" = asyncio.get_running_loop().create_future()

    async def on_task_start(payload: Dict[str, Any]) -> None:
        received.set_result(payload)

    bridge.on("task.start", on_task_start)
    async with fake_extension(bridge) as ws:
        await ws.send(frame("task.start", {"text": "abrir YouTube"}))
        assert await asyncio.wait_for(received, WAIT) == {"text": "abrir YouTube"}


async def test_failing_handler_does_not_break_the_connection(bridge: BridgeServer) -> None:
    calls = []

    async def broken(payload: Dict[str, Any]) -> None:
        calls.append(payload)
        raise RuntimeError("boom")

    bridge.on("task.start", broken)
    async with fake_extension(bridge) as ws:
        assert await bridge.wait_connected(WAIT)
        await ws.send(frame("task.start", {"n": 1}))
        responder = asyncio.create_task(answer_next(ws, {"alive": True}))
        assert await bridge.request("tab.info", timeout=WAIT) == {"alive": True}
        await responder
        assert calls == [{"n": 1}]


async def test_ping_frames_are_ignored(bridge: BridgeServer) -> None:
    pings = []

    async def on_ping(payload: Dict[str, Any]) -> None:
        pings.append(payload)

    bridge.on("ping", on_ping)
    async with fake_extension(bridge) as ws:
        assert await bridge.wait_connected(WAIT)
        await ws.send(frame("ping"))
        await ws.send(frame("ping"))
        responder = asyncio.create_task(answer_next(ws, {"ok": 1}))
        assert await bridge.request("tab.info", timeout=WAIT) == {"ok": 1}
        await responder
        await asyncio.sleep(0.05)
        assert pings == []
        assert bridge.connected is True


async def test_malformed_frames_are_ignored(bridge: BridgeServer) -> None:
    async with fake_extension(bridge) as ws:
        assert await bridge.wait_connected(WAIT)
        await ws.send("not json at all")
        await ws.send(json.dumps({"v": 2, "id": "x", "type": "task.start", "payload": {}}))
        await ws.send(json.dumps({"v": 1, "id": "", "type": "task.start", "payload": {}}))
        responder = asyncio.create_task(answer_next(ws, {"still": "here"}))
        assert await bridge.request("tab.info", timeout=WAIT) == {"still": "here"}
        await responder
        assert bridge.connected is True


async def test_stop_fails_pending_and_closes_client() -> None:
    server = BridgeServer(port=0)
    await server.start()
    try:
        async with fake_extension(server) as ws:
            assert await server.wait_connected(WAIT)
            pending = asyncio.create_task(server.request("page.state", timeout=WAIT))
            assert (await recv_json(ws))["type"] == "page.state"
            await server.stop()
            with pytest.raises(DriverError) as info:
                await asyncio.wait_for(pending, WAIT)
            assert info.value.code == ErrorCode.NO_EXTENSION
            assert await expect_closed(ws) == 1001
    finally:
        await server.stop()
