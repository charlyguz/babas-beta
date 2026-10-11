import asyncio
from typing import List, Optional

import pytest

from babas.drivers.base import DriverError, Element, ErrorCode, PageState, Target

pytestmark = pytest.mark.e2e

URL_WAIT_SECONDS = 10.0


def labelled(state: PageState, label: str, role: Optional[str] = None) -> List[Element]:
    return [e for e in state.elements if e.label == label and (role is None or e.role == role)]


def only(state: PageState, label: str, role: Optional[str] = None) -> Element:
    found = labelled(state, label, role)
    assert len(found) == 1, f"expected exactly one {label!r}, got {[(e.index, e.role, e.label) for e in state.elements]}"
    return found[0]


def at(state: PageState, element: Element) -> Target:
    return Target(index=element.index, snapshot_id=state.snapshot_id)


async def open_page(driver, pages_url: str, name: str) -> PageState:
    await driver.goto(f"{pages_url}/{name}")
    return await driver.get_page_state()


async def wait_for_url_suffix(driver, suffix: str) -> str:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + URL_WAIT_SECONDS
    url = await driver.current_url()
    while not url.endswith(suffix) and loop.time() < deadline:
        await asyncio.sleep(0.1)
        url = await driver.current_url()
    return url


async def test_goto_and_current_url(driver, pages_url):
    await driver.goto(f"{pages_url}/form.html")
    assert (await driver.current_url()).endswith("/form.html")
    state = await driver.get_page_state()
    assert state.url.endswith("/form.html")
    assert state.title == "Formulario de prueba"
    assert state.tab == driver.current_tab == "main"
    assert driver.list_tabs() == ["main"]


async def test_flags(driver, pages_url):
    state = await open_page(driver, pages_url, "form.html")

    search = labelled(state, "Buscar")
    assert {e.role for e in search} == {"textbox", "button"}
    assert all(e.has("search_form") for e in search)

    correo = only(state, "Correo")
    assert correo.has("pii")
    assert correo.type == "email"
    assert only(state, "Teléfono").has("pii")
    assert only(state, "CURP").has("pii")
    assert not only(state, "Nombre").has("pii")

    enviar = only(state, "Enviar solicitud")
    assert enviar.has("submit")
    assert enviar.has("consequential")
    assert not enviar.has("search_form")

    estado = only(state, "Estado")
    assert estado.options == ["Aguascalientes", "Jalisco", "Yucatán"]
    assert estado.value == "Aguascalientes"

    assert only(state, "Acepto").checked is False
    assert not state.has_password
    assert not state.has_captcha


async def test_describe_and_is_visible(driver, pages_url):
    await open_page(driver, pages_url, "form.html")

    element = await driver.describe(Target(selector="#show"))
    assert element.label == "Mostrar mensaje"
    assert element.role == "button"
    assert not element.has("consequential")

    assert await driver.is_visible(Target(selector="#show")) is True
    assert await driver.is_visible(Target(text="Mostrar mensaje")) is True
    assert await driver.is_visible(Target(selector="#no-existe")) is False


async def test_type_text_by_index(driver, pages_url):
    state = await open_page(driver, pages_url, "form.html")
    nombre = only(state, "Nombre")
    assert nombre.value == ""

    await driver.type_text(at(state, nombre), "María López")

    after = await driver.get_page_state()
    assert only(after, "Nombre").value == "María López"
    assert only(after, "Comentarios").value == ""


async def test_click_by_index_text_and_selector(driver, pages_url):
    state = await open_page(driver, pages_url, "form.html")
    show = only(state, "Mostrar mensaje")
    before = await driver.read_text()
    assert "Mensaje mostrado" not in before
    assert "Clics: 0" in before

    targets = [
        at(state, show),
        Target(text="Mostrar mensaje"),
        Target(selector="#show"),
        Target(selector="button:has-text('Mostrar')"),
    ]
    for count, target in enumerate(targets, start=1):
        await driver.click(target)
        text = await driver.read_text()
        assert "Mensaje mostrado" in text, f"click by {target.describe()} did not reach the button"
        assert f"Clics: {count}" in text, f"click by {target.describe()} did not run exactly once: {text!r}"


async def test_read_text_of_a_target(driver, pages_url):
    await open_page(driver, pages_url, "form.html")
    text = await driver.read_text(Target(selector="#ayuda"))
    assert "oficina local" in text
    assert "Clics" not in text


async def test_select_option(driver, pages_url):
    state = await open_page(driver, pages_url, "form.html")
    estado = only(state, "Estado")

    assert await driver.select_option(at(state, estado), "jalisco") == "Jalisco"
    assert only(await driver.get_page_state(), "Estado").value == "Jalisco"

    assert await driver.select_option(Target(text="Estado"), "Yucatan") == "Yucatán"
    assert only(await driver.get_page_state(), "Estado").value == "Yucatán"


async def test_scroll_brings_last_link_into_view(driver, pages_url):
    state = await open_page(driver, pages_url, "form.html")
    assert only(state, "Volver arriba").in_viewport is False
    assert only(state, "Mostrar mensaje").in_viewport is True

    await driver.scroll("down", 3000)

    after = await driver.get_page_state()
    assert after.snapshot_id != state.snapshot_id
    assert only(after, "Volver arriba").in_viewport is True


async def test_click_link_navigates(driver, pages_url):
    state = await open_page(driver, pages_url, "nav_a.html")
    link = only(state, "Ir a B")
    assert state.title == "Página A"

    await driver.click(at(state, link))

    assert (await wait_for_url_suffix(driver, "nav_b.html")).endswith("nav_b.html")
    assert (await driver.get_page_state()).title == "Página B"


async def test_shadow_dom_button_is_listed_and_clickable(driver, pages_url):
    state = await open_page(driver, pages_url, "shadow.html")
    button = only(state, "Botón sombra", "button")
    assert "sombra ok" not in await driver.read_text()

    await driver.click(at(state, button))

    assert "sombra ok" in await driver.read_text()


async def test_overlay_blocks_click_until_cookies_are_accepted(driver, pages_url):
    state = await open_page(driver, pages_url, "overlay.html")
    continuar = only(state, "Continuar")
    aceptar = only(state, "Aceptar cookies")
    assert "continuado" not in await driver.read_text()

    with pytest.raises(DriverError) as blocked:
        await driver.click(at(state, continuar))
    assert blocked.value.code in {ErrorCode.OBSCURED, ErrorCode.TIMEOUT}
    assert "continuado" not in await driver.read_text()

    await driver.click(at(state, aceptar))

    fresh = await driver.get_page_state()
    assert labelled(fresh, "Aceptar cookies") == []
    await driver.click(at(fresh, only(fresh, "Continuar")))
    assert "continuado" in await driver.read_text()


async def test_password_field_is_refused(driver, pages_url):
    state = await open_page(driver, pages_url, "login.html")
    assert state.has_password
    clave = only(state, "Contraseña")
    assert clave.has("password")
    assert clave.value is None
    usuario = only(state, "Usuario")

    with pytest.raises(DriverError) as refused:
        await driver.type_text(at(state, clave), "secreto123")
    assert refused.value.code == ErrorCode.PASSWORD_FIELD

    await driver.type_text(at(state, usuario), "ana")
    after = await driver.get_page_state()
    assert only(after, "Usuario").value == "ana"
    assert only(after, "Contraseña").value is None


async def test_old_snapshot_id_is_stale(driver, pages_url):
    old = await open_page(driver, pages_url, "form.html")
    ayuda = only(old, "Ayuda")
    current = await driver.get_page_state()
    assert current.snapshot_id != old.snapshot_id

    with pytest.raises(DriverError) as stale:
        await driver.click(at(old, ayuda))
    assert stale.value.code == ErrorCode.STALE_SNAPSHOT

    await driver.click(at(current, only(current, "Ayuda")))


async def test_extension_requires_confirmation_for_pii_value(extension_driver, pages_url):
    state = await open_page(extension_driver, pages_url, "form.html")
    correo = only(state, "Correo")

    with pytest.raises(DriverError) as gated:
        await extension_driver.type_text(at(state, correo), "ana@example.com")

    assert gated.value.code == ErrorCode.CONFIRMATION_REQUIRED
    assert "pii_field" in gated.value.data.get("reasons", [])
    assert only(await extension_driver.get_page_state(), "Correo").value == ""


async def test_extension_requires_confirmation_for_submit(extension_driver, pages_url):
    state = await open_page(extension_driver, pages_url, "form.html")
    enviar = only(state, "Enviar solicitud")

    with pytest.raises(DriverError) as gated:
        await extension_driver.click(at(state, enviar))

    assert gated.value.code == ErrorCode.CONFIRMATION_REQUIRED
    reasons = gated.value.data.get("reasons", [])
    assert "submit_form" in reasons
    assert "Enviado" not in await extension_driver.read_text()
