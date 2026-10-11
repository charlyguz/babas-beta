import asyncio
import json
import logging
import os
import re
import stat
import time
from typing import Any, Callable, Dict, List, Sequence, Tuple

import pytest
from playwright.async_api import expect

from babas.bridge.server import BridgeServer, reset_pairing
from babas.drivers.base import DriverError, ErrorCode
from babas.safety import CAPTCHA_TEXT, HANDOFF_TEXTS, mask

pytestmark = pytest.mark.e2e

UI_TIMEOUT_MS = 10000
RUN_TIMEOUT_MS = 30000
EMAIL = "ana@example.com"
PHONE = "5512345678"
CURP = "GOMC800101HDFRRL09"
DONE = {"steps": [{"action": "done", "value": "Todo listo."}]}


def type_correo(value: str = EMAIL) -> Dict[str, Any]:
    return {"op": "type", "target": {"text": "Correo"}, "text": value}


async def log_entries(panel) -> List[Tuple[str, str]]:
    rows = await panel.eval_on_selector_all(
        "[data-testid=log-entry]", "els => els.map(e => [e.dataset.kind, e.textContent])"
    )
    return [(kind, text) for kind, text in rows]


async def wait_run_state(panel, text: str, timeout: int = RUN_TIMEOUT_MS) -> None:
    await expect(panel.get_by_test_id("run-state")).to_contain_text(text, timeout=timeout)


async def wait_until(predicate: Callable[[], bool], timeout: float = 10.0, what: str = "the condition") -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout:.0f} s waiting for {what}")
        await asyncio.sleep(0.05)


async def stored_local(sw, key: str) -> Any:
    return await sw.evaluate(f"chrome.storage.local.get({json.dumps(key)}).then(items => items[{json.dumps(key)}] ?? null)")


async def approve_in_panel(bridge, panel, actions: Sequence[Dict[str, Any]], reasons: Sequence[str] = ("pii_value",)) -> str:
    request = asyncio.ensure_future(
        bridge.request(
            "ui.confirm",
            {"actions": list(actions), "reasons": list(reasons), "explanation": "Prueba"},
            timeout=30,
        )
    )
    try:
        await panel.get_by_test_id("confirm-yes").click(timeout=UI_TIMEOUT_MS)
        answer = await asyncio.wait_for(request, 10)
    finally:
        if not request.done():
            request.cancel()
    assert answer["approved"] is True
    return answer["confirmation_id"]


async def expect_refused(bridge, payload: Dict[str, Any], code: str) -> DriverError:
    with pytest.raises(DriverError) as caught:
        await bridge.request("page.act", payload, timeout=10)
    assert caught.value.code == code, f"{caught.value.code}: {caught.value.message}"
    return caught.value



@pytest.mark.parametrize(
    "extra",
    [{"confirmation_id": "forged"}, {}, {"confirmation_id": ""}],
    ids=["forged_id", "no_id", "empty_id"],
)
async def test_forged_act_refused(ext_env, work_page, fixture_url, field_value, extra):
    await ext_env.bridge.request("tab.goto", {"url": fixture_url("form.html")})

    error = await expect_refused(ext_env.bridge, {**type_correo("x@y.com"), **extra}, ErrorCode.CONFIRMATION_REQUIRED)

    assert "pii_value" in error.data["reasons"] or "pii_field" in error.data["reasons"]
    assert error.data["label"] == "Correo"
    assert await field_value("#correo") == ""


async def test_forged_act_refused_for_submit_and_enter(ext_env, work_page, fixture_url, field_value):
    await ext_env.bridge.request("tab.goto", {"url": fixture_url("form.html")})

    submit = await expect_refused(
        ext_env.bridge,
        {"op": "click", "target": {"text": "Enviar solicitud"}, "confirmation_id": "forged"},
        ErrorCode.CONFIRMATION_REQUIRED,
    )
    await expect_refused(
        ext_env.bridge,
        {"op": "press", "key": "Enter", "target": {"text": "Nombre"}, "confirmation_id": "forged"},
        ErrorCode.CONFIRMATION_REQUIRED,
    )

    assert "submit_form" in submit.data["reasons"]
    assert await field_value("#result") == ""


async def test_token_single_use(ext_env, panel, work_page, fixture_url, field_value):
    payload = type_correo()
    await ext_env.bridge.request("tab.goto", {"url": fixture_url("form.html")})

    confirmation_id = await approve_in_panel(ext_env.bridge, panel, [payload])
    first = await ext_env.bridge.request("page.act", {**payload, "confirmation_id": confirmation_id}, timeout=10)
    assert first["result"]["label"] == "Correo"
    assert await field_value("#correo") == EMAIL

    await work_page.fill("#correo", "")
    await expect_refused(ext_env.bridge, {**payload, "confirmation_id": confirmation_id}, ErrorCode.CONFIRMATION_REQUIRED)
    assert await field_value("#correo") == ""


async def test_confirmation_is_bound_to_the_action(ext_env, panel, work_page, fixture_url, field_value):
    approved = type_correo()
    await ext_env.bridge.request("tab.goto", {"url": fixture_url("form.html")})
    confirmation_id = await approve_in_panel(ext_env.bridge, panel, [approved])

    await expect_refused(
        ext_env.bridge, {**type_correo("otra@example.com"), "confirmation_id": confirmation_id}, ErrorCode.CONFIRMATION_REQUIRED
    )
    await expect_refused(
        ext_env.bridge,
        {"op": "type", "target": {"text": "Teléfono"}, "text": EMAIL, "confirmation_id": confirmation_id},
        ErrorCode.CONFIRMATION_REQUIRED,
    )
    assert await field_value("#correo") == ""

    await ext_env.bridge.request("page.act", {**approved, "confirmation_id": confirmation_id}, timeout=10)
    assert await field_value("#correo") == EMAIL


async def test_stop_revokes_unused_confirmation(ext_env, panel, work_page, fixture_url, field_value):
    payload = type_correo()
    await ext_env.bridge.request("tab.goto", {"url": fixture_url("form.html")})
    confirmation_id = await approve_in_panel(ext_env.bridge, panel, [payload])
    await ext_env.bridge.request("ui.run_state", {"state": "running"}, timeout=10)

    await panel.get_by_test_id("stop").click(timeout=UI_TIMEOUT_MS)
    await expect_refused(ext_env.bridge, {**payload, "confirmation_id": confirmation_id}, ErrorCode.HALTED)
    await panel.get_by_test_id("resume").click(timeout=UI_TIMEOUT_MS)
    await expect(panel.get_by_test_id("status")).to_have_text("Conectado", timeout=UI_TIMEOUT_MS)

    await expect_refused(ext_env.bridge, {**payload, "confirmation_id": confirmation_id}, ErrorCode.CONFIRMATION_REQUIRED)
    assert await field_value("#correo") == ""


async def test_extension_refuses_to_type_a_password_even_when_approved(ext_env, panel, work_page, fixture_url, field_value):
    payload = {"op": "type", "target": {"text": "Contraseña"}, "text": "secreto-123"}
    await ext_env.bridge.request("tab.goto", {"url": fixture_url("login.html")})
    confirmation_id = await approve_in_panel(ext_env.bridge, panel, [payload], reasons=("pii_field",))

    await expect_refused(ext_env.bridge, {**payload, "confirmation_id": confirmation_id}, ErrorCode.PASSWORD_FIELD)
    await expect_refused(ext_env.bridge, dict(payload), ErrorCode.PASSWORD_FIELD)

    assert await field_value("#clave") == ""


async def test_extension_blocks_unlisted_sites(ext_env, work_page, fixture_url, pages_url):
    await ext_env.bridge.request("tab.goto", {"url": fixture_url("form.html")})
    port = pages_url.rsplit(":", 1)[1]
    unlisted = f"http://localhost:{port}/form.html"

    for request_type in ("tab.goto", "tab.new"):
        with pytest.raises(DriverError) as caught:
            await ext_env.bridge.request(request_type, {"url": unlisted}, timeout=10)
        assert caught.value.code == ErrorCode.DOMAIN_NOT_ALLOWED
        assert caught.value.data["host"] == "localhost"

    # The user can browse anywhere on their own, but BABAS refuses to read a page outside its list.
    await work_page.goto(unlisted)
    for request_type, payload in (("page.state", {}), ("page.describe", {"target": {"text": "Correo"}})):
        with pytest.raises(DriverError) as caught:
            await ext_env.bridge.request(request_type, payload, timeout=10)
        assert caught.value.code == ErrorCode.DOMAIN_NOT_ALLOWED



async def test_search_form_no_prompt(session, panel, work_page, fake_llm, fixture_url, submit_task, bridge_log, field_value):
    fake_llm.push(
        {"steps": [{"action": "goto", "value": fixture_url("search.html"), "explanation": "Voy a abrir el buscador."}]},
        {"steps": [{"action": "smart_search", "value": "pozole", "explanation": "Voy a buscar pozole."}]},
        {"steps": [{"action": "done", "value": "Ya busqué pozole."}]},
    )

    await submit_task("Busca recetas de pozole")

    await wait_run_state(panel, "Listo.")
    await expect(work_page).to_have_url(re.compile(r"/search\.html\?q=pozole"))
    assert "pozole" in await field_value("#resultado")
    assert bridge_log.count("ui.confirm") == 0
    await expect(panel.get_by_test_id("confirm-card")).to_have_count(0)


async def test_allow_site_prompt(session, panel, ext_env, work_page, fake_llm, pages_url, submit_task, bridge_log):
    port = pages_url.rsplit(":", 1)[1]
    unlisted = f"http://localhost:{port}/form.html"
    fake_llm.push(
        {"steps": [{"action": "goto", "value": unlisted, "explanation": "Voy a abrir la página de prueba."}]},
        {"steps": [{"action": "done", "value": "Abrí la página."}]},
    )

    await submit_task("Abre la página de prueba")

    allow = panel.get_by_test_id("allow-card")
    await expect(allow).to_be_visible(timeout=UI_TIMEOUT_MS)
    await expect(panel.get_by_test_id("allow-text")).to_contain_text("localhost")
    assert work_page.url == "about:blank"
    assert "localhost" not in await stored_local(ext_env.sw, "allowedSites")

    await panel.get_by_test_id("allow-yes").click(timeout=UI_TIMEOUT_MS)

    await wait_run_state(panel, "Listo.")
    await expect(work_page).to_have_url(re.compile(rf"^http://localhost:{port}/form\.html"))
    assert "localhost" in await stored_local(ext_env.sw, "allowedSites")
    assert [payload["host"] for payload in bridge_log.of("ui.allow_site")] == ["localhost"]


async def test_allow_site_denied_stops_run(session, panel, ext_env, work_page, fake_llm, pages_url, submit_task, bridge_log):
    port = pages_url.rsplit(":", 1)[1]
    fake_llm.push({"steps": [{"action": "goto", "value": f"http://localhost:{port}/form.html"}]})

    await submit_task("Abre la página de prueba")
    await expect(panel.get_by_test_id("allow-card")).to_be_visible(timeout=UI_TIMEOUT_MS)
    await panel.get_by_test_id("allow-no").click(timeout=UI_TIMEOUT_MS)

    await wait_run_state(panel, "Detenido")
    await wait_until(lambda: not session.running, what="the run to end")
    assert work_page.url == "about:blank"
    assert "localhost" not in await stored_local(ext_env.sw, "allowedSites")
    assert bridge_log.count("tab.goto") == 0
    assert len(fake_llm.calls) == 1


async def test_captcha_handoff(session, panel, work_page, fake_llm, fixture_url, submit_task, field_value):
    fake_llm.push(
        {"steps": [{"action": "goto", "value": fixture_url("captcha.html"), "explanation": "Voy a abrir la página."}]},
        {"steps": [{"action": "done", "value": "Ya continuó."}]},
    )

    await submit_task("Consulta mi trámite")

    ask = panel.get_by_test_id("ask-card")
    await expect(ask).to_be_visible(timeout=UI_TIMEOUT_MS)
    await expect(panel.get_by_test_id("ask-question")).to_have_text(CAPTCHA_TEXT)
    await expect(panel.get_by_test_id("handoff-done")).to_have_text("Listo")
    assert len(fake_llm.calls) == 1
    assert await work_page.is_checked("#robot") is False

    await panel.get_by_test_id("handoff-done").click(timeout=UI_TIMEOUT_MS)

    await wait_run_state(panel, "Listo.")
    assert len(fake_llm.calls) == 2
    assert await work_page.is_checked("#robot") is False
    await expect(ask).to_have_count(0)


async def test_captcha_element_is_never_clicked(session, panel, work_page, fake_llm, fixture_url, submit_task, bridge_log):
    fake_llm.push(
        {"steps": [{"action": "goto", "value": fixture_url("captcha.html")}]},
        {"steps": [{"action": "click", "text": "No soy un robot", "explanation": "Voy a marcar la casilla."}]},
        {"steps": [{"action": "done", "value": "Listo."}]},
    )

    await submit_task("Consulta mi trámite")
    done_button = panel.get_by_test_id("handoff-done")
    await expect(done_button).to_be_visible(timeout=UI_TIMEOUT_MS)
    await done_button.click(timeout=UI_TIMEOUT_MS)

    # The page-level handoff is over; clicking the challenge itself is a second, separate handoff.
    await expect(panel.get_by_test_id("ask-question")).to_have_text(CAPTCHA_TEXT, timeout=UI_TIMEOUT_MS)
    await expect(done_button).to_be_visible()
    await done_button.click(timeout=UI_TIMEOUT_MS)

    await wait_run_state(panel, "Listo.")
    assert await work_page.is_checked("#robot") is False
    assert bridge_log.of("page.act") == []


async def test_password_handoff(session, panel, work_page, fake_llm, fixture_url, submit_task, bridge_log, field_value):
    fake_llm.push(
        {"steps": [{"action": "goto", "value": fixture_url("login.html"), "explanation": "Voy a abrir el acceso."}]},
        {"steps": [{"action": "type_text", "text": "Contraseña", "value": "secreto-123", "explanation": "Voy a escribir la contraseña."}]},
        {"steps": [{"action": "done", "value": "Ya entró."}]},
    )

    await submit_task("Entra a mi cuenta")

    ask = panel.get_by_test_id("ask-card")
    await expect(ask).to_be_visible(timeout=UI_TIMEOUT_MS)
    await expect(panel.get_by_test_id("ask-question")).to_have_text(HANDOFF_TEXTS["password_field"])
    assert await field_value("#clave") == ""

    await panel.get_by_test_id("handoff-done").click(timeout=UI_TIMEOUT_MS)

    await wait_run_state(panel, "Listo.")
    assert await field_value("#clave") == ""
    assert [p for p in bridge_log.of("page.act") if p.get("op") == "type"] == []
    assert "secreto-123" not in await panel.inner_text("body")
    assert "secreto-123" not in json.dumps([payload for kind, payload in bridge_log if kind.startswith("ui.")])


async def test_non_pii_typing_needs_no_confirmation(session, panel, fake_llm, open_fixture, submit_task, bridge_log, field_value):
    await open_fixture("form.html")
    fake_llm.push(
        {
            "steps": [
                {"action": "type_text", "text": "Nombre", "value": "María López", "explanation": "Voy a escribir su nombre."},
                {"action": "done", "value": "Escribí su nombre."},
            ]
        }
    )

    await submit_task("Escribe mi nombre")

    await wait_run_state(panel, "Listo.")
    assert await field_value("#nombre") == "María López"
    assert bridge_log.count("ui.confirm") == 0
    await expect(panel.get_by_test_id("confirm-card")).to_have_count(0)


@pytest.mark.parametrize("approve", [False, True], ids=["no", "yes"])
async def test_enter_in_non_search_form_is_confirmed(
    session, panel, fake_llm, open_fixture, submit_task, bridge_log, field_value, approve
):
    await open_fixture("form.html")
    fake_llm.push(
        {
            "steps": [
                {"action": "type_text", "text": "Nombre", "value": "María López"},
                {"action": "press_key", "value": "Enter", "text": "Nombre", "explanation": "Voy a enviar el formulario."},
            ]
        }
    )

    await submit_task("Llena mi nombre y envía")

    card = panel.get_by_test_id("confirm-card")
    await expect(card).to_be_visible(timeout=UI_TIMEOUT_MS)
    await expect(panel.get_by_test_id("confirm-title")).to_have_text("Enviar un formulario")
    await expect(panel.get_by_test_id("confirm-line")).to_have_text(["Presionar la tecla Enter en «Nombre» (127.0.0.1)"])
    assert await field_value("#nombre") == "María López"
    assert await field_value("#result") == ""

    await panel.get_by_test_id("confirm-yes" if approve else "confirm-no").click(timeout=UI_TIMEOUT_MS)

    if approve:
        await wait_run_state(panel, "Listo.")
        assert await field_value("#result") == "Enviado: María López"
    else:
        await wait_run_state(panel, "Detenido")
        assert await field_value("#result") == ""
    assert bridge_log.count("ui.confirm") == 1


@pytest.mark.parametrize("approve", [False, True], ids=["no", "yes"])
async def test_submit_button_is_confirmed(session, panel, fake_llm, open_fixture, submit_task, bridge_log, field_value, approve):
    await open_fixture("form.html")
    fake_llm.push(
        {"steps": [{"action": "click", "text": "Enviar solicitud", "explanation": "Voy a enviar la solicitud."}]},
        {"steps": [{"action": "done", "value": "Ya la envié."}]},
    )

    await submit_task("Envía la solicitud")

    await expect(panel.get_by_test_id("confirm-card")).to_be_visible(timeout=UI_TIMEOUT_MS)
    await expect(panel.get_by_test_id("confirm-title")).to_have_text("Enviar un formulario")
    await expect(panel.get_by_test_id("confirm-line")).to_have_text(["Presionar «Enviar solicitud» en 127.0.0.1"])
    assert await field_value("#result") == ""

    await panel.get_by_test_id("confirm-yes" if approve else "confirm-no").click(timeout=UI_TIMEOUT_MS)

    if approve:
        await wait_run_state(panel, "Listo.")
        assert (await field_value("#result")).startswith("Enviado")
    else:
        await wait_run_state(panel, "Detenido")
        await wait_until(lambda: not session.running, what="the run to end")
        assert await field_value("#result") == ""
        assert [p for p in bridge_log.of("page.act") if p.get("op") == "click"] == []
        assert len(fake_llm.calls) == 1


async def test_batch_confirmation_single_card(session, panel, fake_llm, open_fixture, submit_task, bridge_log, field_value):
    await open_fixture("form.html")
    fake_llm.push(
        {
            "steps": [
                {"action": "type_text", "text": "Correo", "value": EMAIL, "explanation": "Voy a llenar sus datos."},
                {"action": "type_text", "text": "Teléfono", "value": PHONE},
                {"action": "type_text", "text": "CURP", "value": CURP},
                {"action": "done", "value": "Ya llené sus datos."},
            ]
        }
    )

    await submit_task("Llena mis datos")

    await expect(panel.get_by_test_id("confirm-card")).to_be_visible(timeout=UI_TIMEOUT_MS)
    lines = panel.get_by_test_id("confirm-line")
    await expect(lines).to_have_count(3)
    await expect(lines).to_have_text(
        [
            f"Escribir «{EMAIL}» en «Correo» (127.0.0.1)",
            f"Escribir «{PHONE}» en «Teléfono» (127.0.0.1)",
            f"Escribir «{CURP}» en «CURP» (127.0.0.1)",
        ]
    )
    assert await field_value("#correo") == ""

    await panel.get_by_test_id("confirm-yes").click(timeout=UI_TIMEOUT_MS)

    await wait_run_state(panel, "Listo.")
    assert (await field_value("#correo"), await field_value("#telefono"), await field_value("#curp")) == (EMAIL, PHONE, CURP)
    assert bridge_log.count("ui.confirm") == 1
    typed = [p for p in bridge_log.of("page.act") if p["op"] == "type"]
    assert len(typed) == 3
    assert len({p["confirmation_id"] for p in typed}) == 1
    await expect(panel.get_by_test_id("confirm-card")).to_have_count(0)


async def test_pii_never_in_step_log(session, panel, fake_llm, open_fixture, submit_task, bridge_log, field_value, caplog):
    caplog.set_level(logging.DEBUG, logger="babas")
    await open_fixture("form.html")
    fake_llm.push(
        {
            "steps": [
                {"action": "type_text", "text": "Correo", "value": EMAIL, "explanation": f"Voy a escribir {EMAIL}."},
                {"action": "type_text", "text": "CURP", "value": CURP},
            ]
        },
        {"steps": [{"action": "done", "value": f"Escribí los datos de {EMAIL}."}]},
    )

    await submit_task("Llena mi correo y mi CURP")

    card = panel.get_by_test_id("confirm-card")
    await expect(card).to_be_visible(timeout=UI_TIMEOUT_MS)
    card_text = await card.inner_text()
    assert EMAIL in card_text and CURP in card_text
    await panel.get_by_test_id("confirm-yes").click(timeout=UI_TIMEOUT_MS)

    await wait_run_state(panel, "Listo.")
    assert await field_value("#correo") == EMAIL
    assert await field_value("#curp") == CURP

    entries = await log_entries(panel)
    shown = "\n".join([text for _, text in entries] + [await panel.inner_text("body")])
    for raw in (EMAIL, CURP):
        assert raw not in shown
    steps = [text for kind, text in entries if kind == "step"]
    assert any(mask(CURP) in text for text in steps), steps
    assert any(mask(EMAIL) in text for text in steps), steps

    assert len(fake_llm.calls) == 2
    for _, user in fake_llm.calls:
        assert EMAIL not in user and CURP not in user
    assert mask(CURP) in fake_llm.calls[1][1]

    for kind, payload in bridge_log:
        if kind not in ("ui.confirm", "page.act", "page.describe"):
            assert EMAIL not in json.dumps(payload) and CURP not in json.dumps(payload), kind

    formatter = logging.Formatter()
    logged = "\n".join(formatter.format(record) for record in caplog.records if record.name.startswith("babas"))
    assert logged
    assert EMAIL not in logged and CURP not in logged



async def test_pairing_flow(ext_env, point_extension_at, tmp_path):
    pairing = tmp_path / "state" / "pairing.json"
    servers: List[BridgeServer] = []

    async def start_server() -> BridgeServer:
        server = BridgeServer(port=0, pairing_file=pairing)
        await server.start()
        servers.append(server)
        await point_extension_at(server)
        return server

    async def wait_for_stored_token(expected: str) -> None:
        for _ in range(100):
            if await stored_local(ext_env.sw, "pairToken") == expected:
                return
            await asyncio.sleep(0.1)
        assert await stored_local(ext_env.sw, "pairToken") == expected

    def file_token() -> str:
        return json.loads(pairing.read_text(encoding="utf-8"))["token"]

    try:
        assert await stored_local(ext_env.sw, "pairToken") is None

        first = await start_server()
        assert await first.wait_connected(15), "the extension did not connect to a bridge with no pairing yet"
        token = file_token()
        assert len(token) >= 32
        assert stat.S_IMODE(os.stat(pairing).st_mode) == 0o600
        await wait_for_stored_token(token)

        await first.stop()
        second = await start_server()
        assert await second.wait_connected(15), "the extension did not reconnect with its stored token"
        assert file_token() == token

        await second.stop()
        pairing.write_text(json.dumps({"token": "another-extension"}), encoding="utf-8")
        third = await start_server()
        assert not await third.wait_connected(3), "a bridge paired with someone else accepted this extension"
        assert await stored_local(ext_env.sw, "pairToken") == token

        assert reset_pairing(pairing) is True
        assert await third.wait_connected(15), "the extension was not accepted after the pairing was reset"
        renewed = file_token()
        assert renewed != token
        await wait_for_stored_token(renewed)
    finally:
        for server in servers:
            await server.stop()
