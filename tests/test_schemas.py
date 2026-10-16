import inspect
import json
from typing import Any, Dict, List, get_args

import pytest
from pydantic import TypeAdapter, ValidationError

from babas.drivers.base import Target
from babas.schemas import (
    ACTION_MODELS,
    ALWAYS_CONFIRM,
    ActStep,
    ClickStep,
    GotoStep,
    Plan,
    PressKeyStep,
    ReadTextStep,
    ScrollStep,
    SelectOptionStep,
    Step,
    TypeTextStep,
    WaitStep,
    changes_page,
    plan_json_schema,
)

ALL_ACTIONS = [
    "goto", "wait", "click", "type_text", "press_key", "screenshot", "scroll",
    "smart_search", "smart_click", "analyze_page", "new_tab", "switch_tab", "close_tab",
    "search_youtube", "click_first_video", "open_gmail", "compose_email", "send_email",
    "open_whatsapp", "search_contact", "send_whatsapp_message", "select_option", "read_text",
    "ask_user", "done",
]

# The examples of the legacy prompt; "down|up" is already replaced by "down".
LEGACY_EXAMPLES: List[Dict[str, Any]] = [
    {"action": "goto", "value": "URL"},
    {"action": "goto", "value": "https://leetcode.com"},
    {"action": "wait", "ms": 2000},
    {"action": "wait", "ms": 3000},
    {"action": "click", "selector": "CSS"},
    {"action": "type_text", "selector": "CSS", "value": "text"},
    {"action": "press_key", "value": "Enter"},
    {"action": "scroll", "value": "down"},
    {"action": "smart_search", "value": "text"},
    {"action": "smart_search", "value": "two sum"},
    {"action": "smart_click", "value": "visible text"},
    {"action": "analyze_page"},
    {"action": "new_tab", "tab_name": "name"},
    {"action": "switch_tab", "tab_name": "name"},
    {"action": "close_tab"},
    {"action": "search_youtube", "value": "query"},
    {"action": "click_first_video"},
    {"action": "open_gmail"},
    {"action": "compose_email", "to": "email", "subject": "subject", "body": "message"},
    {"action": "send_email"},
    {"action": "open_whatsapp"},
    {"action": "search_contact", "value": "name"},
    {"action": "send_whatsapp_message", "body": "message"},
]

NEW_EXAMPLES: List[Dict[str, Any]] = [
    {"action": "select_option", "index": 7, "value": "Jalisco"},
    {"action": "read_text", "index": 3},
    {"action": "read_text"},
    {"action": "ask_user", "value": "Cual es su CURP?"},
    {"action": "ask_user", "value": "Inicie sesion y avise.", "kind": "handoff"},
    {"action": "done", "value": "Listo.", "success": False},
]

STEP = TypeAdapter(Step)
ACT_STEP = TypeAdapter(ActStep)


def parse(data: Dict[str, Any]):
    return STEP.validate_python(data)


def label(data: Dict[str, Any]) -> str:
    return json.dumps(data, sort_keys=True)


@pytest.mark.parametrize("data", LEGACY_EXAMPLES, ids=label)
def test_legacy_prompt_examples_validate(data: Dict[str, Any]) -> None:
    step = parse(data)
    assert step.action == data["action"]
    assert type(step) is ACTION_MODELS[data["action"]]


@pytest.mark.parametrize("data", NEW_EXAMPLES, ids=label)
def test_new_action_examples_validate(data: Dict[str, Any]) -> None:
    step = parse(data)
    assert type(step) is ACTION_MODELS[data["action"]]


def test_legacy_goto_example_is_normalized() -> None:
    assert parse({"action": "goto", "value": "URL"}).value == "https://URL"
    assert parse({"action": "goto", "value": "https://leetcode.com"}).value == "https://leetcode.com"


def test_legacy_click_selector_becomes_a_target() -> None:
    target = parse({"action": "click", "selector": "CSS"}).to_target("s1")
    assert target == Target(selector="CSS")


def test_action_models_cover_all_25_actions() -> None:
    assert list(ACTION_MODELS) == ALL_ACTIONS
    assert len(ACTION_MODELS) == 25
    for action, model in ACTION_MODELS.items():
        assert get_args(model.model_fields["action"].annotation) == (action,)
        assert model.__name__.endswith("Step")


def test_step_union_has_every_model() -> None:
    members = get_args(get_args(Step)[0])
    assert set(members) == set(ACTION_MODELS.values())


def test_act_step_excludes_ask_user_and_done() -> None:
    members = set(get_args(get_args(ActStep)[0]))
    assert members == set(ACTION_MODELS.values()) - {ACTION_MODELS["ask_user"], ACTION_MODELS["done"]}
    for data in ({"action": "ask_user", "value": "?"}, {"action": "done", "value": "ok"}):
        with pytest.raises(ValidationError):
            ACT_STEP.validate_python(data)
    assert ACT_STEP.validate_python({"action": "click", "index": 1}).action == "click"


@pytest.mark.parametrize("action", ALL_ACTIONS)
def test_every_model_example_validates(action: str) -> None:
    model = ACTION_MODELS[action]
    example = model.EXAMPLE
    assert example["action"] == action
    assert example.get("explanation")
    assert type(model.model_validate(example)) is model
    assert type(parse(example)) is model
    assert type(Plan.model_validate({"steps": [example]}).steps[0]) is model


@pytest.mark.parametrize("action", ALL_ACTIONS)
def test_every_model_has_a_one_line_docstring(action: str) -> None:
    doc = inspect.getdoc(ACTION_MODELS[action])
    assert doc and "\n" not in doc
    assert len(doc) <= 120


@pytest.mark.parametrize("action", ["click", "type_text", "select_option"])
def test_missing_target_fails(action: str) -> None:
    data: Dict[str, Any] = {"action": action}
    if action != "click":
        data["value"] = "x"
    with pytest.raises(ValidationError) as info:
        parse(data)
    assert "needs an index, a selector or a text" in str(info.value)


@pytest.mark.parametrize("blank", [{"selector": ""}, {"selector": "   "}, {"text": ""}, {"selector": "", "text": " "}])
def test_blank_target_counts_as_missing(blank: Dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        parse({"action": "click", **blank})


@pytest.mark.parametrize(
    "locator",
    [{"index": 0}, {"index": 3}, {"selector": "#go"}, {"text": "Buscar"}, {"index": None, "selector": "#go"}],
)
def test_any_single_locator_satisfies_a_required_target(locator: Dict[str, Any]) -> None:
    assert parse({"action": "click", **locator}).to_target("s1") is not None


def test_index_zero_is_a_valid_target() -> None:
    assert parse({"action": "click", "index": 0}).to_target("s1") == Target(index=0, snapshot_id="s1")


@pytest.mark.parametrize("action", ["press_key", "read_text"])
def test_target_is_optional_for_press_key_and_read_text(action: str) -> None:
    step = parse({"action": action, "value": "Enter"})
    assert step.to_target("s1") is None
    assert PressKeyStep.TARGET_REQUIRED is False and ReadTextStep.TARGET_REQUIRED is False
    assert ClickStep.TARGET_REQUIRED and TypeTextStep.TARGET_REQUIRED and SelectOptionStep.TARGET_REQUIRED


def test_to_target_precedence_is_index_then_selector_then_text() -> None:
    both = parse({"action": "click", "index": 2, "selector": "#a", "text": "Hola"})
    assert both.to_target("snap-9") == Target(index=2, snapshot_id="snap-9")
    assert both.to_target(None) == Target(index=2, snapshot_id=None)
    selector = parse({"action": "click", "selector": "#a", "text": "Hola"})
    assert selector.to_target("snap-9") == Target(selector="#a")
    text = parse({"action": "click", "text": "Hola"})
    assert text.to_target("snap-9") == Target(text="Hola")


def test_press_key_with_target_resolves_it() -> None:
    step = parse({"action": "press_key", "value": "Enter", "index": 5})
    assert step.to_target("s2") == Target(index=5, snapshot_id="s2")


def test_unknown_action_fails() -> None:
    with pytest.raises(ValidationError):
        parse({"action": "explode"})
    with pytest.raises(ValidationError):
        Plan.model_validate({"steps": [{"action": "launch_missiles", "value": "x"}]})


def test_missing_or_null_action_fails() -> None:
    with pytest.raises(ValidationError):
        parse({"value": "https://example.com"})
    with pytest.raises(ValidationError):
        parse({"action": None, "value": "https://example.com"})


def test_empty_steps_fails() -> None:
    with pytest.raises(ValidationError):
        Plan.model_validate({"steps": []})
    with pytest.raises(ValidationError):
        Plan.model_validate({"reasoning": "nothing to do"})


@pytest.mark.parametrize(
    "data,expected",
    [
        ({"action": "goto", "value": "gob.mx"}, True),
        ({"action": "wait", "ms": 500}, False),
        ({"action": "click", "index": 1}, True),
        ({"action": "type_text", "index": 1, "value": "x"}, False),
        ({"action": "press_key", "value": "Enter"}, True),
        ({"action": "press_key", "value": "enter"}, True),
        ({"action": "press_key", "value": "Return"}, True),
        ({"action": "press_key", "value": "Tab"}, False),
        ({"action": "press_key", "value": "Escape"}, False),
        ({"action": "press_key", "value": "Enter", "index": 2}, True),
        ({"action": "screenshot"}, False),
        ({"action": "scroll", "value": "down"}, False),
        ({"action": "smart_search", "value": "x"}, True),
        ({"action": "smart_click", "value": "x"}, True),
        ({"action": "analyze_page"}, True),
        ({"action": "new_tab"}, True),
        ({"action": "switch_tab", "tab_name": "a"}, True),
        ({"action": "close_tab"}, True),
        ({"action": "search_youtube", "value": "x"}, True),
        ({"action": "click_first_video"}, True),
        ({"action": "open_gmail"}, True),
        ({"action": "compose_email", "to": "a@b.mx"}, True),
        ({"action": "send_email"}, True),
        ({"action": "open_whatsapp"}, True),
        ({"action": "search_contact", "value": "x"}, True),
        ({"action": "send_whatsapp_message", "body": "x"}, True),
        ({"action": "select_option", "index": 1, "value": "x"}, False),
        ({"action": "read_text"}, False),
        ({"action": "ask_user", "value": "?"}, True),
        ({"action": "done", "value": "ok"}, False),
    ],
    ids=lambda value: value["action"] + ":" + str(value.get("value", "")) if isinstance(value, dict) else str(value),
)
def test_changes_page_table(data: Dict[str, Any], expected: bool) -> None:
    assert changes_page(parse(data)) is expected


def test_changes_page_is_decided_for_every_action() -> None:
    assert set(ACTION_MODELS) == set(ALL_ACTIONS)
    static = {"wait", "type_text", "screenshot", "scroll", "select_option", "read_text", "done"}
    for action, model in ACTION_MODELS.items():
        if action == "press_key":
            continue
        assert model.CHANGES_PAGE is (action not in static), action


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("gob.mx", "https://gob.mx"),
        ("www.youtube.com/watch?v=1", "https://www.youtube.com/watch?v=1"),
        ("localhost:8080/app", "https://localhost:8080/app"),
        ("  gob.mx  ", "https://gob.mx"),
        ("//gob.mx/tramites", "https://gob.mx/tramites"),
        ("https://gob.mx", "https://gob.mx"),
        ("http://127.0.0.1:8000/x", "http://127.0.0.1:8000/x"),
        ("HTTPS://GOB.MX", "HTTPS://GOB.MX"),
    ],
)
def test_goto_normalization(raw: str, expected: str) -> None:
    assert parse({"action": "goto", "value": raw}).value == expected
    assert GotoStep(action="goto", value=raw).value == expected


@pytest.mark.parametrize("raw", ["", "   ", "file:///etc/passwd", "ftp://example.com", "chrome://settings"])
def test_goto_rejects_empty_and_non_http_urls(raw: str) -> None:
    with pytest.raises(ValidationError):
        parse({"action": "goto", "value": raw})


def test_goto_requires_a_value() -> None:
    with pytest.raises(ValidationError):
        parse({"action": "goto"})


@pytest.mark.parametrize(
    "raw,expected",
    [(-5, 0), (0, 0), (1500, 1500), (30000, 30000), (30001, 30000), (99999999, 30000), ("2500", 2500), (3000.0, 3000)],
)
def test_wait_clamps_ms(raw: Any, expected: int) -> None:
    assert parse({"action": "wait", "ms": raw}).ms == expected


def test_wait_defaults_to_two_seconds() -> None:
    assert parse({"action": "wait"}).ms == 2000
    assert parse({"action": "wait", "ms": None}).ms == 2000
    assert WaitStep(action="wait", ms=40000).ms == 30000


def test_scroll_defaults() -> None:
    step = parse({"action": "scroll"})
    assert step.value == "down"
    assert step.ms == 600
    assert parse({"action": "scroll", "value": "up", "ms": 250}).ms == 250
    assert parse({"action": "scroll", "ms": None}).ms == 600


@pytest.mark.parametrize("raw", ["UP", " Down ", "up"])
def test_scroll_direction_is_case_insensitive(raw: str) -> None:
    assert parse({"action": "scroll", "value": raw}).value == raw.strip().lower()


def test_scroll_rejects_other_directions() -> None:
    with pytest.raises(ValidationError):
        parse({"action": "scroll", "value": "down|up"})
    with pytest.raises(ValidationError):
        parse({"action": "scroll", "value": "left"})


def test_scroll_pixels_are_kept_in_a_sane_range() -> None:
    assert ScrollStep(action="scroll", ms=-300).ms == 1
    assert ScrollStep(action="scroll", ms=10**7).ms == 10000


@pytest.mark.parametrize(
    "raw,expected",
    [("Enter", "Enter"), ("enter", "Enter"), (" RETURN ", "Enter"), ("esc", "Escape"), ("tab", "Tab"), ("a", "a"), (" ", " ")],
)
def test_press_key_names_are_canonical(raw: str, expected: str) -> None:
    assert parse({"action": "press_key", "value": raw}).value == expected


def test_press_key_requires_a_key() -> None:
    with pytest.raises(ValidationError):
        parse({"action": "press_key"})
    with pytest.raises(ValidationError):
        parse({"action": "press_key", "value": ""})


def test_type_text_allows_an_empty_value_but_needs_one() -> None:
    assert parse({"action": "type_text", "index": 1, "value": ""}).value == ""
    with pytest.raises(ValidationError):
        parse({"action": "type_text", "index": 1})


@pytest.mark.parametrize(
    "data",
    [
        {"action": "smart_search"},
        {"action": "smart_search", "value": ""},
        {"action": "smart_click"},
        {"action": "search_youtube"},
        {"action": "search_contact", "value": ""},
        {"action": "compose_email"},
        {"action": "compose_email", "to": ""},
        {"action": "send_whatsapp_message"},
        {"action": "send_whatsapp_message", "body": ""},
        {"action": "select_option", "index": 1},
        {"action": "ask_user"},
        {"action": "ask_user", "value": "?", "kind": "shout"},
        {"action": "done"},
    ],
    ids=lambda data: data["action"] + str(sorted(data)),
)
def test_missing_required_fields_fail(data: Dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        parse(data)


def test_compose_email_optional_fields() -> None:
    step = parse({"action": "compose_email", "to": "ana@example.com"})
    assert step.subject is None and step.body is None


def test_tab_steps_accept_name_and_fallback_value() -> None:
    for action in ("new_tab", "switch_tab", "close_tab"):
        bare = parse({"action": action})
        assert bare.tab_name is None and bare.value is None
        both = parse({"action": action, "tab_name": "a", "value": "b"})
        assert (both.tab_name, both.value) == ("a", "b")


def test_ask_user_and_done_defaults() -> None:
    assert parse({"action": "ask_user", "value": "?"}).kind == "text"
    assert parse({"action": "done", "value": "ok"}).success is True


def test_step_base_defaults() -> None:
    step = parse({"action": "click", "index": 1})
    assert step.explanation is None
    assert step.consequential is False


def test_extra_keys_are_ignored() -> None:
    step = parse({"action": "click", "index": 1, "color": "red", "confidence": 0.9})
    assert not hasattr(step, "color")
    assert "color" not in step.model_dump()
    plan = Plan.model_validate({"steps": [{"action": "analyze_page", "foo": 1}], "reasoning": "r", "extra": True})
    assert len(plan.steps) == 1 and not hasattr(plan, "extra")


def test_null_values_fall_back_to_defaults() -> None:
    step = parse({"action": "click", "index": 1, "explanation": None, "consequential": None, "selector": None})
    assert step.consequential is False and step.explanation is None and step.selector is None
    assert parse({"action": "done", "value": "ok", "success": None}).success is True
    assert parse({"action": "ask_user", "value": "?", "kind": None}).kind == "text"


def test_index_accepts_numeric_strings_but_not_garbage() -> None:
    assert parse({"action": "click", "index": "4"}).index == 4
    with pytest.raises(ValidationError):
        parse({"action": "click", "index": "four"})


def test_plan_validates_a_realistic_llm_answer() -> None:
    answer = {
        "steps": [
            {"action": "goto", "value": "www.imss.gob.mx", "explanation": "Voy a abrir la página del IMSS."},
            {"action": "wait", "ms": 3000},
            {"action": "analyze_page", "explanation": "Reviso qué hay en la página."},
            {"action": "click", "index": 14, "selector": None, "explanation": "Presiono Consulta de NSS."},
            {"action": "type_text", "index": 3, "value": "GOMC800101HDFRRL09", "explanation": "Escribo su CURP."},
            {"action": "press_key", "value": "enter", "index": 3, "explanation": "Envío la consulta."},
            {"action": "select_option", "index": 8, "value": "Jalisco"},
            {"action": "read_text", "explanation": "Leo el resultado.", "unexpected": 1},
            {"action": "ask_user", "value": "¿Me confirma su correo?", "kind": "text"},
            {"action": "done", "value": "Su número de seguridad social aparece en pantalla.", "success": True},
        ],
        "reasoning": "Abrir el sitio, capturar la CURP y leer el resultado.",
    }
    plan = Plan.model_validate(answer)
    assert [step.action for step in plan.steps] == [item["action"] for item in answer["steps"]]
    assert plan.reasoning
    goto, _, _, click, typed, press = plan.steps[:6]
    assert goto.value == "https://www.imss.gob.mx"
    assert click.to_target("s1") == Target(index=14, snapshot_id="s1")
    assert typed.value == "GOMC800101HDFRRL09"
    assert press.value == "Enter" and changes_page(press)
    assert changes_page(plan.steps[-2]) and not changes_page(plan.steps[-1])


def test_plan_reports_which_step_is_wrong() -> None:
    with pytest.raises(ValidationError) as info:
        Plan.model_validate({"steps": [{"action": "analyze_page"}, {"action": "click"}]})
    assert "steps.1.click" in str(info.value)


def test_always_confirm_set() -> None:
    assert ALWAYS_CONFIRM == {"send_email", "send_whatsapp_message"}
    assert ALWAYS_CONFIRM <= set(ACTION_MODELS)


def test_plan_json_schema_is_usable_by_a_provider() -> None:
    schema = plan_json_schema()
    text = json.dumps(schema)
    assert json.loads(text) == schema
    assert schema["required"] == ["steps"]
    steps = schema["properties"]["steps"]
    assert steps["minItems"] == 1
    mapping = steps["items"]["discriminator"]["mapping"]
    assert set(mapping) == set(ALL_ACTIONS)
    for action, ref in mapping.items():
        definition = schema["$defs"][ref.rsplit("/", 1)[1]]
        assert definition["properties"]["action"]["const"] == action
        assert "action" in definition["required"]
        assert definition["description"] == inspect.getdoc(ACTION_MODELS[action])


def test_dump_round_trips_through_the_union() -> None:
    plan = Plan.model_validate({"steps": [m.EXAMPLE for m in ACTION_MODELS.values()]})
    again = Plan.model_validate(json.loads(plan.model_dump_json()))
    assert again == plan
