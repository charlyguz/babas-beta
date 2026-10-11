import ipaddress
import os
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Literal, Optional, Pattern, Set, Tuple, Union

from babas.drivers.base import (
    FLAG_CAPTCHA,
    FLAG_CONSEQUENTIAL,
    FLAG_PASSWORD,
    FLAG_PAYMENT,
    FLAG_PII,
    FLAG_SEARCH_FORM,
    FLAG_SUBMIT,
    Element,
)

_I = re.IGNORECASE

PII_PATTERNS: Dict[str, Pattern[str]] = {
    "curp": re.compile(r"(?<![A-Z0-9])[A-Z]{4}[0-9]{6}[HMX][A-Z]{5}[A-Z0-9][0-9](?![A-Z0-9])", _I),
    "rfc": re.compile(r"(?<![A-Z0-9Ñ&])[A-ZÑ&]{3,4}[0-9]{6}(?:[A-Z0-9]{3})?(?![A-Z0-9])", _I),
    "email": re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", _I),
    "nss": re.compile(r"(?<![0-9])[0-9]{2}[\s-]?[0-9]{2}[\s-]?[0-9]{2}[\s-]?[0-9]{4}[\s-]?[0-9](?![0-9])"),
    "phone": re.compile(
        r"(?<![0-9+])(?:\+?52[\s-]?)?(?:1[\s-]?)?(?:\(?[0-9]{2}\)?[\s-]?[0-9]{4}|\(?[0-9]{3}\)?[\s-]?[0-9]{3})[\s-]?[0-9]{4}(?![0-9])"
    ),
    "card": re.compile(r"(?<![0-9])(?:[0-9][ -]?){12,18}[0-9](?![0-9])"),
}

DEFAULT_ALLOWED_DOMAINS: Tuple[str, ...] = (
    "gob.mx",
    "google.com",
    "google.com.mx",
    "youtube.com",
    "whatsapp.com",
    "wikipedia.org",
)

ALLOW_ALL = "*"

HANDOFF_TEXTS: Dict[str, str] = {
    "password_field": "Por seguridad, BABAS no escribe contraseñas. Escriba su contraseña directamente en la página y luego presione «Listo».",
    "captcha": "La página pide comprobar que usted es una persona. Resuélvalo en la página y luego presione «Listo».",
    "qr": "Abra WhatsApp en su teléfono y escanee el código de la pantalla. Luego presione «Listo».",
}
CAPTCHA_TEXT = HANDOFF_TEXTS["captcha"]

REASONS: Tuple[str, ...] = (
    "submit_form",
    "consequential",
    "payment",
    "pii_field",
    "pii_value",
    "enter_submits_form",
    "enter_in_editor",
    "macro",
    "llm_consequential",
)

_CLICKABLE_ROLES = frozenset(
    {"button", "link", "menuitem", "menuitemcheckbox", "menuitemradio", "tab", "option", "treeitem"}
)
_TEXTLIKE_ROLES = frozenset({"textbox", "searchbox", "combobox", "spinbutton"})
_ENTER_KEYS = frozenset({"enter", "return", "numpadenter"})
_SPACE_KEYS = frozenset({"space", "spacebar"})


def _luhn(digits: str) -> bool:
    total = 0
    double = False
    for ch in reversed(digits):
        d = ord(ch) - 48
        if double:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        double = not double
    return total % 10 == 0


def _card_ok(raw: str) -> bool:
    digits = "".join(ch for ch in raw if "0" <= ch <= "9")
    return len(digits) >= 13 and _luhn(digits)


def _matches(kind: str, pattern: Pattern[str], text: str):
    if kind == "email" and "@" not in text:
        return
    for match in pattern.finditer(text):
        if kind == "card" and not _card_ok(match.group(0)):
            continue
        yield match


def detect_pii(value: Optional[str]) -> List[str]:
    if not value:
        return []
    text = value if isinstance(value, str) else str(value)
    found: List[str] = []
    order = ["card"] + [kind for kind in PII_PATTERNS if kind != "card"]
    for kind in order:
        for _ in _matches(kind, PII_PATTERNS[kind], text):
            found.append(kind)
            break
    return found


def mask(value: str) -> str:
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}{'*' * (len(value) - 4)}{value[-2:]}"


def redact(text: str) -> str:
    if not text:
        return text if isinstance(text, str) else ""
    if not isinstance(text, str):
        text = str(text)
    spans: List[Tuple[int, int]] = []
    for kind, pattern in PII_PATTERNS.items():
        for match in _matches(kind, pattern, text):
            spans.append((match.start(), match.end()))
    if not spans:
        return text
    spans.sort()
    merged: List[List[int]] = []
    for start, end in spans:
        if merged and start < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    out: List[str] = []
    cursor = 0
    for start, end in merged:
        out.append(text[cursor:start])
        out.append(mask(text[start:end]))
        cursor = end
    out.append(text[cursor:])
    return "".join(out)


_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):")
_HOST_WITH_PORT_RE = re.compile(r"^[^/\\?#@:\s]+:[0-9]+(?:[/\\?#]|$)")
_PORT_RE = re.compile(r"^[0-9]*$")
_VALID_HOST_RE = re.compile(r"^[a-z0-9_-]+(?:\.[a-z0-9_-]+)*$")
_WRAPPER_SCHEMES = frozenset({"blob", "filesystem", "view-source"})

_NONE = ("none", "")
_INVALID = ("invalid", "")


def _parse_authority(rest: str) -> Tuple[str, str]:
    end = len(rest)
    for ch in "/\\?#":
        i = rest.find(ch)
        if i != -1 and i < end:
            end = i
    hostport = rest[:end].rsplit("@", 1)[-1]
    if hostport.startswith("["):
        close = hostport.find("]")
        if close == -1 or (hostport[close + 1 :] and not re.fullmatch(r":[0-9]*", hostport[close + 1 :])):
            return _INVALID
        try:
            ipaddress.IPv6Address(hostport[1:close])
        except ValueError:
            return _INVALID
        return "host", hostport[1:close].lower()
    host = hostport
    if ":" in hostport:
        host, _, port = hostport.rpartition(":")
        if not _PORT_RE.match(port):
            return _INVALID
    host = host.rstrip(".").lower()
    if not host:
        return _INVALID
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii").lower()
        except UnicodeError:
            return _INVALID
    if not _VALID_HOST_RE.match(host):
        return _INVALID
    if host.startswith("www."):
        host = host[4:]
        if not host:
            return _INVALID
    return "host", host


def _classify(value: object) -> Tuple[str, str]:
    if not isinstance(value, str):
        return _NONE
    text = value.strip()
    if not text:
        return _NONE
    rest = text
    match = _SCHEME_RE.match(text)
    if match and not _HOST_WITH_PORT_RE.match(text):
        scheme = match.group(1).lower()
        after = text[match.end() :]
        if scheme in ("http", "https"):
            rest = after.lstrip("/\\")
        elif scheme in _WRAPPER_SCHEMES:
            return _classify(after)
        else:
            return _NONE
    elif text.startswith(("//", "\\\\")):
        rest = text.lstrip("/\\")
    return _parse_authority(rest)


def normalize_host(url_or_host: object) -> str:
    kind, host = _classify(url_or_host)
    return host if kind == "host" else ""


def is_web_address(url_or_host: object) -> bool:
    return _classify(url_or_host)[0] != "none"


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


Op = Literal["click", "type", "select", "press"]


@dataclass
class Decision:
    kind: Literal["allow", "confirm", "handoff"]
    reasons: List[str] = field(default_factory=list)


def _add(reasons: List[str], reason: str) -> None:
    if reason not in reasons:
        reasons.append(reason)


def _click_reasons(element: Element, reasons: List[str]) -> None:
    if element.has(FLAG_SUBMIT) and not element.has(FLAG_SEARCH_FORM):
        _add(reasons, "submit_form")
    if element.has(FLAG_CONSEQUENTIAL):
        _add(reasons, "consequential")
    if element.has(FLAG_PAYMENT):
        _add(reasons, "payment")


def _typing_reasons(element: Element, value: Optional[str], reasons: List[str]) -> None:
    if element.has(FLAG_PII):
        _add(reasons, "pii_field")
    if element.has(FLAG_PAYMENT):
        _add(reasons, "payment")
    if value and detect_pii(value):
        _add(reasons, "pii_value")


def _is_clickable(element: Element) -> bool:
    return element.role.lower() in _CLICKABLE_ROLES or element.tag.lower() in ("button", "a") or element.has(FLAG_SUBMIT)


def _enter_reasons(element: Optional[Element], reasons: List[str]) -> None:
    if element is None:
        _add(reasons, "enter_submits_form")
        return
    if _is_clickable(element):
        _click_reasons(element, reasons)
        return
    tag = element.tag.lower()
    role = element.role.lower()
    search = element.has(FLAG_SEARCH_FORM)
    if tag == "select":
        return
    if tag == "textarea" or (role == "textbox" and tag != "input"):
        if not search:
            _add(reasons, "enter_in_editor")
        return
    if tag == "input":
        if not search:
            _add(reasons, "enter_submits_form")
        return
    if role in _TEXTLIKE_ROLES and not search:
        _add(reasons, "enter_in_editor")


def _parse_domains(value: Union[str, Iterable[str], None]) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return [str(part).strip() for part in value if str(part).strip()]


class SafetyPolicy:
    def __init__(self, allowed_domains: Union[str, Iterable[str]] = DEFAULT_ALLOWED_DOMAINS):
        self._allow_all = False
        self._allowed: List[str] = []
        self._approved: Set[str] = set()
        for entry in _parse_domains(allowed_domains):
            if entry == ALLOW_ALL:
                self._allow_all = True
                continue
            host = normalize_host(entry)
            if host and host not in self._allowed:
                self._allowed.append(host)

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "SafetyPolicy":
        raw = (env if env is not None else os.environ).get("BABAS_ALLOWED_DOMAINS", "")
        extra = _parse_domains(raw)
        if ALLOW_ALL in extra:
            return cls([ALLOW_ALL])
        return cls([*DEFAULT_ALLOWED_DOMAINS, *extra])

    @property
    def allowed_domains(self) -> Tuple[str, ...]:
        return tuple(self._allowed) + ((ALLOW_ALL,) if self._allow_all else ())

    @property
    def approved_domains(self) -> frozenset:
        return frozenset(self._approved)

    def domain_allowed(self, url_or_host: object) -> bool:
        kind, host = _classify(url_or_host)
        if kind == "none":
            return True
        if kind == "invalid":
            return False
        if self._allow_all:
            return True
        for domain in (*self._allowed, *self._approved):
            if host == domain or (not _is_ip(domain) and host.endswith("." + domain)):
                return True
        return False

    def approve_domain(self, host: object) -> None:
        kind, normalized = _classify(host)
        if kind == "host":
            self._approved.add(normalized)

    def evaluate(
        self,
        op: Op,
        element: Optional[Element],
        *,
        value: Optional[str] = None,
        key: Optional[str] = None,
        force_confirm: bool = False,
        llm_consequential: bool = False,
    ) -> Decision:
        pressed = key or ""
        lowered = pressed.strip().lower()
        is_enter = op == "press" and lowered in _ENTER_KEYS
        is_space = op == "press" and bool(pressed) and (not lowered or lowered in _SPACE_KEYS)
        types_char = op == "press" and len(pressed) == 1 and not is_space
        if element is not None:
            if (op == "type" or types_char or (is_space and not _is_clickable(element))) and element.has(FLAG_PASSWORD):
                return Decision("handoff", ["password_field"])
            if element.has(FLAG_CAPTCHA):
                return Decision("handoff", ["captcha"])
        reasons: List[str] = []
        if element is not None:
            if op in ("click", "select"):
                _click_reasons(element, reasons)
            if op in ("type", "select"):
                _typing_reasons(element, value, reasons)
            elif types_char or (is_space and not _is_clickable(element)):
                _typing_reasons(element, None, reasons)
            if is_space and _is_clickable(element):
                _click_reasons(element, reasons)
        elif op in ("type", "select") and value and detect_pii(value):
            _add(reasons, "pii_value")
        if is_enter:
            _enter_reasons(element, reasons)
        if force_confirm:
            _add(reasons, "macro")
        if llm_consequential:
            _add(reasons, "llm_consequential")
        return Decision("confirm", reasons) if reasons else Decision("allow", [])


def _label(element: Optional[Element]) -> str:
    if element is None:
        return "la página"
    label = " ".join((element.label or element.text or "").split())
    if not label:
        return "la página"
    return label if len(label) <= 120 else label[:119] + "…"


def describe_action_es(
    op: str,
    element: Optional[Element],
    value: Optional[str] = None,
    key: Optional[str] = None,
    host: str = "",
) -> str:
    label = _label(element)
    where = f" ({host})" if host else ""
    if op == "click":
        return f"Presionar «{label}» en {host or 'la página'}"
    if op == "type":
        return f"Escribir «{value or ''}» en «{label}»{where}"
    if op == "select":
        return f"Elegir «{value or ''}» en «{label}»{where}"
    if op == "press":
        return f"Presionar la tecla {key or ''} en «{label}»{where}"
    return f"{op} «{label}»{where}"


def title_es(reasons: Iterable[str]) -> str:
    found = set(reasons)
    if "payment" in found:
        return "Confirmar un pago"
    if found & {"submit_form", "enter_submits_form"}:
        return "Enviar un formulario"
    if found & {"pii_field", "pii_value"}:
        return "Escribir datos personales"
    return "Acción importante"
