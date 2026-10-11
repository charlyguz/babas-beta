import asyncio
import json
import logging
import os
import stat
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Optional, Tuple

import pytest
from websockets.asyncio.client import ClientConnection, connect
from websockets.asyncio.server import ServerConnection
from websockets.exceptions import ConnectionClosed

from babas.bridge.protocol import PINNED_EXTENSION_ID, PROTOCOL_VERSION
from babas.bridge.server import BridgeServer, default_pairing_file, reset_pairing

ORIGIN = f"chrome-extension://{PINNED_EXTENSION_ID}"
WAIT = 3.0
POSIX = sys.platform != "win32"


def frame(type: str, payload: Optional[Dict[str, Any]] = None, id: str = "c0ffee00c0ffee00c0ffee00c0ffee00") -> str:
    return json.dumps({"v": 1, "id": id, "type": type, "payload": payload or {}})


def hello_frame(token: Optional[str] = None, protocol: int = PROTOCOL_VERSION) -> str:
    payload: Dict[str, Any] = {"ext_version": "0.2.0", "protocol": protocol}
    if token is not None:
        payload["token"] = token
    return frame("hello", payload)


async def recv_json(ws: ClientConnection) -> Dict[str, Any]:
    return json.loads(await asyncio.wait_for(ws.recv(), WAIT))


def open_socket(bridge: BridgeServer):
    return connect(bridge.url, origin=ORIGIN, proxy=None, open_timeout=WAIT)


async def closed_with(ws: ClientConnection) -> Tuple[int, str]:
    with pytest.raises(ConnectionClosed) as info:
        await asyncio.wait_for(ws.recv(), WAIT)
    assert info.value.rcvd is not None
    return info.value.rcvd.code, info.value.rcvd.reason


@asynccontextmanager
async def paired_extension(bridge: BridgeServer, token: Optional[str] = None) -> AsyncIterator[Tuple[ClientConnection, Dict[str, Any]]]:
    async with open_socket(bridge) as ws:
        await ws.send(hello_frame(token))
        welcome = await recv_json(ws)
        assert welcome["type"] == "welcome"
        yield ws, welcome["payload"]


async def rejected_with(bridge: BridgeServer, token: Optional[str]) -> Tuple[int, str]:
    async with open_socket(bridge) as ws:
        await ws.send(hello_frame(token))
        return await closed_with(ws)


async def wait_disconnected(bridge: BridgeServer) -> None:
    for _ in range(100):
        if not bridge.connected:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("bridge still reports a connection")


async def answer_next(ws: ClientConnection, payload: Dict[str, Any]) -> None:
    seen = await recv_json(ws)
    await ws.send(json.dumps({"v": 1, "id": seen["id"], "type": "result", "ok": True, "payload": payload}))


@pytest.fixture
def pairing_file(tmp_path: Path) -> Path:
    return tmp_path / "home" / "pairing.json"


@pytest.fixture
async def bridge(pairing_file: Path) -> AsyncIterator[BridgeServer]:
    server = BridgeServer(port=0, pairing_file=pairing_file)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


async def pair(bridge: BridgeServer) -> str:
    async with paired_extension(bridge) as (_, payload):
        token = payload["token"]
    await wait_disconnected(bridge)
    return token


async def test_first_connection_gets_a_token_and_the_file_is_created(bridge: BridgeServer, pairing_file: Path) -> None:
    assert not pairing_file.exists()
    async with paired_extension(bridge) as (_, payload):
        assert isinstance(payload["token"], str)
        assert len(payload["token"]) >= 43
        assert payload["server_version"]
        assert await bridge.wait_connected(WAIT)
        assert json.loads(pairing_file.read_text(encoding="utf-8")) == {"token": payload["token"]}
    if POSIX:
        assert stat.S_IMODE(pairing_file.stat().st_mode) == 0o600
        assert stat.S_IMODE(pairing_file.parent.stat().st_mode) == 0o700
    assert [entry.name for entry in pairing_file.parent.iterdir()] == ["pairing.json"]


async def test_second_connection_with_the_right_token_is_accepted(bridge: BridgeServer, pairing_file: Path) -> None:
    token = await pair(bridge)
    before = pairing_file.read_text(encoding="utf-8")
    async with paired_extension(bridge, token) as (ws, payload):
        assert "token" not in payload
        assert await bridge.wait_connected(WAIT)
        responder = asyncio.create_task(answer_next(ws, {"tab_id": 1}))
        assert await bridge.request("tab.info", timeout=WAIT) == {"tab_id": 1}
        await responder
    assert pairing_file.read_text(encoding="utf-8") == before


async def test_right_token_replaces_the_previous_connection(bridge: BridgeServer) -> None:
    async with paired_extension(bridge) as (first, payload):
        async with paired_extension(bridge, payload["token"]) as (second, again):
            assert "token" not in again
            assert await closed_with(first) == (1000, "replaced by a new connection")
            assert bridge.connected is True
            responder = asyncio.create_task(answer_next(second, {"who": "second"}))
            assert await bridge.request("tab.info", timeout=WAIT) == {"who": "second"}
            await responder


@pytest.mark.parametrize("presented", [None, "", "wrong-token", "tökén-ñ"])
async def test_wrong_or_missing_token_is_closed_without_disturbing_the_connection(
    bridge: BridgeServer, pairing_file: Path, presented: Optional[str]
) -> None:
    async with paired_extension(bridge) as (good, payload):
        token = payload["token"]
        assert await bridge.wait_connected(WAIT)
        pending = asyncio.create_task(bridge.request("tab.info", timeout=WAIT))
        seen = await recv_json(good)

        assert await rejected_with(bridge, presented) == (1008, "not paired")

        assert bridge.connected is True
        assert not pending.done()
        assert json.loads(pairing_file.read_text(encoding="utf-8")) == {"token": token}
        await good.send(json.dumps({"v": 1, "id": seen["id"], "type": "result", "ok": True, "payload": {"tab_id": 2}}))
        assert await pending == {"tab_id": 2}


async def test_rejection_reason_does_not_reveal_whether_a_token_exists(bridge: BridgeServer) -> None:
    await pair(bridge)
    wrong = await rejected_with(bridge, "wrong-token")
    missing = await rejected_with(bridge, None)
    assert wrong == missing == (1008, "not paired")
    assert bridge.connected is False


async def test_protocol_mismatch_is_checked_before_pairing(bridge: BridgeServer, pairing_file: Path) -> None:
    async with open_socket(bridge) as ws:
        await ws.send(hello_frame(protocol=2))
        code, _ = await closed_with(ws)
    assert code == 1008
    assert not pairing_file.exists()


async def test_reset_pairing_allows_a_fresh_trust_on_first_use(bridge: BridgeServer, pairing_file: Path) -> None:
    old = await pair(bridge)
    assert await rejected_with(bridge, None) == (1008, "not paired")

    assert reset_pairing(pairing_file) is True
    assert not pairing_file.exists()
    assert reset_pairing(pairing_file) is False

    async with paired_extension(bridge, old) as (_, payload):
        new = payload["token"]
        assert new != old
        assert json.loads(pairing_file.read_text(encoding="utf-8")) == {"token": new}
    await wait_disconnected(bridge)
    assert await rejected_with(bridge, old) == (1008, "not paired")
    async with paired_extension(bridge, new) as (_, payload):
        assert "token" not in payload


async def test_simultaneous_first_connections_cannot_both_pair(bridge: BridgeServer, pairing_file: Path) -> None:
    async with open_socket(bridge) as first, open_socket(bridge) as second:
        await asyncio.gather(first.send(hello_frame()), second.send(hello_frame()))
        outcomes = await asyncio.gather(
            asyncio.wait_for(first.recv(), WAIT), asyncio.wait_for(second.recv(), WAIT), return_exceptions=True
        )
        welcomed = [item for item in outcomes if isinstance(item, str)]
        closed = [item for item in outcomes if isinstance(item, ConnectionClosed)]
        assert len(welcomed) == 1 and len(closed) == 1
        token = json.loads(welcomed[0])["payload"]["token"]
        assert closed[0].rcvd is not None and closed[0].rcvd.code == 1008
        assert json.loads(pairing_file.read_text(encoding="utf-8")) == {"token": token}


async def test_failed_welcome_does_not_leave_a_token_the_extension_never_got(
    bridge: BridgeServer, pairing_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = ServerConnection.send

    async def failing_send(self: ServerConnection, message: Any, *args: Any, **kwargs: Any) -> None:
        if isinstance(message, str) and '"welcome"' in message:
            raise ConnectionClosed(None, None)
        await original(self, message, *args, **kwargs)

    monkeypatch.setattr(ServerConnection, "send", failing_send)
    async with open_socket(bridge) as ws:
        await ws.send(hello_frame())
        with pytest.raises(ConnectionClosed):
            await asyncio.wait_for(ws.recv(), WAIT)
    await wait_disconnected(bridge)
    assert not pairing_file.exists()
    monkeypatch.undo()

    async with paired_extension(bridge) as (_, payload):
        assert isinstance(payload["token"], str)
        assert pairing_file.exists()


async def test_pairing_disabled_sends_no_token_and_accepts_anything() -> None:
    server = BridgeServer(port=0)
    assert server.pairing_file is None
    await server.start()
    try:
        async with paired_extension(server) as (_, payload):
            assert payload == {"server_version": payload["server_version"]}
        await wait_disconnected(server)
        async with paired_extension(server, "anything-at-all") as (_, payload):
            assert "token" not in payload
    finally:
        await server.stop()


@pytest.mark.parametrize(
    "content",
    ["", "not json", "[]", '"token"', "{}", '{"token": ""}', '{"token": 5}', '{"token": null}', '{"tok": "abc"}'],
)
@pytest.mark.parametrize("presented", [None, "abc"])
async def test_corrupt_pairing_file_fails_closed(
    bridge: BridgeServer, pairing_file: Path, caplog: pytest.LogCaptureFixture, content: str, presented: Optional[str]
) -> None:
    pairing_file.parent.mkdir(parents=True)
    pairing_file.write_text(content, encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="babas.bridge"):
        assert await rejected_with(bridge, presented) == (1008, "not paired")
    assert bridge.connected is False
    assert pairing_file.read_text(encoding="utf-8") == content
    assert "pairing file is corrupt" in caplog.text


async def test_unreadable_pairing_path_fails_closed(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    server = BridgeServer(port=0, pairing_file=tmp_path)
    await server.start()
    try:
        with caplog.at_level(logging.WARNING, logger="babas.bridge"):
            assert await rejected_with(server, None) == (1008, "not paired")
        assert server.connected is False
        assert "pairing file unreadable" in caplog.text
    finally:
        await server.stop()


async def test_unwritable_pairing_location_fails_closed(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory", encoding="utf-8")
    server = BridgeServer(port=0, pairing_file=blocker / "pairing.json")
    await server.start()
    try:
        with caplog.at_level(logging.WARNING, logger="babas.bridge"):
            assert await rejected_with(server, None) == (1008, "not paired")
        assert server.connected is False
        assert caplog.text
    finally:
        await server.stop()


async def test_repeated_rejections_log_one_warning(bridge: BridgeServer, caplog: pytest.LogCaptureFixture) -> None:
    await pair(bridge)
    with caplog.at_level(logging.WARNING, logger="babas.bridge"):
        for _ in range(3):
            assert await rejected_with(bridge, "wrong-token") == (1008, "not paired")
    assert len([record for record in caplog.records if "not paired" in record.getMessage()]) == 1


async def test_token_never_appears_in_logs(bridge: BridgeServer, caplog: pytest.LogCaptureFixture) -> None:
    secret = "SECRET-TOKEN-VALUE"
    with caplog.at_level(logging.DEBUG, logger="babas"):
        token = await pair(bridge)
        async with paired_extension(bridge, token) as (_, payload):
            assert "token" not in payload
        await wait_disconnected(bridge)
        await rejected_with(bridge, secret)
        async with open_socket(bridge) as ws:
            await ws.send(frame("hello", {"token": secret}))
            await closed_with(ws)
        async with open_socket(bridge) as ws:
            await ws.send(json.dumps({"v": 1, "type": "hello", "payload": {"token": secret, "protocol": 1}}))
            await closed_with(ws)
        async with open_socket(bridge) as ws:
            await ws.send(json.dumps({"v": 1, "id": "x", "type": "hello", "payload": {"token": 12345678, "protocol": 1}}))
            await closed_with(ws)
    assert any(record.name == "babas.bridge" for record in caplog.records)
    assert "extension connected" in caplog.text
    for value in (token, secret, "12345678"):
        assert value not in caplog.text


def test_default_pairing_file_honors_babas_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BABAS_HOME", str(tmp_path / "custom"))
    assert default_pairing_file() == tmp_path / "custom" / "pairing.json"


def test_default_pairing_file_falls_back_to_the_home_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    for value in (None, ""):
        if value is None:
            monkeypatch.delenv("BABAS_HOME", raising=False)
        else:
            monkeypatch.setenv("BABAS_HOME", value)
        assert default_pairing_file() == Path.home() / ".babas" / "pairing.json"
        assert default_pairing_file().parent.name == ".babas"


def test_default_pairing_file_expands_user(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("BABAS_HOME", os.path.join("~", "babas-data"))
    assert default_pairing_file() == tmp_path / "babas-data" / "pairing.json"


def test_reset_pairing_reports_whether_a_file_existed(tmp_path: Path) -> None:
    path = tmp_path / "pairing.json"
    assert reset_pairing(path) is False
    path.write_text('{"token": "x"}', encoding="utf-8")
    assert reset_pairing(path) is True
    assert not path.exists()
