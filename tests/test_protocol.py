import json
from pathlib import Path
from typing import Any, Dict

import pytest
from pydantic import ValidationError

from babas.bridge.protocol import (
    PINNED_EXTENSION_ID,
    PROTOCOL_VERSION,
    Envelope,
    Hello,
    ResultMessage,
    parse_incoming,
    request,
)
from babas.drivers.base import (
    FLAG_CONSEQUENTIAL,
    FLAG_PII,
    FLAG_SEARCH_FORM,
    FLAG_SUBMIT,
    ErrorCode,
    PageState,
)

FIXTURES = Path(__file__).parent / "fixtures" / "protocol"
FIXTURE_FILES = sorted(FIXTURES.glob("*.json"))
EXPECTED_FIXTURES = {
    "event_ping",
    "hello",
    "request_page_state",
    "result_error",
    "result_page_state",
    "welcome",
}


def load(name: str) -> Dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def test_fixture_set_is_complete() -> None:
    assert {path.stem for path in FIXTURE_FILES} == EXPECTED_FIXTURES


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda path: path.stem)
def test_every_fixture_parses_with_parse_incoming(path: Path) -> None:
    raw = json.loads(path.read_text(encoding="utf-8"))
    message = parse_incoming(raw)
    assert message.v == PROTOCOL_VERSION
    assert message.id == raw["id"]
    assert message.type == raw["type"]
    if raw["type"] == "result":
        assert isinstance(message, ResultMessage)
    else:
        assert isinstance(message, Envelope)
        assert not isinstance(message, ResultMessage)


def test_pinned_extension_id_matches_manifest_key_format() -> None:
    assert len(PINNED_EXTENSION_ID) == 32
    assert set(PINNED_EXTENSION_ID) <= set("abcdefghijklmnop")


def test_hello_payload_parses_as_hello() -> None:
    message = parse_incoming(load("hello"))
    assert isinstance(message, Envelope)
    assert message.type == "hello"
    hello = Hello.model_validate(message.payload)
    assert hello.ext_version == "0.2.0"
    assert hello.protocol == PROTOCOL_VERSION
    assert hello.token is None


def test_hello_requires_protocol_and_version() -> None:
    with pytest.raises(ValidationError):
        Hello.model_validate({"ext_version": "0.2.0"})
    with pytest.raises(ValidationError):
        Hello.model_validate({"protocol": 1})


def test_welcome_carries_server_version() -> None:
    message = parse_incoming(load("welcome"))
    assert isinstance(message, Envelope)
    assert message.type == "welcome"
    assert message.payload == {"server_version": "0.2.0"}


def test_request_fixture_and_its_results_share_the_id() -> None:
    req = parse_incoming(load("request_page_state"))
    ok = parse_incoming(load("result_page_state"))
    failed = parse_incoming(load("result_error"))
    assert isinstance(req, Envelope)
    assert req.type == "page.state"
    assert req.payload["highlight"] is False
    assert ok.id == failed.id == req.id


def test_page_state_result_parses_as_page_state() -> None:
    result = parse_incoming(load("result_page_state"))
    assert isinstance(result, ResultMessage)
    assert result.ok is True
    assert result.error is None
    assert result.payload is not None

    state = PageState.model_validate(result.payload)
    assert state.snapshot_id == "k3j9x0qa"
    assert state.url == "http://127.0.0.1:8000/form.html"
    assert state.title == "Formulario de contacto"
    assert state.has_captcha is False
    assert state.has_password is False
    assert state.truncated is False
    assert state.tab is None
    assert len(state.elements) == 7
    assert [el.index for el in state.elements] == list(range(7))

    expected = {
        0: ("Buscar", [FLAG_SEARCH_FORM]),
        1: ("Buscar", [FLAG_SUBMIT, FLAG_SEARCH_FORM]),
        2: ("Correo", [FLAG_PII]),
        3: ("Estado", []),
        4: ("Acepto", []),
        5: ("Ayuda", []),
        6: ("Enviar solicitud", [FLAG_SUBMIT, FLAG_CONSEQUENTIAL]),
    }
    for index, (label, flags) in expected.items():
        element = state.element(index)
        assert element is not None
        assert element.label == label
        assert element.flags == flags

    assert state.element(1).has(FLAG_SUBMIT)
    assert state.element(1).has(FLAG_SEARCH_FORM)
    assert not state.element(1).has(FLAG_CONSEQUENTIAL)
    assert state.element(2).has(FLAG_PII)
    assert state.element(2).type == "email"
    assert state.element(3).options == ["Aguascalientes", "Jalisco", "Yucatán"]
    assert state.element(4).checked is False
    assert state.element(5).href == "#ayuda"
    assert state.element(6).in_viewport is False
    assert all(state.element(i).in_viewport for i in range(6))
    assert state.element(7) is None


def test_error_result_carries_stale_snapshot_code() -> None:
    result = parse_incoming(load("result_error"))
    assert isinstance(result, ResultMessage)
    assert result.ok is False
    assert result.payload is None
    assert result.error is not None
    assert result.error.code == ErrorCode.STALE_SNAPSHOT
    assert result.error.message
    assert result.error.data is None


def test_event_ping_is_a_plain_envelope() -> None:
    message = parse_incoming(load("event_ping"))
    assert isinstance(message, Envelope)
    assert message.type == "ping"
    assert message.payload == {}


def test_request_builds_envelope_with_payload() -> None:
    envelope = request("tab.goto", {"url": "https://example.com"})
    assert envelope.v == PROTOCOL_VERSION
    assert envelope.type == "tab.goto"
    assert envelope.payload == {"url": "https://example.com"}
    assert request("tab.info").payload == {}
    round_trip = parse_incoming(json.loads(envelope.model_dump_json()))
    assert round_trip == envelope


def test_request_ids_are_unique() -> None:
    ids = [request("tab.info").id for _ in range(2000)]
    assert len(set(ids)) == len(ids)
    assert all(len(i) == 32 and int(i, 16) >= 0 for i in ids)


@pytest.mark.parametrize("name", ["hello", "welcome", "request_page_state", "event_ping"])
def test_envelope_rejects_protocol_v2(name: str) -> None:
    raw = {**load(name), "v": 2}
    with pytest.raises(ValidationError):
        Envelope.model_validate(raw)
    with pytest.raises(ValidationError):
        parse_incoming(raw)


@pytest.mark.parametrize("name", ["hello", "welcome", "request_page_state", "event_ping"])
def test_envelope_rejects_empty_id(name: str) -> None:
    raw = {**load(name), "id": ""}
    with pytest.raises(ValidationError):
        Envelope.model_validate(raw)
    with pytest.raises(ValidationError):
        parse_incoming(raw)


def test_envelope_rejects_missing_id_and_empty_type() -> None:
    raw = load("hello")
    without_id = {k: v for k, v in raw.items() if k != "id"}
    with pytest.raises(ValidationError):
        Envelope.model_validate(without_id)
    with pytest.raises(ValidationError):
        Envelope.model_validate({**raw, "type": ""})


@pytest.mark.parametrize("name", ["result_page_state", "result_error"])
def test_result_rejects_protocol_v2_and_empty_id(name: str) -> None:
    with pytest.raises(ValidationError):
        parse_incoming({**load(name), "v": 2})
    with pytest.raises(ValidationError):
        parse_incoming({**load(name), "id": ""})


def test_result_requires_ok_flag() -> None:
    raw = {k: v for k, v in load("result_page_state").items() if k != "ok"}
    with pytest.raises(ValidationError):
        parse_incoming(raw)
