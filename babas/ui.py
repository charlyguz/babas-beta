import argparse
import asyncio
import contextlib
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterator, List, Mapping, Optional, Sequence, TextIO, Tuple

MIN_WIDTH = 40
MAX_WIDTH = 100
DEFAULT_WIDTH = 80
MIN_CARD_INNER = 16
TAGLINE = "Su asistente para usar internet, paso a paso"
SUBTITLE = "Browser Automation Bot Assistant System"
UNICODE_PROBE = "─│╭╮╰╯✓✗●…·❯"
WINDOWS_MODERN_MARKERS = ("WT_SESSION", "TERM_PROGRAM", "ConEmuANSI", "ANSICON", "TERM")
FALSY = frozenset({"0", "false", "no", "off"})
ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004

_SGR = {
    "bold": "1",
    "dim": "2",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "magenta": "35",
    "cyan": "36",
    "bright_blue": "94",
    "bright_cyan": "96",
}

_GLYPHS = {
    "ok": ("✓", "+"),
    "fail": ("✗", "x"),
    "run": ("●", "*"),
    "pending": ("○", "o"),
    "info": ("•", "i"),
    "warn": ("▲", "!"),
    "ask": ("?", "?"),
    "hand": ("◆", "*"),
    "bot": ("◆", "*"),
    "bullet": ("•", "-"),
    "pointer": ("→", "->"),
    "prompt": ("❯", ">"),
    "ellipsis": ("…", "..."),
    "dot": ("·", "|"),
    "dash": ("—", "-"),
    "h": ("─", "-"),
    "v": ("│", "|"),
    "tl": ("╭", "+"),
    "tr": ("╮", "+"),
    "bl": ("╰", "+"),
    "br": ("╯", "+"),
}

_SPINNER = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
_SPINNER_ASCII = ("|", "/", "-", "\\")

_STEPS = {
    "running": ("run", ("cyan",)),
    "success": ("ok", ("green",)),
    "failure": ("fail", ("red",)),
    "info": ("info", ("bright_blue",)),
    "warn": ("warn", ("yellow",)),
    "pending": ("pending", ("dim",)),
}

_CARDS = {
    "info": ("info", ("bright_blue",)),
    "success": ("ok", ("green",)),
    "error": ("fail", ("red",)),
    "warn": ("warn", ("yellow",)),
    "confirm": ("warn", ("yellow",)),
    "question": ("ask", ("bright_cyan",)),
    "handoff": ("hand", ("magenta",)),
}

_WORDMARK = {
    True: {"B": ("█▄▄", "█▄█"), "A": ("▄▀█", "█▀█"), "S": ("█▀", "▄█")},
    False: {
        "B": (" ___ ", "| _ )", "| _ \\", "|___/"),
        "A": ("   _   ", "  /_\\  ", " / _ \\ ", "/_/ \\_\\"),
        "S": (" ___ ", "/ __|", "\\__ \\", "|___/"),
    },
}

_ASCII_MAP = str.maketrans({"«": '"', "»": '"', "“": '"', "”": '"', "‘": "'", "’": "'", "—": "-", "–": "-", "…": "...", "¿": "?", "¡": "!"})
_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-Z\\-_])")
_CONTROL_RE = re.compile("[\x00-\x09\x0b-\x1f\x7f-\x9f‪-‮⁦-⁩]")
_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")
_BULLET_RE = re.compile(r"^(\s*)((?:[-•*·+]|\d{1,3}[.)])\s+)?")
_ZERO_WIDTH = frozenset("​‌‍‎‏⁠﻿︎️")


@dataclass(frozen=True)
class Term:
    color: bool = False
    unicode: bool = True
    width: int = DEFAULT_WIDTH
    interactive: bool = False

    def paint(self, text: str, *styles: str) -> str:
        if not self.color or not text or not styles:
            return text
        return f"\x1b[{';'.join(_SGR[style] for style in styles)}m{text}\x1b[0m"

    def glyph(self, name: str) -> str:
        return _GLYPHS[name][0 if self.unicode else 1]


def _isatty(stream: Any) -> bool:
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def _enable_windows_ansi(stream: Any, kernel32: Any = None) -> bool:
    import ctypes

    try:
        api = kernel32 if kernel32 is not None else ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = api.GetStdHandle(-12 if stream is sys.stderr else -11)
        mode = ctypes.c_uint32()
        if not api.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        if mode.value & ENABLE_VIRTUAL_TERMINAL_PROCESSING:
            return True
        return bool(api.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING))
    except Exception:
        return False


def _ansi_ready(stream: Any, platform: str) -> bool:
    return True if platform != "win32" else _enable_windows_ansi(stream)


def supports_color(stream: Optional[TextIO] = None, env: Optional[Mapping[str, str]] = None, platform: Optional[str] = None) -> bool:
    stream = stream if stream is not None else sys.stdout
    env = os.environ if env is None else env
    platform = platform or sys.platform
    if env.get("NO_COLOR"):
        return False
    tty = _isatty(stream)
    force = (env.get("FORCE_COLOR") or "").strip().lower()
    if force:
        if force in FALSY:
            return False
        if tty:
            _ansi_ready(stream, platform)
        return True
    if (env.get("TERM") or "").lower() == "dumb" or not tty:
        return False
    return _ansi_ready(stream, platform)


def supports_unicode(stream: Optional[TextIO] = None, env: Optional[Mapping[str, str]] = None, platform: Optional[str] = None) -> bool:
    stream = stream if stream is not None else sys.stdout
    env = os.environ if env is None else env
    platform = platform or sys.platform
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        UNICODE_PROBE.encode(encoding)
    except (UnicodeError, LookupError):
        return False
    if platform == "win32" and not any(env.get(name) for name in WINDOWS_MODERN_MARKERS):
        return False
    return True


def terminal_width(stream: Optional[TextIO] = None, env: Optional[Mapping[str, str]] = None) -> int:
    stream = stream if stream is not None else sys.stdout
    env = os.environ if env is None else env
    raw = (env.get("COLUMNS") or "").strip()
    columns = int(raw) if raw.isdigit() and int(raw) > 0 else 0
    if not columns:
        try:
            columns = os.get_terminal_size(stream.fileno()).columns
        except (AttributeError, ValueError, OSError):
            columns = DEFAULT_WIDTH
    return max(MIN_WIDTH, min(MAX_WIDTH, columns or DEFAULT_WIDTH))


def detect(stream: Optional[TextIO] = None, env: Optional[Mapping[str, str]] = None, platform: Optional[str] = None) -> Term:
    stream = stream if stream is not None else sys.stdout
    env = os.environ if env is None else env
    platform = platform or sys.platform
    tty = _isatty(stream)
    dumb = (env.get("TERM") or "").lower() == "dumb"
    return Term(
        color=supports_color(stream, env, platform),
        unicode=supports_unicode(stream, env, platform),
        width=terminal_width(stream, env),
        interactive=tty and not dumb and _ansi_ready(stream, platform),
    )


def printable(text: str, stream: Optional[TextIO] = None) -> str:
    encoding = getattr(stream if stream is not None else sys.stdout, "encoding", None)
    if not encoding:
        return text
    try:
        text.encode(encoding)
        return text
    except UnicodeEncodeError:
        pass
    except LookupError:
        return text
    out: List[str] = []
    for ch in text:
        for candidate in (ch, ch.translate(_ASCII_MAP), "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))):
            try:
                candidate.encode(encoding)
            except UnicodeEncodeError:
                continue
            out.append(candidate)
            break
        else:
            out.append("?")
    return "".join(out)


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def clean(text: Any) -> str:
    value = text if isinstance(text, str) else str(text)
    value = _ANSI_RE.sub("", value).replace("\t", " ")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    return _CONTROL_RE.sub("", value)


def _one_line(text: Any) -> str:
    return " ".join(clean(text).split())


def char_width(ch: str) -> int:
    if ch in _ZERO_WIDTH:
        return 0
    code = ord(ch)
    if code < 32 or 0x7F <= code < 0xA0:
        return 0
    if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def display_width(text: str) -> int:
    return sum(char_width(ch) for ch in strip_ansi(text))


def _take(text: str, width: int) -> Tuple[str, str]:
    used = 0
    for position, ch in enumerate(text):
        cols = char_width(ch)
        if used + cols > width and position > 0:
            return text[:position], text[position:]
        used += cols
    return text, ""


def truncate(text: str, width: int, ellipsis: str = "…") -> str:
    text = _one_line(text)
    if display_width(text) <= width:
        return text
    room = width - display_width(ellipsis)
    if room <= 0:
        return _take(ellipsis, max(width, 0))[0] if width > 0 else ""
    head, _rest = _take(text, room)
    return head.rstrip() + ellipsis


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


def wrap(text: Any, width: int, *, indent: str = "", hanging: Optional[str] = None) -> List[str]:
    rest_indent = indent if hanging is None else hanging
    lines: List[str] = []
    for paragraph in clean(text).split("\n"):
        if not paragraph.strip():
            lines.append("")
            continue
        found = _BULLET_RE.match(paragraph)
        lead = found.group(1) if found else ""
        bullet = (found.group(2) or "") if found else ""
        words = paragraph[found.end() if found else 0 :].split()
        first = indent + lead + bullet
        later = rest_indent + lead + " " * display_width(bullet)
        current = first
        used = display_width(current)
        has_word = False
        for word in words:
            size = display_width(word)
            if used + (1 if has_word else 0) + size <= width:
                current += (" " if has_word else "") + word
                used += (1 if has_word else 0) + size
                has_word = True
                continue
            if has_word:
                lines.append(current)
                current, used, has_word = later, display_width(later), False
            while display_width(word) > max(1, width - used):
                head, word = _take(word, max(1, width - used))
                lines.append(current + head)
                current, used = later, display_width(later)
            current += word
            used += display_width(word)
            has_word = True
        lines.append(current if has_word else current.rstrip())
    return lines


def _span(term: Term, width: Optional[int] = None) -> int:
    return width if width else max(MIN_WIDTH, term.width) - 1


def rule(term: Term, title: str = "", *, width: Optional[int] = None) -> str:
    total = _span(term, width)
    dash = term.glyph("h")
    if not title:
        return term.paint(dash * total, "dim")
    label = f" {truncate(title, max(total - 8, 4), term.glyph('ellipsis'))} "
    fill = max(2, total - 2 - display_width(label))
    return term.paint(dash * 2, "dim") + term.paint(label, "bold") + term.paint(dash * fill, "dim")


def prompt(term: Term, label: str = "") -> str:
    chevron = term.paint(term.glyph("prompt"), "bold", "bright_cyan")
    return f"{term.paint(_one_line(label), 'dim')} {chevron} " if label else f"{chevron} "


def question(term: Term, text: str) -> str:
    return f"{term.paint(term.glyph('prompt'), 'bold', 'bright_cyan')} {term.paint(clean(text), 'bold')}"


def for_input(text: str) -> str:
    if "readline" not in sys.modules or "\x1b" not in text:
        return text
    return _SGR_RE.sub(lambda found: f"\x01{found.group(0)}\x02", text)


def muted(term: Term, text: str) -> str:
    return term.paint(clean(text), "dim")


def step(
    term: Term,
    status: str,
    text: Any,
    *,
    detail: str = "",
    indent: int = 2,
    wrap_text: bool = True,
    width: Optional[int] = None,
) -> str:
    if status not in _STEPS:
        raise ValueError(f"unknown step status: {status}")
    glyph, styles = _STEPS[status]
    icon = term.paint(term.glyph(glyph), *styles)
    pad_left = " " * indent
    if wrap_text:
        lines = wrap(text, max(8, _span(term, width) - indent - 2))
    else:
        lines = clean(text).split("\n")
    body = ("dim",) if status == "pending" else ()
    lines = [term.paint(line, *body) for line in lines] or [""]
    out = [f"{pad_left}{icon} {lines[0]}"] + [f"{pad_left}  {line}" for line in lines[1:]]
    if detail:
        out[-1] += "  " + term.paint(_one_line(detail), "dim")
    return "\n".join(out)


def speech(term: Term, text: Any, *, name: str = "BABAS", indent: int = 2, width: Optional[int] = None) -> str:
    icon = term.paint(term.glyph("bot"), "bright_cyan")
    label = term.paint(f"{name}:", "bold", "bright_cyan")
    lead = display_width(f"{name}: ") + 2
    lines = wrap(text, max(8, _span(term, width) - indent - lead)) or [""]
    pad_left = " " * indent
    out = [f"{pad_left}{icon} {label} {lines[0]}"]
    out.extend(f"{pad_left}{' ' * lead}{line}" for line in lines[1:])
    return "\n".join(out)


def note(term: Term, text: str) -> str:
    body = clean(text)
    if body.startswith("Note:"):
        return term.paint("Note:", "bold", "yellow") + body[len("Note:") :]
    return body


def info_row(term: Term, pairs: Sequence[Tuple[str, str]], *, indent: int = 2, width: Optional[int] = None) -> str:
    separator = f" {term.glyph('dot')} "
    room = _span(term, width) - indent
    lines: List[str] = []
    plain = ""
    painted = ""
    for label, value in pairs:
        text = f"{_one_line(label)} {_one_line(value)}"
        shown = f"{term.paint(_one_line(label), 'dim')} {term.paint(_one_line(value), 'bold')}"
        if plain and display_width(plain) + display_width(separator) + display_width(text) > room:
            lines.append(painted)
            plain = painted = ""
        if plain:
            plain += separator + text
            painted += term.paint(separator, "dim") + shown
        else:
            plain, painted = text, shown
    if plain:
        lines.append(painted)
    return "\n".join(" " * indent + line for line in lines)


def _wordmark(term: Term) -> List[str]:
    letters = _WORDMARK[term.unicode]
    rows = len(letters["B"])
    gap = " "
    return [gap.join(letters[ch][row] for ch in "BABAS") for row in range(rows)]


def banner(
    term: Term,
    info: Sequence[Tuple[str, str]] = (),
    *,
    tagline: str = TAGLINE,
    subtitle: str = SUBTITLE,
    version: str = "",
    indent: int = 2,
) -> str:
    mark = _wordmark(term)
    second = subtitle + (f"  v{version}" if version else "")
    mark_width = max(display_width(row) for row in mark)
    side_by_side = term.unicode and indent + mark_width + 3 + max(display_width(tagline), display_width(second)) <= _span(term)
    pad_left = " " * indent
    out: List[str] = []
    if side_by_side:
        out.append(f"{pad_left}{term.paint(pad(mark[0], mark_width), 'bold', 'bright_cyan')}   {term.paint(tagline, 'bold')}")
        out.append(f"{pad_left}{term.paint(pad(mark[1], mark_width), 'bold', 'bright_cyan')}   {term.paint(second, 'dim')}")
    else:
        out.extend(f"{pad_left}{term.paint(row, 'bold', 'bright_cyan')}" for row in mark)
        out.append("")
        room = max(8, _span(term) - indent)
        out.extend(f"{pad_left}{term.paint(line, 'bold')}" for line in wrap(tagline, room))
        out.extend(f"{pad_left}{term.paint(line, 'dim')}" for line in wrap(second, room))
    if info:
        out.append("")
        out.append(info_row(term, info, indent=indent))
    return "\n".join(out)


def card(
    term: Term,
    title: str,
    lines: Sequence[Any] = (),
    *,
    kind: str = "info",
    width: Optional[int] = None,
) -> str:
    if kind not in _CARDS:
        raise ValueError(f"unknown card kind: {kind}")
    glyph, styles = _CARDS[kind]
    total = max(MIN_CARD_INNER + 4, _span(term, width))
    inner = total - 4
    v = term.paint(term.glyph("v"), *styles)
    dash = term.glyph("h")
    head = f" {term.glyph(glyph)} {truncate(title, inner - 4, term.glyph('ellipsis'))} "
    fill = max(1, total - 3 - display_width(head))
    top = (
        term.paint(term.glyph("tl") + dash, *styles)
        + term.paint(head, "bold", *styles)
        + term.paint(dash * fill + term.glyph("tr"), *styles)
    )
    rows: List[str] = []
    for line in lines:
        rows.extend(wrap(line, inner))
    if not rows:
        rows = [""]
    body = [f"{v} {pad(row, inner)} {v}" for row in rows]
    bottom = term.paint(term.glyph("bl") + dash * (total - 2) + term.glyph("br"), *styles)
    return "\n".join([top, *body, bottom])


def answer_panel(term: Term, text: str, *, title: str = "Respuesta", width: Optional[int] = None) -> str:
    return card(term, title, [text], kind="success", width=width)


def error_panel(term: Term, message: str, *, title: str = "Error", hint: str = "", width: Optional[int] = None) -> str:
    lines = [message] + (["", hint] if hint else [])
    return card(term, title, lines, kind="error", width=width)


def table(
    term: Term,
    headers: Sequence[str],
    rows: Sequence[Sequence[Any]],
    *,
    indent: int = 2,
    gap: int = 2,
    width: Optional[int] = None,
    styles: Sequence[Sequence[str]] = (),
) -> str:
    count = len(headers)
    heads = [_one_line(header) for header in headers]
    body = [[_one_line(row[index]) if index < len(row) else "" for index in range(count)] for row in rows]
    sizes = [max([display_width(heads[i])] + [display_width(row[i]) for row in body]) for i in range(count)]
    room = _span(term, width) - indent - gap * (count - 1)
    floor = 6
    while sum(sizes) > room and max(sizes) > floor:
        sizes[sizes.index(max(sizes))] -= 1
    ellipsis = term.glyph("ellipsis")
    dash = term.glyph("h")

    def line(cells: Sequence[str], fixed: Sequence[str] = ()) -> str:
        parts = []
        for i, cell in enumerate(cells):
            shown = truncate(cell, sizes[i], ellipsis)
            styled = term.paint(shown, *(fixed or (styles[i] if i < len(styles) else ())))
            parts.append(styled + " " * (sizes[i] - display_width(shown)))
        return (" " * indent + (" " * gap).join(parts)).rstrip()

    out = [line(heads, ("bold",))]
    out.append(" " * indent + term.paint((" " * gap).join(dash * size for size in sizes), "dim"))
    out.extend(line(row) for row in body)
    return "\n".join(out)


_active: Optional["Status"] = None


def emit(text: str = "", *, stream: Optional[TextIO] = None) -> None:
    out = stream if stream is not None else sys.stdout
    active = _active
    if active is not None and active.stream is out:
        active.write(text)
        return
    print(printable(text, out), file=out, flush=True)


@contextlib.contextmanager
def suspended() -> Iterator[None]:
    active = _active
    if active is None:
        yield
    else:
        with active.suspend():
            yield


class Status:
    def __init__(
        self,
        term: Term,
        label: str = "Thinking",
        *,
        hint: str = "",
        stream: Optional[TextIO] = None,
        interval: float = 0.1,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.term = term
        self.label = label
        self.hint = hint
        self.stream = stream if stream is not None else sys.stdout
        self.interval = interval
        self._clock = clock
        self._task: Optional["asyncio.Task[None]"] = None
        self._started = 0.0
        self._tick = 0
        self._paused = 0
        self._broken = False

    @property
    def running(self) -> bool:
        return self._task is not None

    def start(self) -> bool:
        global _active
        if self._task is not None:
            return True
        if not self.term.interactive:
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        self._started = self._clock()
        self._tick = 0
        self._broken = False
        self._task = loop.create_task(self._run())
        _active = self
        return True

    async def stop(self) -> None:
        global _active
        task, self._task = self._task, None
        if _active is self:
            _active = None
        if task is None:
            return
        task.cancel()
        self._erase()
        await asyncio.wait({task})

    async def __aenter__(self) -> "Status":
        self.start()
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.stop()

    async def _run(self) -> None:
        while True:
            self._draw()
            await asyncio.sleep(self.interval)
            self._tick += 1

    def frame(self) -> str:
        glyphs = _SPINNER if self.term.unicode else _SPINNER_ASCII
        mark = self.term.paint(glyphs[self._tick % len(glyphs)], "cyan")
        ellipsis = self.term.glyph("ellipsis")
        elapsed = int(self._clock() - self._started) if self._task is not None else 0
        text = f"{_one_line(self.label)}{ellipsis}" + (f" {elapsed}s" if elapsed >= 2 else "")
        room = max(MIN_WIDTH, self.term.width) - 3
        hint = _one_line(self.hint)
        if hint and display_width(text) + 2 + display_width(hint) > room:
            hint = ""
        text = truncate(text, room, ellipsis)
        return f"{mark} {text}" + (f"  {self.term.paint(hint, 'dim')}" if hint else "")

    def _out(self, data: str) -> None:
        if self._broken:
            return
        try:
            self.stream.write(printable(data, self.stream))
            self.stream.flush()
        except (OSError, ValueError):
            self._broken = True

    def _erase(self) -> None:
        self._out("\r\x1b[2K")

    def _draw(self) -> None:
        if self._task is None or self._paused:
            return
        self._out("\r\x1b[2K" + self.frame() + "\r")

    def write(self, text: str) -> None:
        self._erase()
        self._out(text + "\n")
        self._draw()

    @contextlib.contextmanager
    def suspend(self) -> Iterator[None]:
        self._paused += 1
        self._erase()
        try:
            yield
        finally:
            self._paused -= 1
            self._draw()


def _demo_lines(term: Term) -> List[str]:
    out: List[str] = []
    add = out.append
    bullet = term.glyph("bullet")
    add(banner(term, [("mode", "text"), ("browser", "chrome"), ("model", "gpt-5.2"), ("lang", "es"), ("safety", "on")], version="0.2.0"))
    add("")
    add(rule(term, "Steps"))
    add(step(term, "running", "Opening the browser"))
    add(step(term, "success", 'goto https://www.google.com.mx', detail="1.2 s"))
    add(step(term, "failure", 'click [7] "Buscar" (the element is covered by another one)'))
    add(step(term, "info", "Connected to Chrome on port 9222"))
    add(step(term, "warn", "The page is taking longer than usual"))
    add(step(term, "pending", "Step 2/3: type_text [3] \"recetas de mole\""))
    add(speech(term, "Voy a buscar recetas de mole en Google y le muestro los primeros resultados."))
    add("")
    add(rule(term, "Cards"))
    add(
        card(
            term,
            "Escribir datos personales",
            [f"{bullet} Escribir «GOMC800101HDFRRN09» en «CURP» (gob.mx)", "", "BABAS dice: necesito su CURP para consultar su acta."],
            kind="confirm",
        )
    )
    add(card(term, "Necesito su ayuda", ["La página pide comprobar que usted es una persona. Resuélvalo en la página y luego presione «Listo»."], kind="handoff"))
    add(card(term, "BABAS pregunta", ["¿En qué estado nació? Escríbalo tal como aparece en su acta."], kind="question"))
    add(card(term, "Permiso para un sitio", ["BABAS quiere usar el sitio imss.gob.mx."], kind="warn"))
    add(answer_panel(term, "Encontré tres recetas de mole poblano. La primera es de Cocina Fácil y lleva chile ancho, pasilla y mulato."))
    add(error_panel(term, "No pude completar la tarea. Puede intentarlo de nuevo o pedirlo de otra manera.", hint="Escriba «ayuda» para ver los comandos."))
    add("")
    add(rule(term, "Tables"))
    add(table(term, ["", "Tab", "Title"], [[term.glyph("pointer"), "main", "Google"], ["", "correo", "Bandeja de entrada (3) - Gmail"], ["", "sat", "Servicio de Administración Tributaria 日本語"]], styles=[("cyan",), ("bold",), ()]))
    add("")
    add(table(term, ["Command", "What it does"], [["exit, quit, q", "Close the program"], ["help, ?", "Show this help"], ["tabs", "Show the open tabs"]], styles=[("bold", "bright_cyan"), ()]))
    add("")
    add(rule(term))
    add(question(term, "Continue? (y/N): ").rstrip())
    add(prompt(term).rstrip() + " open youtube and search lofi music")
    return out


def demo(term: Term, *, stream: Optional[TextIO] = None) -> None:
    out = stream if stream is not None else sys.stdout
    for text in _demo_lines(term):
        print(text, file=out)
    print("", file=out)
    print(rule(term, "Thinking indicator"), file=out)
    if term.interactive:
        asyncio.run(_demo_status(term))
    else:
        print("  " + Status(term, "Thinking", hint="Ctrl+C to stop").frame(), file=out)
        note_lines = wrap("(shown only in a terminal: one line that disappears when the work ends)", _span(term) - 2)
        print("\n".join("  " + muted(term, line) for line in note_lines), file=out)
    out.flush()


async def _demo_status(term: Term) -> None:
    async with Status(term, "Thinking", hint="Ctrl+C to stop", interval=0.08):
        await asyncio.sleep(1.6)
    emit(step(term, "success", "Done", detail="3 steps in 1.6 s"))


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m babas.ui", description="Preview of the BABAS terminal components")
    parser.add_argument("--ascii", action="store_true", help="force the ASCII fallback")
    parser.add_argument("--width", type=int, default=None, help="force the terminal width")
    args = parser.parse_args(argv)
    term = detect(sys.stdout)
    if args.ascii:
        term = replace(term, unicode=False)
    if args.width:
        term = replace(term, width=max(MIN_WIDTH, min(MAX_WIDTH, args.width)))
    demo(term)


if __name__ == "__main__":
    main()
