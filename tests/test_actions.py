import inspect
import re
from typing import Any, Dict, Optional

import pytest

from babas import actions as actions_module
from babas.actions import AGENT_ACTIONS, ActionExecutor, StepOutcome
from babas.drivers.base import DriverError, ErrorCode, Target
from babas.gate import ActionDenied, SafeDriver
from babas.safety import SafetyPolicy
from babas.schemas import ACTION_MODELS
from helpers import PNG_STUB, FakeDriver, RecordingChannel, make_element, make_state

CURP = "GOMC800101HDFRRL09"
PLAIN = {"confirmation": None, "llm_consequential": False, "explanation": ""}

COMPOSE = 'div[role="button"]:has-text("Compose")'
TO = 'input[name="to"]'
SUBJECT = 'input[name="subjectbox"]'
BODY = 'div[role="textbox"][aria-label*="Message"]'
SEND = 'div[role="button"]:has-text("Send")'
YT_SEARCH = 'input[name="search_query"]'
YT_VIDEO = "ytd-video-renderer a#thumbnail"
WA_SEARCH = 'div[contenteditable="true"][data-tab="3"]'
WA_RESULT = 'div[role="listitem"]'
WA_BOX = 'div[contenteditable="true"][data-tab="10"]'


def step(**data: Any):
    return ACTION_MODELS[data["action"]].model_validate(data)


def gmail_selectors() -> Dict[str, Any]:
    return {
        COMPOSE: make_element(0, "button", "Compose", tag="div"),
        TO: make_element(1, "textbox", "To", tag="input", flags=["pii"]),
        SUBJECT: make_element(2, "textbox", "Subject", tag="input"),
        BODY: make_element(3, "textbox", "Message Body", tag="div"),
        SEND: make_element(4, "button", "Send", tag="div"),
    }


def whatsapp_selectors() -> Dict[str, Any]:
    return {
        WA_SEARCH: make_element(0, "textbox", "Buscar", tag="div"),
        WA_RESULT: make_element(1, "button", "Mamá", tag="div"),
        WA_BOX: make_element(2, "textbox", "Escribir un mensaje", tag="div"),
    }


def build(driver: Optional[FakeDriver] = None, **kwargs: Any):
    driver = driver or FakeDriver()
    return ActionExecutor(driver, find_timeout_ms=0, video_timeout_ms=0, **kwargs), driver


def build_gated(driver: Optional[FakeDriver] = None, channel: Optional[RecordingChannel] = None, **kwargs: Any):
    driver = driver or FakeDriver()
    channel = channel if channel is not None else RecordingChannel(mint_ids=True)
    gate = SafeDriver(driver, SafetyPolicy(), channel)
    return ActionExecutor(gate, find_timeout_ms=0, video_timeout_ms=0, **kwargs), driver, channel


def sequence(driver: FakeDriver, *methods: str):
    return [(name, args) for name, args, _ in driver.calls if name in methods]


def test_every_action_has_a_handler_except_the_agent_ones():
    executor, _ = build()
    assert AGENT_ACTIONS == {"ask_user", "done"}
    assert set(ACTION_MODELS) - AGENT_ACTIONS == set(executor._handlers)
    assert len(executor._handlers) == 23


def test_module_does_not_depend_on_playwright():
    assert "playwright" not in inspect.getsource(actions_module).lower()


@pytest.mark.parametrize(
    "data",
    [
        {"action": "ask_user", "value": "¿Cuál es su CURP?"},
        {"action": "done", "value": "Listo."},
    ],
)
async def test_agent_actions_are_refused(data):
    executor, driver = build()
    with pytest.raises(ValueError):
        await executor.execute(step(**data), None)
    assert driver.calls == []


async def test_unknown_action_is_refused():
    executor, driver = build()

    class Fake:
        action = "fly"

    with pytest.raises(ValueError, match="Unknown action"):
        await executor.execute(Fake(), None)
    assert driver.calls == []


async def test_goto_navigates_then_keeps_the_legacy_wait():
    executor, driver = build()
    outcome = await executor.execute(step(action="goto", value="gob.mx"), None)
    assert outcome == StepOutcome(ok=True, detail="", data=None)
    assert sequence(driver, "goto", "wait") == [("goto", ("https://gob.mx",)), ("wait", (2000,))]


async def test_wait_uses_the_driver_clock():
    executor, driver = build()
    await executor.execute(step(action="wait", ms=1234), None)
    assert driver.calls_of("wait") == [((1234,), {})]


async def test_scroll_passes_direction_and_distance():
    executor, driver = build()
    await executor.execute(step(action="scroll", value="up", ms=300), None)
    await executor.execute(step(action="scroll"), None)
    assert driver.calls_of("scroll") == [(("up", 300), {}), (("down", 600), {})]


async def test_screenshot_writes_a_file_and_reports_its_name(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    executor, _ = build()
    outcome = await executor.execute(step(action="screenshot"), None)
    assert re.fullmatch(r"screenshot_\d{6}\.png", outcome.detail)
    assert (tmp_path / outcome.detail).read_bytes() == PNG_STUB


async def test_screenshot_directory_can_be_chosen(tmp_path):
    executor, _ = build(screenshot_dir=str(tmp_path))
    outcome = await executor.execute(step(action="screenshot"), None)
    assert outcome.detail.startswith(str(tmp_path))
    assert (tmp_path / outcome.detail.rsplit("/", 1)[-1]).read_bytes() == PNG_STUB


async def test_mutating_calls_carry_the_gate_keywords():
    state = make_state(make_element(0, "textbox", "Nombre"), make_element(1, "button", "Buscar"), snapshot_id="snap-7")
    executor, driver = build(FakeDriver(state))

    await executor.execute(step(action="click", index=1), state)
    await executor.execute(step(action="type_text", index=0, value="Ana"), state)
    await executor.execute(step(action="press_key", value="enter", index=0), state)
    await executor.execute(step(action="press_key", value="Tab"), state)
    await executor.execute(step(action="select_option", selector="#estado", value="Jalisco"), state)

    at_1 = Target(index=1, snapshot_id="snap-7")
    at_0 = Target(index=0, snapshot_id="snap-7")
    assert driver.calls_of("click") == [((at_1,), PLAIN)]
    assert driver.calls_of("type_text") == [((at_0, "Ana"), PLAIN)]
    assert driver.calls_of("press_key") == [(("Enter", at_0), PLAIN), (("Tab", None), PLAIN)]
    assert driver.calls_of("select_option") == [((Target(selector="#estado"), "Jalisco"), PLAIN)]


async def test_confirmation_id_consequential_flag_and_explanation_are_forwarded():
    executor, driver = build()
    await executor.execute(
        step(action="click", selector="#pagar", consequential=True, explanation="Voy a pagar."),
        None,
        confirmation="conf-9",
    )
    assert driver.calls_of("click") == [
        ((Target(selector="#pagar"),), {"confirmation": "conf-9", "llm_consequential": True, "explanation": "Voy a pagar."})
    ]


async def test_targets_by_selector_and_text_need_no_state():
    executor, driver = build()
    await executor.execute(step(action="click", selector="#a"), None)
    await executor.execute(step(action="click", text="Aceptar"), None)
    assert [args[0] for args, _ in driver.calls_of("click")] == [Target(selector="#a"), Target(text="Aceptar")]


@pytest.mark.parametrize(
    "data",
    [
        {"action": "click", "index": 3},
        {"action": "type_text", "index": 3, "value": "x"},
        {"action": "press_key", "value": "Enter", "index": 3},
        {"action": "select_option", "index": 3, "value": "x"},
        {"action": "read_text", "index": 3},
    ],
)
async def test_index_target_without_state_fails_clearly(data):
    executor, driver = build()
    with pytest.raises(ValueError, match="no current page state"):
        await executor.execute(step(**data), None)
    assert driver.calls == []


async def test_select_option_reports_the_chosen_option():
    state = make_state(make_element(0, "combobox", "Estado"))
    executor, driver = build(FakeDriver(state))
    outcome = await executor.execute(step(action="select_option", index=0, value="Jalisco"), state)
    assert outcome == StepOutcome(ok=True, detail="Jalisco")


async def test_read_text_is_truncated_to_two_thousand_characters():
    executor, driver = build(FakeDriver(text="a" * 5000))
    outcome = await executor.execute(step(action="read_text"), None)
    assert outcome.ok and outcome.detail == "a" * 2000
    assert driver.calls_of("read_text") == [((None,), {})]


async def test_read_text_of_one_element():
    state = make_state(make_element(0, "link", "Aviso"), snapshot_id="snap-2")
    executor, driver = build(FakeDriver(state, text="Aviso importante"))
    outcome = await executor.execute(step(action="read_text", index=0), state)
    assert outcome.detail == "Aviso importante"
    assert driver.calls_of("read_text") == [((Target(index=0, snapshot_id="snap-2"),), {})]


async def test_analyze_page_summarizes_and_masks_personal_data():
    state = make_state(
        make_element(0, "link", "Inicio"),
        make_element(1, "button", "Salir"),
        url="https://www.gob.mx/tramites",
        title=f"Trámite de {CURP}",
        has_captcha=True,
    )
    executor, driver = build(FakeDriver(state))
    outcome = await executor.execute(step(action="analyze_page"), None)
    assert outcome.ok
    assert "https://www.gob.mx/tramites" in outcome.detail
    assert "ELEMENTS: 2" in outcome.detail and "CAPTCHA: yes" in outcome.detail
    assert CURP not in outcome.detail
    assert outcome.data.url == "https://www.gob.mx/tramites"
    assert len(driver.calls_of("get_page_state")) == 1


def search_state(*elements, **fields):
    return make_state(*elements, snapshot_id="s1", **fields)


async def test_smart_search_prefers_a_visible_search_form_field_by_index():
    state = search_state(
        make_element(0, "combobox", "Estado", flags=["search_form"]),
        make_element(1, "searchbox", "Buscar abajo", flags=["search_form"], in_viewport=False),
        make_element(2, "textbox", "Buscar", flags=["search_form"]),
        make_element(3, "button", "Buscar", flags=["search_form"]),
        make_element(4, "textbox", "Nombre"),
    )
    executor, driver = build(FakeDriver(state))
    await executor.execute(step(action="smart_search", value="clima"), state)

    target = Target(index=2, snapshot_id="s1")
    assert driver.calls_of("type_text") == [((target, "clima"), PLAIN)]
    assert driver.calls_of("press_key") == [(("Enter", target), PLAIN)]
    assert driver.calls_of("is_visible") == []


async def test_smart_search_takes_the_first_one_when_none_is_in_the_viewport():
    state = search_state(
        make_element(1, "searchbox", "Buscar abajo", flags=["search_form"], in_viewport=False),
        make_element(2, "textbox", "Buscar", flags=["search_form"], in_viewport=False),
    )
    executor, driver = build(FakeDriver(state))
    await executor.execute(step(action="smart_search", value="clima"), state)
    assert driver.calls_of("type_text")[0][0][0] == Target(index=1, snapshot_id="s1")


async def test_smart_search_falls_back_to_the_legacy_selector_list_in_order():
    state = search_state(make_element(0, "textbox", "Nombre"), make_element(1, "button", "Buscar", flags=["search_form"]))
    found = make_element(5, "textbox", "q", tag="input")
    driver = FakeDriver(
        state,
        selectors={"[role='searchbox']": found, "input[name*='query']": found, "input[class*='search']": found},
    )
    executor, _ = build(driver)
    await executor.execute(step(action="smart_search", value="recetas"), state)

    target = Target(selector="input[name*='query']")
    assert driver.calls_of("type_text") == [((target, "recetas"), PLAIN)]
    assert driver.calls_of("press_key") == [(("Enter", target), PLAIN)]
    probed = [args[0].selector for args, _ in driver.calls_of("is_visible")]
    assert probed == ["input[type='search']", "input[name*='search']", "input[name*='query']"]


async def test_smart_search_without_state_uses_the_fallback_too():
    driver = FakeDriver(selectors={"input[type='search']": make_element(0, "searchbox", "Buscar", tag="input")})
    executor, _ = build(driver)
    await executor.execute(step(action="smart_search", value="x"), None)
    assert driver.calls_of("type_text")[0][0][0] == Target(selector="input[type='search']")


async def test_smart_search_reports_a_missing_search_box():
    executor, driver = build()
    with pytest.raises(DriverError) as raised:
        await executor.execute(step(action="smart_search", value="x"), None)
    assert raised.value.code == ErrorCode.NOT_FOUND
    assert "search box" in raised.value.message
    assert driver.calls_of("type_text") == []


async def test_smart_search_through_the_gate_needs_no_card_for_a_plain_query():
    state = search_state(make_element(0, "searchbox", "Buscar", flags=["search_form"]))
    executor, driver, channel = build_gated(FakeDriver(state))
    await executor.execute(step(action="smart_search", value="clima en Guadalajara"), state)
    assert channel.confirms == []
    assert [name for name, _ in sequence(driver, "type_text", "press_key")] == ["type_text", "press_key"]


async def test_smart_search_never_bypasses_the_gate_for_personal_data():
    state = search_state(make_element(0, "searchbox", "Buscar", flags=["search_form"]))
    executor, driver, channel = build_gated(FakeDriver(state))
    await executor.execute(step(action="smart_search", value=CURP, explanation="Busco su CURP."), state)
    assert len(channel.confirms) == 1
    assert "pii_value" in channel.confirms[0].reasons
    assert channel.confirms[0].explanation == "Busco su CURP."
    assert driver.calls_of("type_text")[0][1] == {"confirmation": "conf-1"}


async def test_smart_search_stops_when_the_user_says_no():
    state = search_state(make_element(0, "searchbox", "Buscar", flags=["search_form"]))
    executor, driver, channel = build_gated(FakeDriver(state), RecordingChannel(approve=False))
    with pytest.raises(ActionDenied):
        await executor.execute(step(action="smart_search", value=CURP), state)
    assert driver.calls_of("type_text") == [] and driver.calls_of("press_key") == []


async def test_smart_click_clicks_by_visible_text():
    executor, driver = build()
    await executor.execute(step(action="smart_click", value="Consultar CURP", consequential=True), None)
    assert driver.calls_of("click") == [
        ((Target(text="Consultar CURP"),), {"confirmation": None, "llm_consequential": True, "explanation": ""})
    ]


async def test_new_tab_uses_tab_name_then_value_then_lets_the_driver_choose():
    executor, driver = build()
    assert (await executor.execute(step(action="new_tab", tab_name="correo"), None)).detail == "correo"
    assert (await executor.execute(step(action="new_tab", value="notas"), None)).detail == "notas"
    assert (await executor.execute(step(action="new_tab"), None)).detail == "tab_1"
    assert driver.calls_of("new_tab") == [(("correo", None), {}), (("notas", None), {}), ((None, None), {})]
    assert driver.list_tabs() == ["main", "correo", "notas", "tab_1"]


async def test_switch_tab_by_name_or_value():
    executor, driver = build()
    await executor.execute(step(action="new_tab", tab_name="correo"), None)
    await executor.execute(step(action="switch_tab", tab_name="main"), None)
    assert driver.current_tab == "main"
    outcome = await executor.execute(step(action="switch_tab", value="correo"), None)
    assert driver.current_tab == "correo" and outcome.ok


async def test_switch_to_an_unknown_tab_is_a_clear_error():
    executor, driver = build()
    with pytest.raises(DriverError) as raised:
        await executor.execute(step(action="switch_tab", tab_name="nada"), None)
    assert raised.value.code == ErrorCode.TAB_NOT_FOUND
    assert "nada" in raised.value.message and "main" in raised.value.message
    assert driver.calls_of("switch_tab") == []


async def test_switch_tab_without_a_name_is_a_value_error():
    executor, driver = build()
    with pytest.raises(ValueError, match="tab name"):
        await executor.execute(step(action="switch_tab"), None)
    assert driver.calls == []


async def test_close_tab_by_name_and_current():
    executor, driver = build()
    await executor.execute(step(action="new_tab", tab_name="correo"), None)
    await executor.execute(step(action="new_tab", tab_name="notas"), None)

    assert (await executor.execute(step(action="close_tab", tab_name="correo"), None)).ok
    assert driver.list_tabs() == ["main", "notas"]

    assert (await executor.execute(step(action="close_tab"), None)).ok
    assert driver.list_tabs() == ["main"]
    assert driver.current_tab == "main"
    assert [args for args, _ in driver.calls_of("close_tab")] == [("correo",), (None,)]


async def test_closing_the_main_tab_is_reported_as_a_failure():
    executor, driver = build()
    for data in ({"action": "close_tab"}, {"action": "close_tab", "tab_name": "main"}):
        outcome = await executor.execute(step(**data), None)
        assert outcome.ok is False
        assert "main tab" in outcome.detail
    assert driver.list_tabs() == ["main"]


async def test_closing_an_unknown_tab_is_a_clear_error():
    executor, driver = build()
    with pytest.raises(DriverError) as raised:
        await executor.execute(step(action="close_tab", value="fantasma"), None)
    assert raised.value.code == ErrorCode.TAB_NOT_FOUND
    assert driver.calls_of("close_tab") == []


async def test_search_youtube_on_youtube_types_and_presses_enter():
    driver = FakeDriver(url="https://www.youtube.com/", selectors={YT_SEARCH: make_element(0, "searchbox", "Buscar")})
    executor, _ = build(driver)
    await executor.execute(step(action="search_youtube", value="canciones de José José"), None)

    target = Target(selector=YT_SEARCH)
    assert driver.calls_of("goto") == []
    assert driver.calls_of("type_text") == [((target, "canciones de José José"), PLAIN)]
    assert driver.calls_of("press_key") == [(("Enter", target), PLAIN)]
    assert sequence(driver, "type_text", "press_key", "wait") == [
        ("type_text", (target, "canciones de José José")),
        ("press_key", ("Enter", target)),
        ("wait", (2000,)),
    ]
    assert driver.calls_of("click") == []


async def test_search_youtube_opens_youtube_first_when_elsewhere():
    driver = FakeDriver(selectors={YT_SEARCH: make_element(0, "searchbox", "Buscar")})
    executor, _ = build(driver)
    await executor.execute(step(action="search_youtube", value="pozole"), None)
    assert driver.calls_of("goto") == [(("https://www.youtube.com",), {})]
    assert sequence(driver, "goto", "wait", "type_text")[:3] == [
        ("goto", ("https://www.youtube.com",)),
        ("wait", (2000,)),
        ("type_text", (Target(selector=YT_SEARCH), "pozole")),
    ]


async def test_search_youtube_dismisses_one_popup_and_survives_a_failed_click():
    accept_aria = 'button[aria-label*="Accept"]'
    accept_text = 'button:has-text("Accept")'
    aceptar = 'button:has-text("Aceptar")'
    button = make_element(9, "button", "Accept all", tag="button")
    driver = FakeDriver(
        url="https://www.youtube.com/",
        selectors={YT_SEARCH: make_element(0, "searchbox", "Buscar"), accept_aria: button, accept_text: button, aceptar: button},
        errors={"click": DriverError(ErrorCode.OBSCURED, "covered")},
    )
    executor, _ = build(driver)
    await executor.execute(step(action="search_youtube", value="x"), None)
    assert [args[0].selector for args, _ in driver.calls_of("click")] == [accept_aria, accept_text]
    assert len(driver.calls_of("type_text")) == 1


async def test_search_youtube_without_a_search_box_is_not_found():
    executor, driver = build(FakeDriver(url="https://www.youtube.com/"))
    with pytest.raises(DriverError) as raised:
        await executor.execute(step(action="search_youtube", value="x"), None)
    assert raised.value.code == ErrorCode.NOT_FOUND
    assert "YouTube search box" in raised.value.message


async def test_click_first_video():
    driver = FakeDriver(selectors={YT_VIDEO: make_element(0, "link", "Video", tag="a")})
    executor, _ = build(driver)
    await executor.execute(step(action="click_first_video"), None)
    assert driver.calls_of("click") == [((Target(selector=YT_VIDEO),), PLAIN)]


async def test_click_first_video_not_found():
    executor, driver = build()
    with pytest.raises(DriverError) as raised:
        await executor.execute(step(action="click_first_video"), None)
    assert raised.value.code == ErrorCode.NOT_FOUND
    assert driver.calls_of("click") == []


async def test_find_first_visible_polls_until_the_element_appears(monkeypatch):
    monkeypatch.setattr(actions_module, "FIND_POLL_MS", 1)
    driver = FakeDriver()
    original = driver.is_visible
    probes = []

    async def appearing(target):
        probes.append(target.selector)
        if len(probes) > 4:
            driver.selectors["#late"] = make_element(0, "button", "Tarde")
        return await original(target)

    driver.is_visible = appearing
    executor = ActionExecutor(driver)
    found = await executor.find_first_visible(["#early", "#late"], timeout_ms=5000)
    assert found == Target(selector="#late")
    assert probes[:4] == ["#early", "#late", "#early", "#late"]
    assert len(probes) == 6


async def test_find_first_visible_keeps_the_given_order_and_reports_what_is_missing():
    driver = FakeDriver(selectors={"#b": make_element(1, "button", "B"), "#a": make_element(0, "button", "A")})
    executor, _ = build(driver)
    assert await executor.find_first_visible(["#a", "#b"]) == Target(selector="#a")
    assert await executor.find_first_visible(["#b", "#a"]) == Target(selector="#b")
    with pytest.raises(DriverError) as raised:
        await executor.find_first_visible(["#x", "#y"], what="the thing")
    assert raised.value.code == ErrorCode.NOT_FOUND
    assert raised.value.message == "Element not found: the thing"
    assert raised.value.data["selectors"] == ["#x", "#y"]
    with pytest.raises(DriverError, match="NOT_FOUND: Element not found$"):
        await executor.find_first_visible(["#x"])


async def test_find_first_visible_lets_a_stop_through():
    driver = FakeDriver(errors={"is_visible": DriverError(ErrorCode.HALTED, "stopped")})
    executor, _ = build(driver)
    with pytest.raises(DriverError) as raised:
        await executor.find_first_visible(["#a"])
    assert raised.value.code == ErrorCode.HALTED


async def test_find_first_visible_skips_a_failing_selector():
    driver = FakeDriver(
        selectors={"#b": make_element(1, "button", "B")},
        errors={"is_visible": DriverError(ErrorCode.BAD_REQUEST, "bad selector")},
    )
    executor, _ = build(driver)
    assert await executor.find_first_visible(["#a", "#b"]) == Target(selector="#b")


async def test_open_gmail_and_whatsapp_keep_the_legacy_waits():
    executor, driver = build()
    await executor.execute(step(action="open_gmail"), None)
    await executor.execute(step(action="open_whatsapp"), None)
    assert sequence(driver, "goto", "wait") == [
        ("goto", ("https://mail.google.com",)),
        ("wait", (3000,)),
        ("goto", ("https://web.whatsapp.com",)),
        ("wait", (5000,)),
    ]


async def test_compose_email_fills_the_three_fields():
    executor, driver = build(FakeDriver(selectors=gmail_selectors()))
    await executor.execute(
        step(action="compose_email", to="ana@example.com", subject="Reunión", body="Hola Ana", explanation="Preparo el correo."),
        None,
    )
    kw = {"confirmation": None, "llm_consequential": False, "explanation": "Preparo el correo."}
    assert driver.calls_of("click") == [((Target(selector=COMPOSE),), kw)]
    assert driver.calls_of("type_text") == [
        ((Target(selector=TO), "ana@example.com"), kw),
        ((Target(selector=SUBJECT), "Reunión"), kw),
        ((Target(selector=BODY), "Hola Ana"), kw),
    ]
    assert driver.calls_of("wait") == [((2000,), {})]
    assert sequence(driver, "click", "wait", "type_text")[:2] == [("click", (Target(selector=COMPOSE),)), ("wait", (2000,))]


async def test_compose_email_skips_the_empty_optional_fields():
    executor, driver = build(FakeDriver(selectors=gmail_selectors()))
    await executor.execute(step(action="compose_email", to="ana@example.com"), None)
    assert [args[0].selector for args, _ in driver.calls_of("type_text")] == [TO]


async def test_compose_email_uses_the_first_matching_selector_of_each_list():
    selectors = gmail_selectors()
    del selectors[TO], selectors[BODY]
    selectors['input[aria-label*="Para"]'] = make_element(1, "textbox", "Para", tag="input", flags=["pii"])
    selectors['div[role="textbox"]'] = make_element(3, "textbox", "Cuerpo", tag="div")
    executor, driver = build(FakeDriver(selectors=selectors))
    await executor.execute(step(action="compose_email", to="ana@example.com", subject="s", body="b"), None)
    assert [args[0].selector for args, _ in driver.calls_of("type_text")] == [
        'input[aria-label*="Para"]',
        SUBJECT,
        'div[role="textbox"]',
    ]


async def test_compose_email_asks_once_for_the_three_fields():
    executor, driver, channel = build_gated(FakeDriver(selectors=gmail_selectors()))
    await executor.execute(
        step(action="compose_email", to="ana@example.com", subject="Reunión", body="Hola Ana", explanation="Preparo el correo."),
        None,
    )
    assert len(channel.confirms) == 1
    request = channel.confirms[0]
    assert [a["op"] for a in request.actions] == ["type"]
    assert request.actions[0]["text"] == "ana@example.com"
    assert request.explanation == "Preparo el correo."
    typed = driver.calls_of("type_text")
    assert [args[1] for args, _ in typed] == ["ana@example.com", "Reunión", "Hola Ana"]
    assert all(kwargs == {"confirmation": "conf-1"} for _, kwargs in typed)


async def test_compose_email_lists_every_personal_value_on_the_same_card():
    executor, driver, channel = build_gated(FakeDriver(selectors=gmail_selectors()))
    await executor.execute(
        step(action="compose_email", to="ana@example.com", subject="Mi teléfono", body="Llámeme al 55 1234 5678"), None
    )
    assert len(channel.confirms) == 1
    assert [a["text"] for a in channel.confirms[0].actions] == ["ana@example.com", "Llámeme al 55 1234 5678"]
    assert len(driver.calls_of("type_text")) == 3


async def test_compose_email_address_is_personal_data_even_without_the_field_flag():
    selectors = gmail_selectors()
    selectors[TO] = make_element(1, "textbox", "To", tag="input")
    executor, driver, channel = build_gated(FakeDriver(selectors=selectors))
    await executor.execute(step(action="compose_email", to="ana@example.com", subject="s"), None)
    assert len(channel.confirms) == 1
    assert channel.confirms[0].reasons == ["pii_value"]


async def test_compose_email_with_a_channel_without_ids_still_asks_once():
    executor, driver, channel = build_gated(FakeDriver(selectors=gmail_selectors()), RecordingChannel())
    await executor.execute(step(action="compose_email", to="ana@example.com", subject="s", body="b"), None)
    assert len(channel.confirms) == 1
    assert [kwargs["confirmation"] for _, kwargs in driver.calls_of("type_text")] == [None, None, None]


async def test_compose_email_types_nothing_when_the_user_says_no():
    executor, driver, channel = build_gated(FakeDriver(selectors=gmail_selectors()), RecordingChannel(approve=False))
    with pytest.raises(ActionDenied):
        await executor.execute(step(action="compose_email", to="ana@example.com", subject="s", body="b"), None)
    assert driver.calls_of("type_text") == []
    assert len(channel.confirms) == 1


async def test_compose_email_without_gmail_open_is_not_found():
    executor, driver = build()
    with pytest.raises(DriverError) as raised:
        await executor.execute(step(action="compose_email", to="ana@example.com"), None)
    assert raised.value.code == ErrorCode.NOT_FOUND
    assert "Compose" in raised.value.message


async def test_compose_email_without_a_recipient_field_is_not_found():
    selectors = gmail_selectors()
    del selectors[TO]
    executor, driver = build(FakeDriver(selectors=selectors))
    with pytest.raises(DriverError) as raised:
        await executor.execute(step(action="compose_email", to="ana@example.com"), None)
    assert raised.value.code == ErrorCode.NOT_FOUND and "recipient" in raised.value.message
    assert driver.calls_of("type_text") == []


async def test_send_email_forces_the_confirmation_on_the_send_button():
    executor, driver = build(FakeDriver(selectors=gmail_selectors()))
    await executor.execute(step(action="send_email", consequential=True, explanation="Envío el correo."), None, confirmation="c1")
    assert driver.calls_of("click") == [
        (
            (Target(selector=SEND),),
            {"confirmation": "c1", "llm_consequential": True, "explanation": "Envío el correo.", "force_confirm": True},
        )
    ]
    assert driver.calls_of("wait") == [((2000,), {})]


async def test_send_email_always_asks_even_for_a_plain_button():
    executor, driver, channel = build_gated(FakeDriver(selectors=gmail_selectors()))
    await executor.execute(step(action="send_email"), None)
    assert len(channel.confirms) == 1
    assert "macro" in channel.confirms[0].reasons
    assert driver.calls_of("click") == [((Target(selector=SEND),), {"confirmation": "conf-1"})]


async def test_send_email_does_not_send_when_the_user_says_no():
    executor, driver, channel = build_gated(FakeDriver(selectors=gmail_selectors()), RecordingChannel(approve=False))
    with pytest.raises(ActionDenied):
        await executor.execute(step(action="send_email"), None)
    assert driver.calls_of("click") == []
    assert driver.calls_of("wait") == []


async def test_send_email_without_a_message_open_is_not_found():
    executor, driver = build()
    with pytest.raises(DriverError) as raised:
        await executor.execute(step(action="send_email"), None)
    assert raised.value.code == ErrorCode.NOT_FOUND and driver.calls_of("click") == []


async def test_search_contact_types_waits_and_opens_the_first_chat():
    executor, driver = build(FakeDriver(selectors=whatsapp_selectors()))
    await executor.execute(step(action="search_contact", value="Mamá"), None)
    assert sequence(driver, "type_text", "wait", "click") == [
        ("type_text", (Target(selector=WA_SEARCH), "Mamá")),
        ("wait", (2000,)),
        ("click", (Target(selector=WA_RESULT),)),
    ]


async def test_search_contact_asks_when_the_search_text_is_a_phone_number():
    executor, driver, channel = build_gated(FakeDriver(selectors=whatsapp_selectors()))
    await executor.execute(step(action="search_contact", value="55 1234 5678"), None)
    assert len(channel.confirms) == 1 and "pii_value" in channel.confirms[0].reasons


async def test_search_contact_without_results_is_not_found():
    selectors = whatsapp_selectors()
    del selectors[WA_RESULT]
    executor, driver = build(FakeDriver(selectors=selectors))
    with pytest.raises(DriverError) as raised:
        await executor.execute(step(action="search_contact", value="Mamá"), None)
    assert raised.value.code == ErrorCode.NOT_FOUND
    assert driver.calls_of("click") == []


async def test_send_whatsapp_message_forces_the_gate_flags_on_a_plain_driver():
    executor, driver = build(FakeDriver(selectors=whatsapp_selectors()))
    await executor.execute(step(action="send_whatsapp_message", body="Ya llegué", consequential=True), None)
    box = Target(selector=WA_BOX)
    kw = {"confirmation": None, "llm_consequential": True, "explanation": "", "force_confirm": True}
    assert driver.calls_of("type_text") == [((box, "Ya llegué"), kw)]
    assert driver.calls_of("press_key") == [(("Enter", box), kw)]


async def test_send_whatsapp_message_asks_once_with_the_text_then_types_and_sends():
    executor, driver, channel = build_gated(FakeDriver(selectors=whatsapp_selectors()))
    await executor.execute(
        step(action="send_whatsapp_message", body="Ya llegué, gracias.", explanation="Envío su mensaje."), None
    )
    assert len(channel.confirms) == 1
    request = channel.confirms[0]
    assert [a["op"] for a in request.actions] == ["type", "press"]
    assert request.actions[0]["text"] == "Ya llegué, gracias."
    assert "macro" in request.reasons
    assert "Ya llegué, gracias." in request.lines[0]
    assert request.explanation == "Envío su mensaje."
    assert sequence(driver, "type_text", "press_key") == [
        ("type_text", (Target(selector=WA_BOX), "Ya llegué, gracias.")),
        ("press_key", ("Enter", Target(selector=WA_BOX))),
    ]
    assert driver.calls_of("type_text")[0][1] == {"confirmation": "conf-1"}
    assert driver.calls_of("press_key")[0][1] == {"confirmation": "conf-1"}


async def test_send_whatsapp_message_with_a_channel_without_ids_does_not_ask_twice():
    executor, driver, channel = build_gated(FakeDriver(selectors=whatsapp_selectors()), RecordingChannel())
    await executor.execute(step(action="send_whatsapp_message", body="Hola"), None)
    assert len(channel.confirms) == 1
    assert len(driver.calls_of("type_text")) == 1 and len(driver.calls_of("press_key")) == 1


async def test_send_whatsapp_message_sends_nothing_when_the_user_says_no():
    executor, driver, channel = build_gated(FakeDriver(selectors=whatsapp_selectors()), RecordingChannel(approve=False))
    with pytest.raises(ActionDenied):
        await executor.execute(step(action="send_whatsapp_message", body="Hola"), None)
    assert driver.calls_of("type_text") == [] and driver.calls_of("press_key") == []


async def test_send_whatsapp_message_without_an_open_chat_is_not_found():
    executor, driver, channel = build_gated()
    with pytest.raises(DriverError) as raised:
        await executor.execute(step(action="send_whatsapp_message", body="Hola"), None)
    assert raised.value.code == ErrorCode.NOT_FOUND
    assert channel.confirms == []


async def test_driver_errors_reach_the_caller_unchanged():
    error = DriverError(ErrorCode.STALE_SNAPSHOT, "old")
    state = make_state(make_element(0, "button", "Ok"))
    executor, _ = build(FakeDriver(state, errors={"click": error}))
    with pytest.raises(DriverError) as raised:
        await executor.execute(step(action="click", index=0), state)
    assert raised.value is error


async def test_current_tab_name_follows_the_driver():
    executor, driver = build()
    assert executor.current_tab_name == "main"
    await executor.execute(step(action="new_tab", tab_name="correo"), None)
    assert executor.current_tab_name == "correo"
