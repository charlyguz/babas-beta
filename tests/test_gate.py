import asyncio
import builtins
from typing import List

import pytest

from babas import channels as channels_module
from babas.channels import ConfirmRequest, ConfirmResult, ConsoleChannel, DenyChannel, PanelChannel
from babas.drivers.base import BrowserDriver, DriverError, ErrorCode, Target
from babas.drivers.extension_driver import ExtensionDriver, act_payload
from babas.gate import ActionDenied, HandoffRequired, SafeDriver
from babas.safety import HANDOFF_TEXTS, SafetyPolicy
from helpers import FakeBridge, FakeDriver, FakeLLM, RecordingChannel, make_element, make_state

CURP = "GOMC800101HDFRRL09"
SNAPSHOT = "s1"


def build_state(url="https://www.gob.mx/tramites"):
    return make_state(
        make_element(0, "textbox", "Correo", flags=["pii"]),
        make_element(1, "textbox", "Nombre"),
        make_element(2, "textbox", "Contraseña", flags=["password"]),
        make_element(3, "button", "Enviar", flags=["submit", "consequential"]),
        make_element(4, "link", "Inicio"),
        make_element(5, "textbox", "CURP", flags=["pii"]),
        make_element(6, "checkbox", "No soy un robot", tag="input", flags=["captcha"]),
        make_element(7, "searchbox", "Buscar", flags=["search_form"]),
        make_element(8, "combobox", "Estado", tag="select"),
        url=url,
        snapshot_id=SNAPSHOT,
    )


def at(index: int) -> Target:
    return Target(index=index, snapshot_id=SNAPSHOT)


def make_gate(channel=None, driver=None, policy=None):
    driver = driver or FakeDriver(build_state())
    channel = channel if channel is not None else RecordingChannel()
    return SafeDriver(driver, policy or SafetyPolicy(), channel), driver, channel


def mutating(driver: FakeDriver, method: str):
    return driver.calls_of(method)


def test_safe_driver_is_a_browser_driver_and_fake_driver_is_complete():
    gate, driver, _ = make_gate()
    assert isinstance(gate, BrowserDriver) and isinstance(driver, BrowserDriver)
    assert gate.name == driver.name


async def test_pii_field_asks_once_then_the_call_carries_the_id():
    gate, driver, channel = make_gate(RecordingChannel(mint_ids=True))
    await gate.type_text(at(0), "ana@example.com", explanation="Voy a escribir su correo.")
    assert len(channel.confirms) == 1
    request = channel.confirms[0]
    assert request.reasons == ["pii_field", "pii_value"]
    assert request.explanation == "Voy a escribir su correo."
    assert request.actions == [{"op": "type", "target": {"index": 0, "snapshot_id": SNAPSHOT}, "text": "ana@example.com"}]
    assert request.lines == ["Escribir «ana@example.com» en «Correo» (gob.mx)"]
    assert mutating(driver, "type_text") == [((at(0), "ana@example.com"), {"confirmation": "conf-1"})]


async def test_console_style_channel_without_ids_still_works():
    gate, driver, channel = make_gate(RecordingChannel())
    await gate.type_text(at(0), "ana@example.com")
    assert len(channel.confirms) == 1
    assert mutating(driver, "type_text")[0][1] == {"confirmation": None}


async def test_plain_actions_run_without_asking():
    gate, driver, channel = make_gate()
    await gate.type_text(at(1), "Ana")
    await gate.click(at(4))
    await gate.press_key("Tab", at(1))
    assert await gate.select_option(at(8), "Jalisco") == "Jalisco"
    assert channel.confirms == []
    assert [call[0] for call in driver.calls if call[0] in {"type_text", "click", "press_key", "select_option"}] == [
        "type_text",
        "click",
        "press_key",
        "select_option",
    ]


async def test_search_box_enter_needs_no_confirmation():
    gate, driver, channel = make_gate()
    await gate.type_text(at(7), "recetas de pozole")
    await gate.press_key("Enter", at(7))
    assert channel.confirms == []
    assert len(mutating(driver, "press_key")) == 1


@pytest.mark.parametrize(
    "run,method",
    [
        (lambda g: g.type_text(at(0), "x"), "type_text"),
        (lambda g: g.click(at(3)), "click"),
        (lambda g: g.press_key("Enter", at(1)), "press_key"),
    ],
)
async def test_denial_raises_action_denied_and_nothing_is_sent(run, method):
    gate, driver, channel = make_gate(RecordingChannel(approve=False))
    with pytest.raises(ActionDenied) as info:
        await run(gate)
    assert info.value.reason == "denied"
    assert len(channel.confirms) == 1
    assert mutating(driver, method) == []


async def test_no_ui_is_reported_as_no_ui():
    gate, driver, _ = make_gate(DenyChannel())
    with pytest.raises(ActionDenied) as info:
        await gate.click(at(3))
    assert info.value.reason == "no_ui"
    assert mutating(driver, "click") == []


async def test_force_confirm_llm_consequential_and_explanation_reach_the_channel():
    gate, driver, channel = make_gate()
    await gate.click(at(4), force_confirm=True, llm_consequential=True, explanation="Enviaré el correo.")
    assert channel.confirms[0].reasons == ["macro", "llm_consequential"]
    assert channel.confirms[0].explanation == "Enviaré el correo."
    assert channel.confirms[0].lines == ["Presionar «Inicio» en gob.mx"]
    assert len(mutating(driver, "click")) == 1


async def test_password_field_is_a_handoff_before_anything_is_typed():
    gate, driver, channel = make_gate()
    with pytest.raises(HandoffRequired) as info:
        await gate.type_text(at(2), "secreto")
    assert info.value.kind == "password_field"
    assert info.value.message == HANDOFF_TEXTS["password_field"]
    assert channel.confirms == [] and mutating(driver, "type_text") == []


async def test_typing_a_character_key_into_a_password_field_is_a_handoff():
    gate, driver, _ = make_gate()
    with pytest.raises(HandoffRequired):
        await gate.press_key("a", at(2))
    assert mutating(driver, "press_key") == []


@pytest.mark.parametrize("run", [lambda g: g.click(at(6)), lambda g: g.type_text(at(6), "x"), lambda g: g.press_key("Enter", at(6))])
async def test_captcha_is_a_handoff(run):
    gate, driver, channel = make_gate()
    with pytest.raises(HandoffRequired) as info:
        await run(gate)
    assert info.value.kind == "captcha"
    assert info.value.message == HANDOFF_TEXTS["captcha"]
    assert channel.confirms == []
    assert not any(call[0] in {"click", "type_text", "press_key"} for call in driver.calls)


async def test_password_error_from_the_driver_becomes_a_handoff():
    driver = FakeDriver(build_state(), errors={"type_text": DriverError(ErrorCode.PASSWORD_FIELD, "no")})
    gate, _, channel = make_gate(driver=driver)
    with pytest.raises(HandoffRequired) as info:
        await gate.type_text(at(1), "x")
    assert info.value.kind == "password_field"
    assert channel.confirms == []


async def test_confirmation_required_from_the_driver_asks_once_and_retries():
    refusal = DriverError(ErrorCode.CONFIRMATION_REQUIRED, "needs it", {"reasons": ["pii_field"], "label": "Nombre"})
    driver = FakeDriver(build_state(), errors={"type_text": refusal})
    gate, _, channel = make_gate(RecordingChannel(mint_ids=True), driver)
    await gate.type_text(at(1), "Ana")
    assert len(channel.confirms) == 1
    assert channel.confirms[0].reasons == ["pii_field"]
    assert channel.confirms[0].lines == ["Escribir «Ana» en «Nombre» (gob.mx)"]
    calls = mutating(driver, "type_text")
    assert [kwargs["confirmation"] for _, kwargs in calls] == [None, "conf-1"]


async def test_a_second_confirmation_required_propagates():
    refusal = DriverError(ErrorCode.CONFIRMATION_REQUIRED, "needs it", {"reasons": ["consequential"]})
    driver = FakeDriver(build_state(), errors={"click": [refusal, refusal]})
    gate, _, channel = make_gate(RecordingChannel(mint_ids=True), driver)
    with pytest.raises(DriverError) as info:
        await gate.click(at(4))
    assert info.value.code == ErrorCode.CONFIRMATION_REQUIRED
    assert len(channel.confirms) == 1
    assert len(mutating(driver, "click")) == 2


async def test_confirmation_required_then_denied_stops():
    refusal = DriverError(ErrorCode.CONFIRMATION_REQUIRED, "needs it", {"reasons": ["consequential"]})
    driver = FakeDriver(build_state(), errors={"click": refusal})
    gate, _, _ = make_gate(RecordingChannel(approve=False), driver)
    with pytest.raises(ActionDenied):
        await gate.click(at(4))
    assert len(mutating(driver, "click")) == 1


async def test_a_driver_confirmation_refusal_without_reasons_still_asks():
    refusal = DriverError(ErrorCode.CONFIRMATION_REQUIRED, "needs it")
    driver = FakeDriver(build_state(), errors={"click": refusal})
    gate, _, channel = make_gate(driver=driver)
    await gate.click(at(4))
    assert len(channel.confirms) == 1 and channel.confirms[0].reasons == ["consequential"]


async def test_other_driver_errors_pass_through_untouched():
    driver = FakeDriver(build_state(), errors={"click": DriverError(ErrorCode.OBSCURED, "covered", {"blocker": "x"})})
    gate, _, channel = make_gate(driver=driver)
    with pytest.raises(DriverError) as info:
        await gate.click(at(4))
    assert info.value.code == ErrorCode.OBSCURED and info.value.data == {"blocker": "x"}
    assert channel.confirms == []


async def test_unknown_target_is_not_found_and_never_asks():
    gate, driver, channel = make_gate()
    with pytest.raises(DriverError) as info:
        await gate.click(Target(text="No existe"))
    assert info.value.code == ErrorCode.NOT_FOUND
    assert channel.confirms == [] and mutating(driver, "click") == []


async def test_text_targets_are_described_by_label():
    gate, driver, channel = make_gate()
    await gate.click(Target(text="enviar"))
    assert channel.confirms[0].reasons == ["submit_form", "consequential"]
    assert mutating(driver, "click")[0][0] == (Target(text="enviar"),)


async def test_press_enter_without_target_uses_the_focused_element():
    driver = FakeDriver(build_state(), selectors={":focus": make_element(1, "textbox", "Nombre")})
    gate, _, channel = make_gate(driver=driver)
    await gate.press_key("Enter")
    assert channel.confirms[0].reasons == ["enter_submits_form"]
    assert channel.confirms[0].actions == [{"op": "press", "key": "Enter"}]
    assert channel.confirms[0].lines == ["Presionar la tecla Enter en «Nombre» (gob.mx)"]
    assert Target(selector=":focus") in [args[0] for args, _ in driver.calls_of("describe")]


async def test_press_enter_with_nothing_focused_fails_closed():
    gate, driver, channel = make_gate()
    await gate.press_key("Enter")
    assert channel.confirms[0].reasons == ["enter_submits_form"]
    assert channel.confirms[0].lines == ["Presionar la tecla Enter en «la página» (gob.mx)"]
    assert len(mutating(driver, "press_key")) == 1


async def test_press_without_target_and_harmless_key_runs_without_asking():
    gate, driver, channel = make_gate()
    await gate.press_key("Escape")
    assert channel.confirms == [] and len(mutating(driver, "press_key")) == 1


async def test_a_forged_confirmation_id_does_not_skip_the_prompt():
    gate, driver, channel = make_gate(RecordingChannel(mint_ids=True))
    await gate.click(at(3), confirmation="forged-id")
    assert len(channel.confirms) == 1
    assert mutating(driver, "click")[0][1] == {"confirmation": "conf-1"}


async def test_an_id_for_another_action_does_not_cover_this_one():
    gate, driver, channel = make_gate(RecordingChannel(mint_ids=True))
    batch_id = await gate.confirm_batch([("type", at(0), {"text": "ana@example.com"})])
    await gate.type_text(at(0), "otro@example.com", confirmation=batch_id)
    assert len(channel.confirms) == 2


async def test_an_approval_is_spent_after_one_use():
    gate, driver, channel = make_gate(RecordingChannel(mint_ids=True))
    batch_id = await gate.confirm_batch([("type", at(0), {"text": "ana@example.com"})])
    await gate.type_text(at(0), "ana@example.com", confirmation=batch_id)
    await gate.type_text(at(0), "ana@example.com", confirmation=batch_id)
    assert len(channel.confirms) == 2


async def test_batch_of_three_fields_with_two_pii_asks_once_with_two_actions():
    gate, driver, channel = make_gate(RecordingChannel(mint_ids=True))
    items = [
        ("type", at(1), {"text": "Ana Gómez"}),
        ("type", at(0), {"text": "ana@example.com"}),
        ("type", at(5), {"text": CURP}),
    ]
    batch_id = await gate.confirm_batch(items, explanation="Voy a llenar el formulario.")
    assert batch_id == "conf-1"
    assert len(channel.confirms) == 1
    request = channel.confirms[0]
    assert [a["text"] for a in request.actions] == ["ana@example.com", CURP]
    assert request.reasons == ["pii_field", "pii_value"]
    assert request.explanation == "Voy a llenar el formulario."
    assert request.lines == [
        "Escribir «ana@example.com» en «Correo» (gob.mx)",
        f"Escribir «{CURP}» en «CURP» (gob.mx)",
    ]
    await gate.type_text(at(1), "Ana Gómez", confirmation=batch_id)
    await gate.type_text(at(0), "ana@example.com", confirmation=batch_id)
    await gate.type_text(at(5), CURP, confirmation=batch_id)
    assert len(channel.confirms) == 1
    assert [kwargs["confirmation"] for _, kwargs in mutating(driver, "type_text")] == [batch_id, batch_id, batch_id]


async def test_batch_without_ids_is_not_asked_again_one_by_one():
    gate, driver, channel = make_gate(RecordingChannel())
    items = [("type", at(0), {"text": "ana@example.com"}), ("type", at(5), {"text": CURP})]
    batch_id = await gate.confirm_batch(items)
    assert batch_id is None
    await gate.type_text(at(0), "ana@example.com", confirmation=batch_id)
    await gate.type_text(at(5), CURP, confirmation=batch_id)
    assert len(channel.confirms) == 1
    assert len(mutating(driver, "type_text")) == 2


async def test_batch_denied_raises_and_nothing_is_typed():
    gate, driver, channel = make_gate(RecordingChannel(approve=False))
    with pytest.raises(ActionDenied):
        await gate.confirm_batch([("type", at(0), {"text": "ana@example.com"})])
    assert mutating(driver, "type_text") == []


async def test_batch_with_nothing_gated_asks_nothing():
    gate, _, channel = make_gate()
    assert await gate.confirm_batch([("type", at(1), {"text": "Ana"}), ("select", at(8), {"value": "Jalisco"})]) is None
    assert await gate.confirm_batch([]) is None
    assert channel.confirms == []


async def test_batch_with_force_confirm_lists_every_action():
    gate, _, channel = make_gate(RecordingChannel(mint_ids=True))
    items = [("type", at(1), {"text": "Hola, ¿cómo está?"}), ("press", at(1), {"key": "Enter"})]
    batch_id = await gate.confirm_batch(items, force_confirm=True, explanation="Enviaré su mensaje.")
    assert batch_id == "conf-1" and len(channel.confirms) == 1
    request = channel.confirms[0]
    assert request.actions == [act_payload("type", at(1), text="Hola, ¿cómo está?"), act_payload("press", at(1), key="Enter")]
    assert "macro" in request.reasons
    assert request.lines[0] == "Escribir «Hola, ¿cómo está?» en «Nombre» (gob.mx)"


async def test_batch_with_a_password_field_is_a_handoff_before_asking():
    gate, _, channel = make_gate()
    with pytest.raises(HandoffRequired) as info:
        await gate.confirm_batch([("type", at(0), {"text": "ana@example.com"}), ("type", at(2), {"text": "x"})])
    assert info.value.kind == "password_field"
    assert channel.confirms == []


async def test_batch_select_items_use_value():
    gate, _, channel = make_gate()
    await gate.confirm_batch([("select", at(8), {"value": CURP})])
    assert channel.confirms[0].actions == [{"op": "select", "target": {"index": 8, "snapshot_id": SNAPSHOT}, "value": CURP}]
    assert channel.confirms[0].reasons == ["pii_value"]


async def test_a_channel_that_raises_fails_closed():
    gate, driver, _ = make_gate(RecordingChannel(confirm_error=RuntimeError("boom")))
    with pytest.raises(ActionDenied) as info:
        await gate.click(at(3))
    assert info.value.reason == "denied"
    assert mutating(driver, "click") == []


async def test_a_channel_that_times_out_fails_closed():
    gate, driver, _ = make_gate(RecordingChannel(confirm_error=asyncio.TimeoutError()))
    with pytest.raises(ActionDenied):
        await gate.type_text(at(0), "x")
    assert mutating(driver, "type_text") == []


async def test_a_timed_out_answer_from_the_extension_fails_closed():
    gate, driver, _ = make_gate(RecordingChannel(approve=False, deny_reason="timeout"))
    with pytest.raises(ActionDenied) as info:
        await gate.click(at(3))
    assert info.value.reason == "denied" and info.value.detail == "timeout"
    assert mutating(driver, "click") == []


async def test_a_batch_confirmation_that_raises_fails_closed():
    gate, _, _ = make_gate(RecordingChannel(confirm_error=RuntimeError("boom")))
    with pytest.raises(ActionDenied):
        await gate.confirm_batch([("type", at(0), {"text": "x"})])


async def test_cancellation_during_a_confirmation_propagates():
    gate, driver, channel = make_gate(RecordingChannel(delay=30))
    task = asyncio.create_task(gate.click(at(3)))
    await asyncio.sleep(0.05)
    assert len(channel.confirms) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert mutating(driver, "click") == []


async def test_read_only_methods_delegate_without_asking():
    gate, driver, channel = make_gate()
    await gate.get_page_state()
    await gate.describe(at(3))
    assert await gate.is_visible(at(3)) is True
    assert await gate.is_visible(Target(selector="#missing")) is False
    assert await gate.current_url() == driver.url
    await gate.read_text()
    await gate.scroll("up", 200)
    await gate.screenshot()
    await gate.highlight("off")
    await gate.wait(5)
    assert gate.list_tabs() == ["main"] and gate.current_tab == "main"
    await gate.start()
    await gate.close()
    assert channel.calls == []
    assert driver.calls_of("scroll") == [(("up", 200), {})]


async def test_tabs_delegate():
    gate, driver, channel = make_gate()
    name = await gate.new_tab("segunda", "https://www.youtube.com/")
    assert name == "segunda" and gate.current_tab == "segunda"
    await gate.switch_tab("main")
    assert await gate.close_tab("segunda") is True
    assert channel.calls == []


async def test_domain_prompt_approved_allows_goto_and_is_remembered():
    gate, driver, channel = make_gate()
    await gate.goto("https://sat.gob.mx/inicio")
    assert channel.allows == []
    await gate.goto("https://www.example.com/pagina")
    assert channel.allows == [("example.com", "https://www.example.com/pagina")]
    assert driver.url == "https://www.example.com/pagina"
    await gate.goto("https://example.com/otra")
    assert len(channel.allows) == 1
    assert [args[0] for args, _ in driver.calls_of("goto")] == [
        "https://sat.gob.mx/inicio",
        "https://www.example.com/pagina",
        "https://example.com/otra",
    ]


async def test_domain_prompt_denied_raises_site_and_does_not_navigate():
    gate, driver, channel = make_gate(RecordingChannel(allow=False))
    with pytest.raises(ActionDenied) as info:
        await gate.goto("https://example.com/")
    assert info.value.reason == "site"
    assert driver.calls_of("goto") == []
    assert len(channel.allows) == 1


async def test_lookalike_domains_are_prompted():
    gate, driver, channel = make_gate(RecordingChannel(allow=False))
    for url in ("https://gob.mx.evil.com/", "http://evil.com\\@gob.mx/", "https://gob.mx@evil.com/"):
        with pytest.raises(ActionDenied):
            await gate.goto(url)
    assert driver.calls_of("goto") == []
    assert [host for host, _ in channel.allows] == ["gob.mx.evil.com", "evil.com", "evil.com"]


async def test_wildcard_policy_never_prompts():
    gate, driver, channel = make_gate(policy=SafetyPolicy(["*"]))
    await gate.goto("https://example.com/")
    assert channel.allows == [] and len(driver.calls_of("goto")) == 1


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///etc/passwd", "data:text/html,<h1>x</h1>", "chrome://settings", "ftp://example.com/"])
async def test_goto_refuses_non_web_addresses(url):
    gate, driver, channel = make_gate(policy=SafetyPolicy(["*"]))
    with pytest.raises(DriverError) as info:
        await gate.goto(url)
    assert info.value.code == ErrorCode.BAD_REQUEST
    with pytest.raises(DriverError):
        await gate.new_tab(url=url)
    assert driver.calls_of("goto") == [] and driver.calls_of("new_tab") == []
    assert channel.allows == []


async def test_navigation_methods_ignore_the_gate_keywords_they_do_not_use():
    gate, driver, channel = make_gate()
    await gate.goto("https://www.gob.mx/", confirmation="x", force_confirm=True, llm_consequential=True, explanation="y")
    await gate.new_tab("a", "https://www.gob.mx/", confirmation="x", explanation="y")
    await gate.switch_tab("main", confirmation="x")
    await gate.close_tab("a", explanation="y")
    assert channel.calls == []
    assert driver.calls_of("goto") == [(("https://www.gob.mx/",), {})]


async def test_goto_about_blank_is_allowed():
    gate, driver, _ = make_gate()
    await gate.goto("about:blank")
    assert len(driver.calls_of("goto")) == 1


async def test_new_tab_with_url_applies_the_domain_rules():
    gate, driver, channel = make_gate(RecordingChannel(allow=[False, True]))
    with pytest.raises(ActionDenied):
        await gate.new_tab(url="https://example.com/")
    assert driver.calls_of("new_tab") == []
    await gate.new_tab("web", "https://example.com/")
    assert len(driver.calls_of("new_tab")) == 1
    await gate.new_tab("blank")
    assert len(channel.allows) == 2


async def test_get_page_state_applies_the_domain_rules():
    driver = FakeDriver(build_state("https://example.com/cuenta"))
    gate, _, channel = make_gate(RecordingChannel(allow=False), driver)
    with pytest.raises(ActionDenied) as info:
        await gate.get_page_state()
    assert info.value.reason == "site"
    assert driver.calls_of("get_page_state") == []
    channel.allow = True
    state = await gate.get_page_state()
    assert state.url == "https://example.com/cuenta"
    assert channel.allows[-1][0] == "example.com"


async def test_get_page_state_checks_where_the_page_ended_up():
    states = [build_state("https://www.gob.mx/"), build_state("https://example.com/redirigido")]
    driver = FakeDriver(states)
    driver.url = "https://www.gob.mx/"
    gate, _, channel = make_gate(RecordingChannel(allow=False), driver)
    await gate.get_page_state()
    with pytest.raises(ActionDenied):
        await gate.get_page_state()
    assert [host for host, _ in channel.allows] == ["example.com"]


async def test_get_page_state_on_a_browser_page_is_not_a_site():
    driver = FakeDriver(build_state("about:blank"))
    gate, _, channel = make_gate(driver=driver)
    state = await gate.get_page_state()
    assert state.url == "about:blank" and channel.allows == []


def site_error(host="example.com"):
    return DriverError(ErrorCode.DOMAIN_NOT_ALLOWED, "not allowed", {"host": host, "url": f"https://{host}/x"})


async def test_domain_not_allowed_from_the_driver_asks_once_and_retries():
    driver = FakeDriver(build_state(), errors={"click": site_error()})
    gate, _, channel = make_gate(driver=driver)
    await gate.click(at(4))
    assert channel.allows == [("example.com", "https://example.com/x")]
    assert len(mutating(driver, "click")) == 2
    assert gate.policy.domain_allowed("https://example.com/")


async def test_domain_not_allowed_twice_propagates():
    driver = FakeDriver(build_state(), errors={"click": [site_error(), site_error()]})
    gate, _, channel = make_gate(driver=driver)
    with pytest.raises(DriverError) as info:
        await gate.click(at(4))
    assert info.value.code == ErrorCode.DOMAIN_NOT_ALLOWED
    assert len(channel.allows) == 1 and len(mutating(driver, "click")) == 2


async def test_domain_not_allowed_denied_raises_site():
    driver = FakeDriver(build_state(), errors={"goto": site_error("sitio.example")})
    gate, _, channel = make_gate(RecordingChannel(allow=False), driver, SafetyPolicy(["*"]))
    with pytest.raises(ActionDenied) as info:
        await gate.goto("https://sitio.example/")
    assert info.value.reason == "site"
    assert len(driver.calls_of("goto")) == 1


async def test_domain_not_allowed_from_current_url_is_handled_in_observation():
    driver = FakeDriver(build_state(), errors={"current_url": site_error("cuenta.example")})
    gate, _, channel = make_gate(driver=driver)
    state = await gate.get_page_state()
    assert state.url == "https://www.gob.mx/tramites"
    assert channel.allows[0][0] == "cuenta.example"


async def test_domain_not_allowed_without_a_host_propagates():
    driver = FakeDriver(build_state(), errors={"click": DriverError(ErrorCode.DOMAIN_NOT_ALLOWED, "no data")})
    gate, _, channel = make_gate(driver=driver)
    with pytest.raises(DriverError) as info:
        await gate.click(at(4))
    assert info.value.code == ErrorCode.DOMAIN_NOT_ALLOWED and channel.allows == []


async def test_an_allow_site_channel_that_raises_fails_closed():
    class Broken(RecordingChannel):
        async def allow_site(self, host, url):
            raise RuntimeError("boom")

    gate, driver, _ = make_gate(Broken())
    with pytest.raises(ActionDenied) as info:
        await gate.goto("https://example.com/")
    assert info.value.reason == "site" and driver.calls_of("goto") == []


def extension_driver_over(bridge: FakeBridge) -> ExtensionDriver:
    driver = ExtensionDriver(bridge)
    driver._tabs = {"main": 1}
    return driver


def extension_bridge(label="Correo", flags=("pii",)):
    element = make_element(0, "textbox", label, flags=list(flags)).model_dump()
    return FakeBridge(
        {
            "tab.info": {"tab_id": 1, "url": "https://www.gob.mx/tramites", "title": "t"},
            "page.describe": {"element": element, "host": "www.gob.mx", "url": "https://www.gob.mx/tramites"},
            "page.act": {"result": {"label": label}, "tab_id": 1, "url": "https://www.gob.mx/tramites", "title": "t"},
        }
    )


async def test_gate_and_extension_driver_build_the_same_act_payload():
    bridge = extension_bridge()
    channel = RecordingChannel(mint_ids=True)
    gate = SafeDriver(extension_driver_over(bridge), SafetyPolicy(), channel)
    await gate.type_text(at(0), "ana@example.com")
    (sent, _), = bridge.requests_of("page.act")
    assert sent["confirmation_id"] == "conf-1"
    assert channel.confirms[0].actions == [{key: value for key, value in sent.items() if key != "confirmation_id"}]
    assert sent == {
        "op": "type",
        "target": {"index": 0, "snapshot_id": SNAPSHOT},
        "text": "ana@example.com",
        "confirmation_id": "conf-1",
    }


async def test_extension_driver_payloads_for_every_op():
    bridge = extension_bridge()
    driver = extension_driver_over(bridge)
    target = Target(selector="#a")
    await driver.click(target, confirmation="c1")
    await driver.press_key("Enter")
    await driver.press_key("Tab", target)
    await driver.scroll("up", 120)
    await driver.highlight("off")
    bridge.responses["page.act"] = {"result": {"selected": "Jalisco", "text": "hola"}}
    assert await driver.select_option(target, "Jalisco") == "Jalisco"
    assert await driver.read_text() == "hola"
    assert await driver.read_text(target) == "hola"
    sent = [payload for payload, _ in bridge.requests_of("page.act")]
    assert sent == [
        {"op": "click", "target": {"selector": "#a"}, "confirmation_id": "c1"},
        {"op": "press", "key": "Enter"},
        {"op": "press", "target": {"selector": "#a"}, "key": "Tab"},
        {"op": "scroll", "direction": "up", "amount": 120},
        {"op": "highlight", "mode": "off"},
        {"op": "select", "target": {"selector": "#a"}, "value": "Jalisco"},
        {"op": "read"},
        {"op": "read", "target": {"selector": "#a"}},
    ]


def test_act_payload_omits_missing_fields():
    assert act_payload("press", key="Enter") == {"op": "press", "key": "Enter"}
    assert act_payload("type", Target(index=2, snapshot_id="s9"), text="") == {
        "op": "type",
        "target": {"index": 2, "snapshot_id": "s9"},
        "text": "",
    }
    assert act_payload("read", None, max_chars=None, mode="all") == {"op": "read", "mode": "all"}
    assert act_payload("click", {"selector": "#x"}, confirmation_id=None) == {"op": "click", "target": {"selector": "#x"}}


async def test_gate_over_extension_driver_retries_after_a_refusal_from_the_page():
    bridge = extension_bridge(label="Nombre", flags=())
    refusals: List[int] = []

    def act(payload):
        if "confirmation_id" not in payload:
            refusals.append(1)
            raise DriverError(ErrorCode.CONFIRMATION_REQUIRED, "page says so", {"reasons": ["pii_field"]})
        return {"result": {"label": "Nombre"}}

    bridge.responses["page.act"] = act
    channel = RecordingChannel(mint_ids=True)
    gate = SafeDriver(extension_driver_over(bridge), SafetyPolicy(), channel)
    await gate.type_text(at(0), "Ana")
    sent = [payload for payload, _ in bridge.requests_of("page.act")]
    assert len(refusals) == 1 and sent[1]["confirmation_id"] == "conf-1"


def panel(responses=None, **kwargs):
    bridge = FakeBridge(responses)
    return PanelChannel(bridge, **kwargs), bridge


async def test_panel_confirm_sends_the_documented_payload_and_returns_the_id():
    channel, bridge = panel({"ui.confirm": {"approved": True, "confirmation_id": "abc"}})
    actions = [act_payload("type", at(0), text="ana@example.com")]
    result = await channel.confirm(ConfirmRequest(actions, ["pii_field"], f"Su CURP es {CURP}", ["línea"]))
    assert result == ConfirmResult(True, "abc", "")
    (payload, timeout), = bridge.requests_of("ui.confirm")
    assert payload == {
        "actions": actions,
        "reasons": ["pii_field"],
        "explanation": "Su CURP es GO**************09",
        "timeout_s": 300,
    }
    assert timeout == 305


@pytest.mark.parametrize(
    "answer,reason",
    [
        ({"approved": False, "reason": "denied"}, "denied"),
        ({"approved": False, "reason": "timeout"}, "timeout"),
        ({"approved": False, "reason": "stopped"}, "stopped"),
        ({"approved": False}, "denied"),
        ({}, "denied"),
        ({"approved": "yes", "confirmation_id": "abc"}, "denied"),
        ({"approved": True}, "malformed"),
        ({"approved": True, "confirmation_id": ""}, "malformed"),
        ({"approved": True, "confirmation_id": 7}, "malformed"),
    ],
)
async def test_panel_confirm_fails_closed_on_anything_but_a_proper_yes(answer, reason):
    channel, _ = panel({"ui.confirm": answer})
    result = await channel.confirm(ConfirmRequest([], ["pii_field"], ""))
    assert result.approved is False and result.confirmation_id is None and result.reason == reason


async def test_panel_confirm_maps_driver_errors_to_their_code():
    for code in (ErrorCode.TIMEOUT, ErrorCode.NO_EXTENSION, ErrorCode.HALTED):
        channel, _ = panel({"ui.confirm": DriverError(code, "x")})
        result = await channel.confirm(ConfirmRequest([], [], ""))
        assert (result.approved, result.reason) == (False, code)


async def test_panel_confirm_survives_unexpected_exceptions_but_not_cancellation():
    channel, _ = panel({"ui.confirm": RuntimeError("boom")})
    assert (await channel.confirm(ConfirmRequest([], [], ""))).approved is False
    channel, _ = panel({"ui.confirm": asyncio.CancelledError()})
    with pytest.raises(asyncio.CancelledError):
        await channel.confirm(ConfirmRequest([], [], ""))


async def test_panel_ask():
    channel, bridge = panel({"ui.ask": {"answer": "mi respuesta"}})
    assert await channel.ask(f"¿Su CURP es {CURP}?") == "mi respuesta"
    (payload, timeout), = bridge.requests_of("ui.ask")
    assert payload == {"question": "¿Su CURP es GO**************09?", "kind": "text", "timeout_s": 600}
    assert timeout == 605
    assert await channel.ask("Resuelva el CAPTCHA", "handoff") == "mi respuesta"
    assert bridge.requests_of("ui.ask")[1][0]["kind"] == "handoff"
    assert (await panel({"ui.ask": {"answer": ""}})[0].ask("?")) == ""
    for answer in ({"answer": None, "reason": "cancelled"}, {"answer": 5}, {}, DriverError(ErrorCode.TIMEOUT, "x"), RuntimeError("x")):
        assert await panel({"ui.ask": answer})[0].ask("?") is None


async def test_panel_allow_site():
    channel, bridge = panel({"ui.allow_site": {"approved": True}})
    assert await channel.allow_site("example.com", "https://example.com/?curp=" + CURP) is True
    (payload, timeout), = bridge.requests_of("ui.allow_site")
    assert payload["host"] == "example.com" and CURP not in payload["url"] and payload["timeout_s"] == 300
    assert timeout == 305
    for answer in ({"approved": False, "reason": "denied"}, {}, {"approved": "true"}, DriverError(ErrorCode.TIMEOUT, "x"), RuntimeError("x")):
        assert await panel({"ui.allow_site": answer})[0].allow_site("example.com", "https://example.com/") is False


async def test_panel_log_redacts_and_swallows_errors():
    channel, bridge = panel()
    await channel.log("step", f"Escribió {CURP} en «CURP»", True)
    await channel.log("assistant", "Hola")
    await channel.log("rareza", "texto")
    sent = [payload for payload, _ in bridge.requests_of("ui.log")]
    assert sent == [
        {"kind": "step", "text": "Escribió GO**************09 en «CURP»", "ok": True},
        {"kind": "assistant", "text": "Hola"},
        {"kind": "info", "text": "texto"},
    ]
    broken, _ = panel({"ui.log": DriverError(ErrorCode.NO_EXTENSION, "x"), "ui.run_state": RuntimeError("x")})
    await broken.log("info", "x")
    await broken.run_state("running")


async def test_panel_run_state():
    channel, bridge = panel()
    await channel.run_state("running")
    await channel.run_state("done", f"Su CURP {CURP}")
    await channel.run_state("max_steps", "Demasiados pasos")
    sent = [payload for payload, _ in bridge.requests_of("ui.run_state")]
    assert sent == [
        {"state": "running"},
        {"state": "done", "summary": "Su CURP GO**************09"},
        {"state": "failed", "summary": "Demasiados pasos"},
    ]


async def test_panel_timeouts_are_configurable():
    channel, bridge = panel({"ui.confirm": {"approved": False}}, confirm_timeout=10)
    await channel.confirm(ConfirmRequest([], [], ""))
    assert bridge.requests_of("ui.confirm")[0][1] == 15


async def test_deny_channel(caplog):
    channel = DenyChannel()
    result = await channel.confirm(ConfirmRequest([], ["pii_field"], ""))
    assert (result.approved, result.confirmation_id, result.reason) == (False, None, "no_ui")
    assert await channel.ask("?") is None
    assert await channel.allow_site("example.com", "https://example.com/") is False
    with caplog.at_level("INFO", logger="babas.channels"):
        await channel.log("step", f"Escribió {CURP}", True)
        await channel.run_state("stopped", "x")
    assert CURP not in caplog.text and "GO**************09" in caplog.text


def scripted_input(*lines, prompts=None):
    queue = list(lines)

    async def ainput(prompt: str) -> str:
        if prompts is not None:
            prompts.append(prompt)
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    return ainput


async def test_console_confirm_shows_the_full_value_but_redacts_the_explanation(capsys):
    prompts: List[str] = []
    channel = ConsoleChannel(ainput=scripted_input("y", prompts=prompts))
    request = ConfirmRequest([], ["pii_value"], f"Su CURP {CURP}", [f"Escribir «{CURP}» en «CURP» (gob.mx)"])
    result = await channel.confirm(request)
    out = capsys.readouterr().out
    assert result.approved is True and result.confirmation_id is None
    assert f"Escribir «{CURP}» en «CURP» (gob.mx)" in out
    assert "Escribir datos personales" in out
    assert "GO**************09" in out and out.count(CURP) == 1
    assert prompts == ["❯ Continue? (y/N): "]


@pytest.mark.parametrize("answer,approved", [("y", True), ("Y", True), (" yes ", True), ("s", True), ("sí", True), ("si", True), ("n", False), ("", False), ("ok", False), ("claro", False)])
async def test_console_confirm_answers(answer, approved):
    channel = ConsoleChannel(ainput=scripted_input(answer))
    result = await channel.confirm(ConfirmRequest([], ["consequential"], "", ["x"]))
    assert result.approved is approved
    assert result.reason == ("" if approved else "denied")


async def test_console_confirm_on_eof_denies_and_cancellation_propagates():
    channel = ConsoleChannel(ainput=scripted_input(EOFError()))
    result = await channel.confirm(ConfirmRequest([], [], "", []))
    assert (result.approved, result.reason) == (False, "no_ui")
    channel = ConsoleChannel(ainput=scripted_input(asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await channel.confirm(ConfirmRequest([], [], "", []))


async def test_console_ask_and_handoff(capsys):
    prompts: List[str] = []
    channel = ConsoleChannel(ainput=scripted_input("  mi respuesta ", "", EOFError(), "", prompts=prompts))
    assert await channel.ask(f"¿CURP {CURP}?") == "mi respuesta"
    assert await channel.ask("Resuelva el CAPTCHA", "handoff") == "listo"
    assert await channel.ask("?") is None
    assert await channel.ask("x", "handoff") == "listo"
    out = capsys.readouterr().out
    assert CURP not in out and "GO**************09" in out
    assert prompts[0] == "❯ "
    assert prompts[1] == "❯ Press Enter when you are done… "


async def test_console_allow_site():
    prompts: List[str] = []
    channel = ConsoleChannel(ainput=scripted_input("y", "n", EOFError(), prompts=prompts))
    assert await channel.allow_site("example.com", "https://example.com/") is True
    assert await channel.allow_site("example.com", "https://example.com/") is False
    assert await channel.allow_site("example.com", "https://example.com/") is False
    assert prompts[0] == "❯ Allow BABAS to use example.com? (y/N): "


async def test_console_log_goes_through_redact(capsys):
    channel = ConsoleChannel()
    await channel.log("assistant", f"Voy a escribir {CURP}")
    await channel.log("step", f"Escribió {CURP}", True)
    await channel.log("step", "Falló", False)
    await channel.log("result", "Listo")
    await channel.log("error", "Algo salió mal")
    await channel.log("info", "dato")
    await channel.run_state("done", f"Su CURP {CURP}")
    out = capsys.readouterr().out
    assert CURP not in out
    assert "BABAS: Voy a escribir GO**************09" in out
    assert "✓ Escribió GO**************09" in out and "✗ Falló" in out
    assert "Respuesta" in out and "│ Listo" in out
    assert "Error" in out and "│ Algo salió mal" in out and "• dato" in out


class FakeVoiceGui:
    def __init__(self, answer):
        self.answer = answer
        self.shown: List[tuple] = []

    def show_plan_and_confirm(self, text, timeout=60):
        self.shown.append((text, timeout))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


@pytest.mark.parametrize("answer,approved", [(True, True), (False, False), (None, False), ("yes", False), (1, False), (RuntimeError("x"), False)])
async def test_console_confirm_with_a_voice_gui(answer, approved):
    gui = FakeVoiceGui(answer)
    channel = ConsoleChannel(voice_gui=gui)
    result = await channel.confirm(ConfirmRequest([], ["pii_value"], "porque sí", [f"Escribir «{CURP}»"]))
    assert result.approved is approved
    text, timeout = gui.shown[0]
    assert timeout == 120 and CURP in text and "Escribir datos personales" in text


async def test_console_allow_site_with_a_voice_gui():
    assert await ConsoleChannel(voice_gui=FakeVoiceGui(True)).allow_site("example.com", "https://example.com/") is True
    assert await ConsoleChannel(voice_gui=FakeVoiceGui(None)).allow_site("example.com", "https://example.com/") is False


async def test_console_default_reader_uses_a_thread_and_maps_eof(monkeypatch):
    answers = iter(["y", EOFError()])

    def fake_input(prompt=""):
        item = next(answers)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(builtins, "input", fake_input)
    assert await channels_module._read_line("? ") == "y"
    channel = ConsoleChannel()
    result = await channel.confirm(ConfirmRequest([], [], "", []))
    assert (result.approved, result.reason) == (False, "no_ui")


async def test_console_default_reader_cancellation_propagates(monkeypatch):
    import threading

    release = threading.Event()

    def blocking_input(prompt=""):
        release.wait(5)
        return "late"

    monkeypatch.setattr(builtins, "input", blocking_input)
    task = asyncio.create_task(ConsoleChannel().ask("?"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await asyncio.sleep(0.05)


async def test_recording_channel_and_fake_llm_helpers_behave():
    channel = RecordingChannel(approve=[True, False], answer=["uno", None], allow=[False, True], delay=0.0)
    assert (await channel.confirm(ConfirmRequest([], [], ""))).approved is True
    assert (await channel.confirm(ConfirmRequest([], [], ""))).approved is False
    assert (await channel.confirm(ConfirmRequest([], [], ""))).approved is False
    assert await channel.ask("a") == "uno" and await channel.ask("b") is None
    assert await channel.allow_site("h", "u") is False and await channel.allow_site("h", "u") is True
    await channel.log("info", "x", None)
    await channel.run_state("running")
    assert [name for name, _ in channel.calls] == ["confirm"] * 3 + ["ask"] * 2 + ["allow_site"] * 2 + ["log", "run_state"]



async def test_fake_llm_helper_behaves():
    llm = FakeLLM([{"steps": [{"action": "wait"}]}, RuntimeError("down"), lambda s, u: {"echo": u}])
    assert await llm.complete_json("sys", "u1") == {"steps": [{"action": "wait"}]}
    with pytest.raises(RuntimeError):
        await llm.complete_json("sys", "u2")
    assert await llm.complete_json("sys", "u3") == {"echo": "u3"}
    assert await llm.complete_json("sys", "u4") == {"steps": [{"action": "done", "value": "Listo."}]}
    llm.push({"x": 1})
    assert await llm.complete_json("sys", "u5") == {"x": 1}
    assert [call[1] for call in llm.calls] == ["u1", "u2", "u3", "u4", "u5"]


async def test_fake_llm_parses_string_responses():
    pytest.importorskip("babas.llm")
    llm = FakeLLM(['```json\n{"steps": []}\n```'])
    assert await llm.complete_json("sys", "u") == {"steps": []}


async def test_fake_llm_delay_happens_before_the_call_is_recorded():
    llm = FakeLLM(delay=30)
    task = asyncio.create_task(llm.complete_json("sys", "u"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert llm.calls == []
