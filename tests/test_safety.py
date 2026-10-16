import json
import logging
from pathlib import Path

import pytest

from babas.logs import RedactingFilter, setup_logging
from babas.safety import (
    CAPTCHA_TEXT,
    DEFAULT_ALLOWED_DOMAINS,
    HANDOFF_TEXTS,
    PII_PATTERNS,
    Decision,
    SafetyPolicy,
    describe_action_es,
    detect_pii,
    is_web_address,
    mask,
    normalize_host,
    redact,
    title_es,
)
from helpers import make_element

CASES = json.loads((Path(__file__).parent / "fixtures" / "pii_cases.json").read_text(encoding="utf-8"))
CURP = "GOMC800101HDFRRL09"
MASKED_CURP = "GO**************09"


def _positive():
    return [(kind, value) for kind, values in CASES["positive"].items() for value in values]


@pytest.mark.parametrize("kind,value", _positive())
def test_pii_vectors(kind, value):
    assert kind in detect_pii(value)


@pytest.mark.parametrize("value", CASES["negative"])
def test_pii_negative_vectors(value):
    assert detect_pii(value) == []
    assert redact(value) == value


def test_pii_vectors_cover_every_kind():
    assert set(CASES["positive"]) == set(PII_PATTERNS)


@pytest.mark.parametrize("value", ["4111 1111 1111 1112", "4111111111111112", "1234 5678 9012 3456", "424242424242"])
def test_card_needs_luhn_and_length(value):
    assert "card" not in detect_pii(value)


def test_detect_pii_reports_each_kind_once_and_several_kinds():
    kinds = detect_pii("ana@example.com, bob@example.com y 55 1234 5678")
    assert kinds.count("email") == 1
    assert "phone" in kinds
    assert detect_pii("") == [] and detect_pii(None) == []


@pytest.mark.parametrize(
    "value,expected",
    [
        ("", ""),
        ("ab", "**"),
        ("abcd", "****"),
        ("abcde", "ab*de"),
        (CURP, MASKED_CURP),
        ("ana@example.com", "an***********om"),
    ],
)
def test_mask(value, expected):
    assert mask(value) == expected


def test_redact():
    assert redact(CURP) == MASKED_CURP
    assert redact(f"Mi CURP es {CURP}.") == f"Mi CURP es {MASKED_CURP}."


@pytest.mark.parametrize(
    "text",
    ["2026-10-11", "recetas de pozole", "$1,234.50", "Paso 3 de 10, pagina 1234", "total 1,234.56 pesos", "", "v1.2.3 build 20261011"],
)
def test_redact_leaves_ordinary_text_alone(text):
    assert redact(text) == text


def test_redact_masks_every_match_in_mixed_text():
    text = f"CURP {CURP}, correo ana@example.com, tel 55 1234 5678, tarjeta 4111 1111 1111 1111, NSS 12-34-56-7890-1"
    out = redact(text)
    for secret in (CURP, "ana@example.com", "55 1234 5678", "4111 1111 1111 1111", "12-34-56-7890-1"):
        assert secret not in out
    assert MASKED_CURP in out
    assert out.startswith("CURP ") and ", correo " in out and ", tel " in out
    assert redact(out) == out


def test_redact_merges_overlapping_matches_into_one_mask():
    out = redact("ABC800101XY2@example.com")
    assert out == mask("ABC800101XY2@example.com")
    assert "800101" not in out


def test_redact_does_not_hide_the_luhn_failures_it_cannot_prove():
    assert redact("4111 1111 1111 1112") == "4111 1111 1111 1112"


def test_redact_survives_odd_input():
    assert redact(None) == ""
    assert redact(12345) == "12345"
    assert redact("x" * 5000) == "x" * 5000


def test_domains():
    policy = SafetyPolicy()
    assert policy.domain_allowed("https://sat.gob.mx/tramites")
    assert policy.domain_allowed("gob.mx")
    assert policy.domain_allowed("www.youtube.com")
    assert policy.domain_allowed("https://WWW.YouTube.com/watch?v=1")
    assert not policy.domain_allowed("gob.mx.evil.com")
    assert not policy.domain_allowed("https://evilgob.mx/")
    assert not policy.domain_allowed("https://example.com/")
    assert SafetyPolicy(["*"]).domain_allowed("https://example.com/")
    assert SafetyPolicy("*").domain_allowed("anything.example")
    assert SafetyPolicy(["gob.mx", "*"]).domain_allowed("anything.example")


@pytest.mark.parametrize(
    "url",
    [
        "http://evil.com\\@gob.mx/",
        "https://gob.mx@evil.com/",
        "https://gob.mx.evil.com/",
        "http://evil.com/?next=https://gob.mx",
        "http://evil.com#gob.mx",
        "https://%67ob.mx/",
        "http://gob.mx\t.evil.com/",
        "http://",
        "https://exa mple.com/",
        "blob:https://evil.com/0b3a",
        "view-source:https://evil.com/",
    ],
)
def test_domain_tricks_are_not_allowed(url):
    assert not SafetyPolicy().domain_allowed(url)


@pytest.mark.parametrize("url", ["about:blank", "chrome://newtab", "chrome-extension://abc/sidepanel.html", "", "   ", "file:///tmp/x.html"])
def test_non_web_addresses_are_not_http_sites(url):
    policy = SafetyPolicy()
    assert policy.domain_allowed(url)
    assert normalize_host(url) == ""
    assert not is_web_address(url)


def test_approved_domains_and_ports():
    policy = SafetyPolicy()
    assert not policy.domain_allowed("https://example.com:8443/x")
    policy.approve_domain("Example.com")
    assert policy.domain_allowed("https://example.com:8443/x")
    assert policy.domain_allowed("https://sub.example.com/")
    assert not policy.domain_allowed("https://example.org/")
    policy.approve_domain("about:blank")
    assert "" not in policy.approved_domains


def test_ip_allowlist_entries_match_exactly():
    policy = SafetyPolicy([*DEFAULT_ALLOWED_DOMAINS, "127.0.0.1"])
    assert policy.domain_allowed("http://127.0.0.1:8000/form.html")
    assert not policy.domain_allowed("http://evil.127.0.0.1/")
    assert not policy.domain_allowed("http://localhost:8000/")


def test_policy_from_env():
    assert SafetyPolicy.from_env({"BABAS_ALLOWED_DOMAINS": "*"}).domain_allowed("https://example.com")
    extra = SafetyPolicy.from_env({"BABAS_ALLOWED_DOMAINS": "example.com, example.org"})
    assert extra.domain_allowed("https://example.org") and extra.domain_allowed("https://sat.gob.mx")
    assert not extra.domain_allowed("https://example.net")
    assert not SafetyPolicy.from_env({}).domain_allowed("https://example.com")


def test_default_allowlist_is_the_documented_one():
    assert DEFAULT_ALLOWED_DOMAINS == ("gob.mx", "google.com", "google.com.mx", "youtube.com", "whatsapp.com", "wikipedia.org")
    assert SafetyPolicy().allowed_domains == DEFAULT_ALLOWED_DOMAINS


@pytest.mark.parametrize(
    "value,expected",
    [
        ("https://www.YouTube.com/watch?v=1", "youtube.com"),
        ("http://user:secret@Sat.GOB.mx:8443/a/b?c=d#e", "sat.gob.mx"),
        ("gob.mx", "gob.mx"),
        ("www.gob.mx/tramites", "gob.mx"),
        ("localhost:3000/x", "localhost"),
        ("http://127.0.0.1:8765/", "127.0.0.1"),
        ("http://[::1]:8080/", "::1"),
        ("https://bücher.example/", "xn--bcher-kva.example"),
        ("https://gob.mx./", "gob.mx"),
        ("//www.gob.mx/x", "gob.mx"),
        ("HTTPS://GOB.MX", "gob.mx"),
        ("about:blank", ""),
        ("chrome://extensions", ""),
        ("javascript:alert(1)", ""),
        ("mailto:ana@example.com", ""),
        ("", ""),
        ("http://", ""),
    ],
)
def test_normalize_host(value, expected):
    assert normalize_host(value) == expected


def test_normalize_host_tolerates_non_strings():
    assert normalize_host(None) == ""
    assert normalize_host(42) == ""


def el(role="textbox", label="Campo", *, tag=None, flags=()):
    return make_element(0, role, label, tag=tag, flags=flags)


SUBMIT = el("button", "Continuar", flags=["submit"])
SEARCH_SUBMIT = el("button", "Buscar", flags=["submit", "search_form"])
CONSEQUENTIAL = el("button", "Entregar tarea", flags=["consequential"])
PAYMENT_BTN = el("button", "Pagar", flags=["consequential", "payment"])
PLAIN_BUTTON = el("button", "Cerrar")
LINK = el("link", "Inicio")
PII_FIELD = el("textbox", "Correo", flags=["pii"])
CARD_FIELD = el("textbox", "Número de tarjeta", flags=["pii", "payment"])
PLAIN_FIELD = el("textbox", "Nombre")
PASSWORD = el("textbox", "Contraseña", flags=["password"])
CAPTCHA = el("checkbox", "No soy un robot", tag="input", flags=["captcha"])
SEARCH_BOX = el("searchbox", "Buscar", flags=["search_form"])
TEXTAREA = el("textbox", "Comentarios", tag="textarea")
SEARCH_TEXTAREA = el("textbox", "Buscar", tag="textarea", flags=["search_form"])
EDITOR = el("textbox", "Mensaje", tag="div")
SELECT = el("combobox", "Estado", tag="select")
PII_SELECT = el("combobox", "Teléfono", tag="select", flags=["pii"])
CHECKBOX = el("checkbox", "Acepto", tag="input")

RULES = [
    ("click_plain_button", "click", PLAIN_BUTTON, {}, "allow", []),
    ("click_link", "click", LINK, {}, "allow", []),
    ("click_submit", "click", SUBMIT, {}, "confirm", ["submit_form"]),
    ("click_search_submit_exempt", "click", SEARCH_SUBMIT, {}, "allow", []),
    ("click_consequential", "click", CONSEQUENTIAL, {}, "confirm", ["consequential"]),
    ("click_payment", "click", PAYMENT_BTN, {}, "confirm", ["consequential", "payment"]),
    ("click_everything_in_order", "click", el("button", "Pagar", flags=["submit", "consequential", "payment"]), {}, "confirm", ["submit_form", "consequential", "payment"]),
    ("click_unknown_element", "click", None, {}, "allow", []),
    ("click_password_is_fine", "click", PASSWORD, {}, "allow", []),
    ("type_plain", "type", PLAIN_FIELD, {"value": "Ana"}, "allow", []),
    ("type_empty", "type", PLAIN_FIELD, {"value": ""}, "allow", []),
    ("type_pii_field", "type", PII_FIELD, {"value": "hola"}, "confirm", ["pii_field"]),
    ("type_payment_field", "type", CARD_FIELD, {"value": "x"}, "confirm", ["pii_field", "payment"]),
    ("type_pii_value", "type", PLAIN_FIELD, {"value": CURP}, "confirm", ["pii_value"]),
    ("type_pii_field_and_value", "type", PII_FIELD, {"value": "ana@example.com"}, "confirm", ["pii_field", "pii_value"]),
    ("type_card_value", "type", PLAIN_FIELD, {"value": "4111 1111 1111 1111"}, "confirm", ["pii_value"]),
    ("type_into_search_box_pii_value", "type", SEARCH_BOX, {"value": "5512345678"}, "confirm", ["pii_value"]),
    ("type_search_text", "type", SEARCH_BOX, {"value": "recetas de pozole"}, "allow", []),
    ("type_password", "type", PASSWORD, {"value": "x"}, "handoff", ["password_field"]),
    ("type_password_beats_force", "type", PASSWORD, {"value": CURP, "force_confirm": True, "llm_consequential": True}, "handoff", ["password_field"]),
    ("type_captcha", "type", CAPTCHA, {"value": "x"}, "handoff", ["captcha"]),
    ("click_captcha", "click", CAPTCHA, {}, "handoff", ["captcha"]),
    ("press_captcha", "press", CAPTCHA, {"key": "Enter"}, "handoff", ["captcha"]),
    ("password_before_captcha", "type", el("textbox", "x", flags=["password", "captcha"]), {"value": "x"}, "handoff", ["password_field"]),
    ("select_plain", "select", SELECT, {"value": "Jalisco"}, "allow", []),
    ("select_pii_field", "select", PII_SELECT, {"value": "55"}, "confirm", ["pii_field"]),
    ("select_pii_value", "select", SELECT, {"value": CURP}, "confirm", ["pii_value"]),
    ("select_consequential", "select", el("combobox", "Enviar a", tag="select", flags=["consequential"]), {"value": "x"}, "confirm", ["consequential"]),
    ("select_payment", "select", el("combobox", "Tarjeta", tag="select", flags=["payment"]), {"value": "x"}, "confirm", ["payment"]),
    ("press_enter_no_element", "press", None, {"key": "Enter"}, "confirm", ["enter_submits_form"]),
    ("press_enter_text_input", "press", PLAIN_FIELD, {"key": "Enter"}, "confirm", ["enter_submits_form"]),
    ("press_return_alias", "press", PLAIN_FIELD, {"key": "Return"}, "confirm", ["enter_submits_form"]),
    ("press_enter_lowercase", "press", PLAIN_FIELD, {"key": "enter"}, "confirm", ["enter_submits_form"]),
    ("press_enter_search_input", "press", SEARCH_BOX, {"key": "Enter"}, "allow", []),
    ("press_enter_textarea", "press", TEXTAREA, {"key": "Enter"}, "confirm", ["enter_in_editor"]),
    ("press_enter_search_textarea", "press", SEARCH_TEXTAREA, {"key": "Enter"}, "allow", []),
    ("press_enter_contenteditable", "press", EDITOR, {"key": "Enter"}, "confirm", ["enter_in_editor"]),
    ("press_enter_select", "press", SELECT, {"key": "Enter"}, "allow", []),
    ("press_enter_checkbox", "press", CHECKBOX, {"key": "Enter"}, "confirm", ["enter_submits_form"]),
    ("press_enter_submit_button", "press", SUBMIT, {"key": "Enter"}, "confirm", ["submit_form"]),
    ("press_enter_search_submit", "press", SEARCH_SUBMIT, {"key": "Enter"}, "allow", []),
    ("press_enter_consequential_button", "press", CONSEQUENTIAL, {"key": "Enter"}, "confirm", ["consequential"]),
    ("press_enter_payment_button", "press", PAYMENT_BTN, {"key": "Enter"}, "confirm", ["consequential", "payment"]),
    ("press_enter_plain_button", "press", PLAIN_BUTTON, {"key": "Enter"}, "allow", []),
    ("press_enter_plain_link", "press", LINK, {"key": "Enter"}, "allow", []),
    ("press_space_consequential_button", "press", CONSEQUENTIAL, {"key": " "}, "confirm", ["consequential"]),
    ("press_space_plain_button", "press", PLAIN_BUTTON, {"key": "Space"}, "allow", []),
    ("press_tab", "press", PLAIN_FIELD, {"key": "Tab"}, "allow", []),
    ("press_escape_no_element", "press", None, {"key": "Escape"}, "allow", []),
    ("press_char_plain", "press", PLAIN_FIELD, {"key": "a"}, "allow", []),
    ("press_char_pii_field", "press", PII_FIELD, {"key": "a"}, "confirm", ["pii_field"]),
    ("press_char_payment_field", "press", CARD_FIELD, {"key": "4"}, "confirm", ["pii_field", "payment"]),
    ("press_char_password", "press", PASSWORD, {"key": "a"}, "handoff", ["password_field"]),
    ("press_space_in_password", "press", PASSWORD, {"key": " "}, "handoff", ["password_field"]),
    ("press_enter_in_password", "press", PASSWORD, {"key": "Enter"}, "confirm", ["enter_submits_form"]),
    ("force_confirm_only", "click", PLAIN_BUTTON, {"force_confirm": True}, "confirm", ["macro"]),
    ("llm_consequential_only", "click", PLAIN_BUTTON, {"llm_consequential": True}, "confirm", ["llm_consequential"]),
    ("llm_consequential_on_type", "type", PLAIN_FIELD, {"value": "Ana", "llm_consequential": True}, "confirm", ["llm_consequential"]),
    ("macro_after_element_rules", "click", SUBMIT, {"force_confirm": True, "llm_consequential": True}, "confirm", ["submit_form", "macro", "llm_consequential"]),
    ("force_on_search_exempt", "click", SEARCH_SUBMIT, {"force_confirm": True}, "confirm", ["macro"]),
    ("type_no_element_pii_value", "type", None, {"value": CURP}, "confirm", ["pii_value"]),
    ("type_no_element_plain", "type", None, {"value": "hola"}, "allow", []),
]


@pytest.mark.parametrize("name,op,element,kwargs,kind,reasons", RULES, ids=[rule[0] for rule in RULES])
def test_gate_rules(name, op, element, kwargs, kind, reasons):
    decision = SafetyPolicy().evaluate(op, element, **kwargs)
    assert isinstance(decision, Decision)
    assert decision.kind == kind
    assert decision.reasons == reasons
    assert len(set(decision.reasons)) == len(decision.reasons)


def test_every_reason_the_spec_names_is_reachable():
    produced = {reason for *_, kind, reasons in RULES if kind == "confirm" for reason in reasons}
    assert produced >= {
        "submit_form",
        "consequential",
        "payment",
        "pii_field",
        "pii_value",
        "enter_submits_form",
        "enter_in_editor",
        "macro",
        "llm_consequential",
    }


def test_decision_defaults():
    assert Decision("allow").reasons == []
    assert Decision("allow").reasons is not Decision("allow").reasons


def test_handoff_texts_are_the_spanish_ones_of_the_spec():
    assert HANDOFF_TEXTS["password_field"] == "Por seguridad, BABAS no escribe contraseñas. Escriba su contraseña directamente en la página y luego presione «Listo»."
    assert HANDOFF_TEXTS["captcha"] == "La página pide comprobar que usted es una persona. Resuélvalo en la página y luego presione «Listo»."
    assert HANDOFF_TEXTS["qr"] == "Abra WhatsApp en su teléfono y escanee el código de la pantalla. Luego presione «Listo»."
    assert CAPTCHA_TEXT == HANDOFF_TEXTS["captcha"]


@pytest.mark.parametrize(
    "op,element,kwargs,expected",
    [
        ("click", LINK, {"host": "gob.mx"}, "Presionar «Inicio» en gob.mx"),
        ("type", PII_FIELD, {"value": "ana@example.com", "host": "gob.mx"}, "Escribir «ana@example.com» en «Correo» (gob.mx)"),
        ("select", SELECT, {"value": "Jalisco", "host": "gob.mx"}, "Elegir «Jalisco» en «Estado» (gob.mx)"),
        ("press", PLAIN_FIELD, {"key": "Enter", "host": "gob.mx"}, "Presionar la tecla Enter en «Nombre» (gob.mx)"),
        ("press", None, {"key": "Enter", "host": "gob.mx"}, "Presionar la tecla Enter en «la página» (gob.mx)"),
        ("click", None, {}, "Presionar «la página» en la página"),
        ("type", el("textbox", ""), {"value": "x", "host": "gob.mx"}, "Escribir «x» en «la página» (gob.mx)"),
    ],
)
def test_describe_action_es(op, element, kwargs, expected):
    assert describe_action_es(op, element, **kwargs) == expected


def test_describe_action_es_shows_the_full_value_and_tidies_labels():
    line = describe_action_es("type", el("textbox", "  CURP \n del   titular "), value=CURP, host="gob.mx")
    assert CURP in line and "«CURP del titular»" in line
    long = describe_action_es("click", el("button", "x" * 500), host="gob.mx")
    assert len(long) < 200 and "…" in long


@pytest.mark.parametrize(
    "reasons,title",
    [
        (["payment"], "Confirmar un pago"),
        (["consequential", "payment"], "Confirmar un pago"),
        (["pii_field", "payment"], "Confirmar un pago"),
        (["submit_form"], "Enviar un formulario"),
        (["enter_submits_form"], "Enviar un formulario"),
        (["pii_field", "submit_form"], "Enviar un formulario"),
        (["pii_field"], "Escribir datos personales"),
        (["pii_value"], "Escribir datos personales"),
        (["consequential"], "Acción importante"),
        (["macro"], "Acción importante"),
        (["enter_in_editor"], "Acción importante"),
        ([], "Acción importante"),
    ],
)
def test_title_es(reasons, title):
    assert title_es(reasons) == title


@pytest.fixture
def clean_babas_logger():
    logger = logging.getLogger("babas")
    handlers, level = list(logger.handlers), logger.level
    yield logger
    logger.handlers[:] = handlers
    logger.setLevel(level)


def test_redacting_filter_masks_messages_args_and_exceptions(caplog):
    caplog.handler.addFilter(RedactingFilter())
    logger = logging.getLogger("babas.test_safety")
    with caplog.at_level(logging.INFO, logger="babas.test_safety"):
        logger.info("typed %s into %s", CURP, "CURP")
        logger.info(f"plain f-string with {CURP}")
        try:
            raise ValueError(f"bad value {CURP} for ana@example.com")
        except ValueError:
            logger.exception("step failed")
    assert CURP not in caplog.text
    assert "ana@example.com" not in caplog.text
    assert f"typed {MASKED_CURP} into CURP" in caplog.text
    assert "step failed" in caplog.text and "ValueError" in caplog.text
    assert all(record.args == () for record in caplog.records)


def test_redacting_filter_keeps_ordinary_messages_and_bad_format_args(caplog):
    caplog.handler.addFilter(RedactingFilter())
    logger = logging.getLogger("babas.test_safety")
    with caplog.at_level(logging.INFO, logger="babas.test_safety"):
        logger.info("step %d of %d done in 2026-10-11", 3, 30)
        logger.info("broken %s %s", "only one")
    assert "step 3 of 30 done in 2026-10-11" in caplog.text


def test_setup_logging_installs_one_redacting_stderr_handler(clean_babas_logger, capsys):
    setup_logging("INFO")
    setup_logging("DEBUG")
    ours = [h for h in clean_babas_logger.handlers if getattr(h, "_babas_handler", False)]
    assert len(ours) == 1
    assert any(isinstance(f, RedactingFilter) for f in ours[0].filters)
    assert clean_babas_logger.level == logging.DEBUG
    logging.getLogger("babas.sample").warning("curp %s", CURP)
    err = capsys.readouterr().err
    assert MASKED_CURP in err and CURP not in err
    assert "babas.sample" in err
