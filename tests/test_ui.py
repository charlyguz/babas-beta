import asyncio
import io
import os
import signal
import sys
import types
from typing import Any, List, Optional

import pytest
from helpers import FakeDriver, FakeLLM, make_element, make_state

from babas import cli, ui
from babas.__main__ import main as babas_main
from babas.channels import ConfirmRequest, ConsoleChannel
from babas.config import Settings
from babas.devrepl import _format_element
from babas.devrepl import _Session as ReplSession
from babas.safety import SafetyPolicy

CURP = "GOMA850101HDFRRN09"
MASKED_CURP = "GO**************09"
PLAIN = ui.Term(color=False, unicode=True, width=60)
COLOR = ui.Term(color=True, unicode=True, width=60)
ASCII = ui.Term(color=False, unicode=False, width=60)
TERMS = [PLAIN, COLOR, ASCII]
BOX_CHARS = "─│╭╮╰╯✓✗●○•▲◆→❯…·—"


class FakeStream(io.StringIO):
    def __init__(self, tty: bool = False, encoding: Optional[str] = "utf-8") -> None:
        super().__init__()
        self._tty = tty
        self._encoding = encoding

    def isatty(self) -> bool:
        return self._tty

    @property
    def encoding(self) -> Optional[str]:  # type: ignore[override]
        return self._encoding


class BrokenStream(FakeStream):
    def write(self, text: str) -> int:
        raise OSError("closed")


def width_of(line: str) -> int:
    return ui.display_width(ui.strip_ansi(line))


class TestColorDetection:
    def test_a_tty_gets_color_and_a_pipe_does_not(self):
        assert ui.supports_color(FakeStream(tty=True), {}) is True
        assert ui.supports_color(FakeStream(tty=False), {}) is False

    def test_no_color_wins_over_everything_when_not_empty(self):
        tty = FakeStream(tty=True)
        assert ui.supports_color(tty, {"NO_COLOR": "1"}) is False
        assert ui.supports_color(tty, {"NO_COLOR": "anything"}) is False
        assert ui.supports_color(tty, {"NO_COLOR": ""}) is True
        assert ui.supports_color(tty, {"NO_COLOR": "1", "FORCE_COLOR": "1"}) is False

    def test_force_color_turns_color_on_for_pipes_and_off_for_zero(self):
        assert ui.supports_color(FakeStream(tty=False), {"FORCE_COLOR": "1"}) is True
        assert ui.supports_color(FakeStream(tty=False), {"FORCE_COLOR": "true"}) is True
        assert ui.supports_color(FakeStream(tty=True), {"FORCE_COLOR": "0"}) is False
        assert ui.supports_color(FakeStream(tty=True), {"FORCE_COLOR": "false"}) is False
        assert ui.supports_color(FakeStream(tty=False), {"FORCE_COLOR": ""}) is False

    def test_a_dumb_terminal_has_no_color_unless_forced(self):
        tty = FakeStream(tty=True)
        assert ui.supports_color(tty, {"TERM": "dumb"}) is False
        assert ui.supports_color(tty, {"TERM": "DUMB"}) is False
        assert ui.supports_color(tty, {"TERM": "dumb", "FORCE_COLOR": "1"}) is True

    def test_a_stream_that_cannot_say_if_it_is_a_tty_has_no_color(self):
        class Odd:
            def isatty(self) -> bool:
                raise ValueError("closed file")

        assert ui.supports_color(Odd(), {}) is False  # type: ignore[arg-type]

    def test_windows_needs_virtual_terminal_processing(self, monkeypatch):
        calls: List[Any] = []

        def fake_enable(stream: Any, kernel32: Any = None) -> bool:
            calls.append(stream)
            return len(calls) > 1

        monkeypatch.setattr(ui, "_enable_windows_ansi", fake_enable)
        tty = FakeStream(tty=True)
        assert ui.supports_color(tty, {}, "win32") is False
        assert ui.supports_color(tty, {}, "win32") is True
        assert ui.supports_color(tty, {}, "linux") is True
        assert len(calls) == 2


class FakeKernel32:
    def __init__(self, mode: int = 3, get_ok: bool = True, set_ok: bool = True) -> None:
        self.mode = mode
        self.get_ok = get_ok
        self.set_ok = set_ok
        self.handle_args: List[int] = []
        self.set_calls: List[int] = []

    def GetStdHandle(self, which: int) -> int:
        self.handle_args.append(which)
        return 7

    def GetConsoleMode(self, handle: int, ref: Any) -> int:
        if not self.get_ok:
            return 0
        ref._obj.value = self.mode
        return 1

    def SetConsoleMode(self, handle: int, mode: int) -> int:
        self.set_calls.append(mode)
        return 1 if self.set_ok else 0


class TestWindowsAnsi:
    def test_sets_the_virtual_terminal_flag(self):
        kernel = FakeKernel32(mode=3)
        assert ui._enable_windows_ansi(sys.stdout, kernel) is True
        assert kernel.set_calls == [3 | ui.ENABLE_VIRTUAL_TERMINAL_PROCESSING]
        assert kernel.handle_args == [-11]

    def test_uses_the_stderr_handle_for_stderr(self):
        kernel = FakeKernel32()
        ui._enable_windows_ansi(sys.stderr, kernel)
        assert kernel.handle_args == [-12]

    def test_already_enabled_is_not_set_again(self):
        kernel = FakeKernel32(mode=ui.ENABLE_VIRTUAL_TERMINAL_PROCESSING)
        assert ui._enable_windows_ansi(sys.stdout, kernel) is True
        assert kernel.set_calls == []

    def test_failures_mean_no_ansi(self):
        assert ui._enable_windows_ansi(sys.stdout, FakeKernel32(get_ok=False)) is False
        assert ui._enable_windows_ansi(sys.stdout, FakeKernel32(set_ok=False)) is False
        assert ui._enable_windows_ansi(sys.stdout, object()) is False


class TestUnicodeDetection:
    def test_utf8_streams_get_box_drawing(self):
        assert ui.supports_unicode(FakeStream(encoding="utf-8"), {}, "linux") is True
        assert ui.supports_unicode(FakeStream(encoding=None), {}, "linux") is True

    @pytest.mark.parametrize("encoding", ["ascii", "cp437", "cp1252", "latin-1"])
    def test_legacy_encodings_fall_back_to_ascii(self, encoding):
        assert ui.supports_unicode(FakeStream(encoding=encoding), {}, "linux") is False

    def test_an_unknown_encoding_falls_back_to_ascii(self):
        assert ui.supports_unicode(FakeStream(encoding="no-such-codec"), {}, "linux") is False

    def test_old_windows_consoles_use_ascii_but_modern_terminals_do_not(self):
        stream = FakeStream()
        assert ui.supports_unicode(stream, {}, "win32") is False
        assert ui.supports_unicode(stream, {"WT_SESSION": "abc"}, "win32") is True
        assert ui.supports_unicode(stream, {"TERM_PROGRAM": "vscode"}, "win32") is True


class TestWidth:
    def test_columns_variable_is_clamped(self):
        stream = FakeStream()
        assert ui.terminal_width(stream, {"COLUMNS": "120"}) == ui.MAX_WIDTH
        assert ui.terminal_width(stream, {"COLUMNS": "10"}) == ui.MIN_WIDTH
        assert ui.terminal_width(stream, {"COLUMNS": "72"}) == 72

    def test_defaults_when_it_cannot_be_measured(self):
        assert ui.terminal_width(FakeStream(), {}) == ui.DEFAULT_WIDTH
        assert ui.terminal_width(FakeStream(), {"COLUMNS": "abc"}) == ui.DEFAULT_WIDTH
        assert ui.terminal_width(FakeStream(), {"COLUMNS": "0"}) == ui.DEFAULT_WIDTH

    def test_asks_the_terminal_when_columns_is_unset(self, monkeypatch):
        class WithFd(FakeStream):
            def fileno(self) -> int:
                return 5

        monkeypatch.setattr(os, "get_terminal_size", lambda fd: os.terminal_size((90, 30)) if fd == 5 else None)
        assert ui.terminal_width(WithFd(), {}) == 90
        monkeypatch.setattr(os, "get_terminal_size", lambda fd: os.terminal_size((500, 30)))
        assert ui.terminal_width(WithFd(), {}) == ui.MAX_WIDTH


class TestDetect:
    def test_pipe(self):
        term = ui.detect(FakeStream(tty=False), {}, "linux")
        assert (term.color, term.unicode, term.interactive, term.width) == (False, True, False, 80)

    def test_tty(self):
        term = ui.detect(FakeStream(tty=True), {"COLUMNS": "70"}, "linux")
        assert (term.color, term.unicode, term.interactive, term.width) == (True, True, True, 70)

    def test_dumb_tty_is_not_interactive(self):
        term = ui.detect(FakeStream(tty=True), {"TERM": "dumb"}, "linux")
        assert (term.color, term.interactive) == (False, False)

    def test_no_color_keeps_the_interactive_indicator(self):
        term = ui.detect(FakeStream(tty=True), {"NO_COLOR": "1"}, "linux")
        assert (term.color, term.interactive) == (False, True)

    def test_forced_color_on_a_pipe_is_not_interactive(self):
        term = ui.detect(FakeStream(tty=False), {"FORCE_COLOR": "1"}, "linux")
        assert (term.color, term.interactive) == (True, False)

    def test_ascii_stream(self):
        assert ui.detect(FakeStream(encoding="ascii"), {}, "linux").unicode is False


class TestText:
    def test_clean_removes_escape_sequences_and_control_characters(self):
        assert ui.clean("a\x1b[31mred\x1b[0m\x07b\x1b]0;title\x07c") == "aredbc"
        assert ui.clean("a\x1b[2Jb\x1b[?25lc") == "abc"
        assert ui.clean("x\x00y\x7fz\x85") == "xyz"
        assert ui.clean("a\tb\r\nc\rd") == "a b\nc\nd"
        assert ui.clean("evil‮txt") == "eviltxt"
        assert ui.clean(None) == "None"

    def test_widths(self):
        assert ui.display_width("hola") == 4
        assert ui.display_width("canción rápida") == 14
        assert ui.display_width("é") == 1
        assert ui.display_width("日本語") == 6
        assert ui.display_width("🙂") == 2
        assert ui.display_width("a‍b") == 2
        assert ui.display_width("\x1b[1mhola\x1b[0m") == 4

    def test_wrap_fills_lines_greedily(self):
        assert ui.wrap("hola mundo cruel", 10) == ["hola mundo", "cruel"]
        assert ui.wrap("   mucho    espacio  ", 40) == ["   mucho espacio"]

    def test_wrap_counts_accents_as_one_column(self):
        lines = ui.wrap("Resuélvalo en la página y luego presione «Listo» cuando termine", 20)
        assert all(ui.display_width(line) <= 20 for line in lines)
        assert " ".join(lines) == "Resuélvalo en la página y luego presione «Listo» cuando termine"

    def test_wrap_counts_wide_characters_as_two_columns(self):
        assert ui.wrap("日本語日本語日本語", 6) == ["日本語", "日本語", "日本語"]
        lines = ui.wrap("東京 es la capital de Japón 日本", 9)
        assert all(ui.display_width(line) <= 9 for line in lines)

    def test_wrap_splits_a_word_longer_than_the_line(self):
        assert ui.wrap("a" * 25, 10) == ["a" * 10, "a" * 10, "a" * 5]
        assert ui.wrap("x https://example.com/a/very/long/path y", 12) == [
            "x",
            "https://exam",
            "ple.com/a/ve",
            "ry/long/path",
            "y",
        ]

    def test_wrap_keeps_blank_lines_and_hangs_bullets(self):
        assert ui.wrap("a\n\nb", 10) == ["a", "", "b"]
        assert ui.wrap("- uno dos tres cuatro", 12) == ["- uno dos", "  tres", "  cuatro"]
        assert ui.wrap("• uno dos tres cuatro", 12) == ["• uno dos", "  tres", "  cuatro"]
        assert ui.wrap("12. uno dos tres", 10) == ["12. uno", "    dos", "    tres"]

    def test_wrap_with_indents(self):
        assert ui.wrap("uno dos tres", 8, indent="> ", hanging="  ") == ["> uno", "  dos", "  tres"]

    def test_wrap_makes_progress_with_tiny_widths(self):
        assert "".join(ui.wrap("abc", 1)) == "abc"
        assert "".join(ui.wrap("日本", 1)) == "日本"

    def test_wrap_sanitizes_untrusted_text(self):
        assert ui.wrap("hola\x1b[2J mundo", 40) == ["hola mundo"]

    def test_truncate(self):
        assert ui.truncate("hola", 10) == "hola"
        assert ui.truncate("hola mundo cruel", 8) == "hola mu…"
        assert ui.truncate("hola mundo cruel", 8, "...") == "hola..."
        assert ui.truncate("日本語日本語", 5) == "日本…"
        assert ui.display_width(ui.truncate("canción rápida y larga", 10)) <= 10

    def test_pad_uses_display_width(self):
        assert ui.pad("日本", 6) == "日本  "
        assert ui.pad("canción", 9) == "canción  "

    def test_printable_transliterates_for_ascii_streams(self):
        ascii_stream = FakeStream(encoding="ascii")
        assert ui.printable("Resuélvalo «Listo» ¿sí? mañana…", ascii_stream) == 'Resuelvalo "Listo" ?si? manana...'
        assert ui.printable("日本", ascii_stream) == "??"
        assert ui.printable("canción", FakeStream(encoding="utf-8")) == "canción"
        assert ui.printable("canción", FakeStream(encoding="cp1252")) == "canción"
        assert ui.printable("canción", FakeStream(encoding=None)) == "canción"

    def test_emit_survives_an_ascii_stream(self):
        stream = FakeStream(encoding="ascii")
        ui.emit("Acción «lista»", stream=stream)
        assert stream.getvalue() == 'Accion "lista"\n'


class TestPaint:
    def test_color_off_returns_the_text(self):
        assert PLAIN.paint("x", "bold", "red") == "x"

    def test_color_on_wraps_in_sgr(self):
        assert COLOR.paint("x", "bold", "green") == "\x1b[1;32mx\x1b[0m"
        assert COLOR.paint("", "bold") == ""
        assert COLOR.paint("x") == "x"

    def test_glyphs_have_an_ascii_fallback(self):
        assert PLAIN.glyph("ok") == "✓" and ASCII.glyph("ok") == "+"
        assert all(ASCII.glyph(name).isascii() for name in ui._GLYPHS)


class TestSteps:
    @pytest.mark.parametrize(
        "status, unicode_icon, ascii_icon",
        [("running", "●", "*"), ("success", "✓", "+"), ("failure", "✗", "x"), ("info", "•", "i"), ("warn", "▲", "!"), ("pending", "○", "o")],
    )
    def test_icons(self, status, unicode_icon, ascii_icon):
        assert ui.step(PLAIN, status, "hola") == f"  {unicode_icon} hola"
        assert ui.step(ASCII, status, "hola") == f"  {ascii_icon} hola"

    def test_color_only_adds_escape_codes(self):
        colored = ui.step(COLOR, "success", "hecho", detail="1 s")
        assert "\x1b[32m" in colored
        assert ui.strip_ansi(colored) == ui.step(PLAIN, "success", "hecho", detail="1 s") == "  ✓ hecho  1 s"

    def test_long_text_wraps_with_a_hanging_indent(self):
        text = "palabra " * 20
        lines = ui.step(PLAIN, "info", text).split("\n")
        assert len(lines) > 1
        assert lines[0].startswith("  • palabra")
        assert all(line.startswith("    ") for line in lines[1:])
        assert all(width_of(line) <= PLAIN.width - 1 for line in lines)

    def test_no_wrap_keeps_the_text_on_one_line(self):
        text = "x" * 200
        assert ui.step(PLAIN, "info", text, wrap_text=False) == f"  • {text}"

    def test_detail_goes_after_the_last_line(self):
        out = ui.step(PLAIN, "success", "palabra " * 20, detail="2 s")
        assert out.endswith("palabra  2 s")

    def test_unknown_status_is_refused(self):
        with pytest.raises(ValueError):
            ui.step(PLAIN, "weird", "x")

    def test_untrusted_text_cannot_inject_escape_codes(self):
        out = ui.step(PLAIN, "info", "titulo\x1b[2J\x1b]0;pwned\x07 listo")
        assert "\x1b" not in out and "\x07" not in out
        assert out == "  • titulo listo"

    def test_pending_text_is_dim(self):
        assert "\x1b[2mStep 1/2\x1b[0m" in ui.step(COLOR, "pending", "Step 1/2")

    def test_speech_hangs_under_the_text(self):
        lines = ui.speech(PLAIN, "una frase larga que ocupa mas de una linea de la terminal estrecha " * 2).split("\n")
        assert lines[0].startswith("  ◆ BABAS: una frase")
        assert all(line.startswith(" " * 11) for line in lines[1:])

    def test_note_paints_only_the_prefix(self):
        assert ui.note(PLAIN, "Note: algo") == "Note: algo"
        assert ui.note(COLOR, "Note: algo") == "\x1b[1;33mNote:\x1b[0m algo"
        assert ui.note(COLOR, "otra cosa") == "otra cosa"


class TestRuleAndPrompt:
    def test_rule_fills_the_width(self):
        line = ui.rule(PLAIN, "Browser")
        assert line.startswith("── Browser ─")
        assert width_of(line) == PLAIN.width - 1
        assert ui.rule(ASCII, "Browser").startswith("-- Browser -")
        assert width_of(ui.rule(PLAIN)) == PLAIN.width - 1
        assert width_of(ui.rule(PLAIN, "x" * 200)) <= PLAIN.width - 1

    def test_prompts(self):
        assert ui.prompt(PLAIN) == "❯ "
        assert ui.prompt(ASCII) == "> "
        assert ui.prompt(PLAIN, "babas") == "babas ❯ "
        assert ui.question(PLAIN, "Continue? (y/N): ") == "❯ Continue? (y/N): "
        assert ui.strip_ansi(ui.prompt(COLOR)) == "❯ "
        assert "\x1b[" in ui.prompt(COLOR)

    def test_readline_markers_only_when_readline_is_loaded(self, monkeypatch):
        colored = ui.prompt(COLOR)
        monkeypatch.delitem(sys.modules, "readline", raising=False)
        assert ui.for_input(colored) == colored
        monkeypatch.setitem(sys.modules, "readline", types.ModuleType("readline"))
        marked = ui.for_input(colored)
        assert "\x01\x1b[1;96m\x02" in marked and "\x01\x1b[0m\x02" in marked
        assert ui.strip_ansi(marked.replace("\x01", "").replace("\x02", "")) == "❯ "
        assert ui.for_input("plain") == "plain"


class TestBanner:
    INFO = [("mode", "text"), ("browser", "chrome"), ("model", "gpt-5.2"), ("lang", "es"), ("safety", "on")]

    def test_wide_terminals_put_the_tagline_beside_the_wordmark(self):
        out = ui.banner(ui.Term(width=80), self.INFO, version="0.2.0")
        lines = out.split("\n")
        assert lines[0].startswith("  █▄▄ ▄▀█ █▄▄ ▄▀█ █▀")
        assert ui.TAGLINE in lines[0]
        assert lines[1].startswith("  █▄█ █▀█ █▄█ █▀█ ▄█") and ui.SUBTITLE in lines[1] and "v0.2.0" in lines[1]
        assert "mode text · browser chrome · model gpt-5.2 · lang es · safety on" in out

    def test_narrow_terminals_stack_it(self):
        lines = ui.banner(ui.Term(width=40), self.INFO).split("\n")
        assert ui.TAGLINE not in lines[0]
        assert ui.TAGLINE in " ".join(line.strip() for line in lines)
        assert all(width_of(line) <= 39 for line in lines)

    def test_ascii_fallback_has_no_box_characters(self):
        out = ui.banner(ASCII, self.INFO, version="1")
        assert "|___/" in out
        assert not any(ch in out for ch in BOX_CHARS + "█▄▀")
        assert "mode text | browser chrome" in out
        assert all(width_of(line) <= ASCII.width - 1 for line in out.split("\n"))

    def test_color_does_not_change_the_text(self):
        assert ui.strip_ansi(ui.banner(COLOR, self.INFO, version="1")) == ui.banner(PLAIN, self.INFO, version="1")

    def test_info_row_wraps_between_pairs(self):
        out = ui.info_row(ui.Term(width=40), self.INFO)
        lines = out.split("\n")
        assert len(lines) > 1
        assert all(width_of(line) <= 39 for line in lines)
        assert " ".join(line.strip() for line in lines).replace(" ·", "").count("safety on") == 1
        assert not any(line.rstrip().endswith("·") for line in lines)


class TestCards:
    @pytest.mark.parametrize("term", TERMS)
    @pytest.mark.parametrize("kind", ["info", "success", "error", "warn", "confirm", "question", "handoff"])
    def test_every_line_has_the_same_width(self, term, kind):
        out = ui.card(term, "Escribir datos personales", ["• Escribir «CURP» en el campo", "", "BABAS dice: " + "palabra " * 30], kind=kind)
        lines = out.split("\n")
        assert {width_of(line) for line in lines} == {term.width - 1}
        top, bottom = ui.strip_ansi(lines[0]), ui.strip_ansi(lines[-1])
        assert (top[0], top[-1], bottom[0], bottom[-1]) == (("╭", "╮", "╰", "╯") if term.unicode else ("+",) * 4)

    def test_shape_unicode(self):
        out = ui.card(PLAIN, "Hola", ["uno", "dos"], kind="info", width=20)
        assert out.split("\n") == [
            "╭─ • Hola ─────────╮",
            "│ uno              │",
            "│ dos              │",
            "╰──────────────────╯",
        ]

    def test_shape_ascii(self):
        out = ui.card(ASCII, "Hola", ["uno"], kind="question", width=20)
        assert out.split("\n") == [
            "+- ? Hola ---------+",
            "| uno              |",
            "+------------------+",
        ]
        assert out.isascii()

    def test_accents_and_wide_characters_keep_the_border_aligned(self):
        out = ui.card(PLAIN, "Dirección", ["canción rápida ñandú", "日本語 日本語 日本語 日本語 日本語 日本語 日本語"], width=24)
        assert {width_of(line) for line in out.split("\n")} == {24}

    def test_long_titles_are_truncated(self):
        out = ui.card(PLAIN, "T" * 200, ["x"])
        assert {width_of(line) for line in out.split("\n")} == {PLAIN.width - 1}
        assert "…" in out.split("\n")[0]

    def test_empty_body_still_draws_a_box(self):
        assert len(ui.card(PLAIN, "Vacío", []).split("\n")) == 3

    def test_color_colors_the_border_and_title(self):
        out = ui.card(COLOR, "Peligro", ["x"], kind="error")
        assert out.split("\n")[0].startswith("\x1b[31m╭─\x1b[0m")
        assert ui.strip_ansi(out) == ui.card(PLAIN, "Peligro", ["x"], kind="error")

    def test_unknown_kind_is_refused(self):
        with pytest.raises(ValueError):
            ui.card(PLAIN, "x", [], kind="weird")

    def test_the_card_never_hides_the_full_value(self):
        out = ui.card(PLAIN, "Confirmar", [f"• Escribir «{CURP}» en «CURP» (gob.mx)"], kind="confirm")
        assert CURP in out

    def test_answer_and_error_panels(self):
        answer = ui.answer_panel(PLAIN, "Encontré tres recetas.")
        assert "✓ Respuesta" in answer and "│ Encontré tres recetas." in answer
        error = ui.error_panel(PLAIN, "No pude abrir la página.", hint="Intente de nuevo.")
        assert "✗ Error" in error and "│ No pude abrir la página." in error and "│ Intente de nuevo." in error
        assert ui.error_panel(PLAIN, "x", title="Fatal error").split("\n")[0].startswith("╭─ ✗ Fatal error")


class TestTable:
    ROWS = [["→", "main", "Google"], ["", "correo", "Bandeja de entrada (3) - Gmail"]]

    def test_columns_line_up(self):
        out = ui.table(PLAIN, ["", "Tab", "Title"], self.ROWS)
        assert out.split("\n") == [
            "     Tab     Title",
            "  ─  ──────  ──────────────────────────────",
            "  →  main    Google",
            "     correo  Bandeja de entrada (3) - Gmail",
        ]

    def test_ascii_table(self):
        out = ui.table(ASCII, ["A", "B"], [["uno", "dos"]])
        assert out.split("\n") == ["  A    B", "  ---  ---", "  uno  dos"]

    def test_wide_columns_are_shrunk_and_truncated(self):
        term = ui.Term(width=40)
        out = ui.table(term, ["Tab", "Title"], [["main", "t" * 100]])
        lines = out.split("\n")
        assert all(width_of(line) <= 39 for line in lines)
        assert lines[2].endswith("…")

    def test_wide_characters_do_not_break_the_alignment(self):
        out = ui.table(PLAIN, ["N", "Texto"], [["1", "日本語"], ["22", "hola"]])
        lines = out.split("\n")
        assert lines[2].index("日") == lines[3].index("h")

    def test_short_rows_and_empty_tables(self):
        assert ui.table(PLAIN, ["A", "B"], [["solo"]]).split("\n")[-1] == "  solo"
        assert len(ui.table(PLAIN, ["A", "B"], []).split("\n")) == 2

    def test_cells_are_sanitized_and_single_line(self):
        out = ui.table(PLAIN, ["A"], [["uno\ndos\x1b[2J"]])
        assert out.split("\n")[-1] == "  uno dos"

    def test_column_styles(self):
        out = ui.table(COLOR, ["A", "B"], [["x", "y"]], styles=[("bold",), ()])
        assert "\x1b[1mx\x1b[0m" in out
        assert ui.strip_ansi(out) == ui.table(PLAIN, ["A", "B"], [["x", "y"]])


class TestEmitAndStatus:
    def interactive(self, **kwargs: Any) -> ui.Term:
        return ui.Term(color=False, unicode=True, width=60, interactive=True, **kwargs)

    def leftovers(self) -> List[Any]:
        return [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]

    def test_emit_prints_one_line(self):
        stream = FakeStream()
        ui.emit("hola", stream=stream)
        ui.emit(stream=stream)
        assert stream.getvalue() == "hola\n\n"

    async def test_a_status_draws_one_line_and_erases_it(self):
        stream = FakeStream(tty=True)
        status = ui.Status(self.interactive(), "Thinking", hint="Ctrl+C to stop", stream=stream, interval=0.01)
        assert status.start() is True
        await asyncio.sleep(0.08)
        assert status.running
        await status.stop()
        out = stream.getvalue()
        assert "Thinking… " in out and "Ctrl+C to stop" in out
        assert out.endswith("\r\x1b[2K")
        assert "\n" not in out
        assert not status.running and ui._active is None
        assert self.leftovers() == []

    async def test_frames_change_and_the_ascii_fallback_is_ascii(self):
        stream = FakeStream(tty=True)
        term = ui.Term(color=False, unicode=False, width=60, interactive=True)
        async with ui.Status(term, "Thinking", stream=stream, interval=0.01):
            await asyncio.sleep(0.08)
        out = stream.getvalue()
        assert out.isascii()
        assert len({frame for frame in "|/-\\" if f"{frame} Thinking..." in out}) >= 2

    async def test_the_label_and_elapsed_time_update(self):
        stream = FakeStream(tty=True)
        now = [100.0]
        status = ui.Status(self.interactive(), "Thinking", stream=stream, interval=0.01, clock=lambda: now[0])
        status.start()
        await asyncio.sleep(0.03)
        status.label = "Working on step 2/3"
        now[0] = 107.0
        await asyncio.sleep(0.05)
        await status.stop()
        out = stream.getvalue()
        assert "Working on step 2/3… 7s" in out

    async def test_non_interactive_terminals_get_nothing(self):
        stream = FakeStream()
        status = ui.Status(ui.Term(), "Thinking", stream=stream)
        assert status.start() is False
        await asyncio.sleep(0.02)
        await status.stop()
        assert stream.getvalue() == ""
        assert ui._active is None

    def test_starting_without_a_loop_is_harmless(self):
        status = ui.Status(self.interactive(), stream=FakeStream(tty=True))
        assert status.start() is False

    async def test_lines_printed_while_active_go_above_the_indicator(self):
        stream = FakeStream(tty=True)
        async with ui.Status(self.interactive(), "Thinking", stream=stream, interval=0.01):
            await asyncio.sleep(0.03)
            ui.emit("  ✓ paso 1", stream=stream)
            await asyncio.sleep(0.03)
        out = stream.getvalue()
        assert "\r\x1b[2K  ✓ paso 1\n" in out
        after = out.split("  ✓ paso 1\n", 1)[1]
        assert "Thinking…" in after

    async def test_prints_to_other_streams_are_not_intercepted(self):
        stream = FakeStream(tty=True)
        other = FakeStream()
        async with ui.Status(self.interactive(), "Thinking", stream=stream, interval=0.01):
            ui.emit("aparte", stream=other)
        assert other.getvalue() == "aparte\n"
        assert "aparte" not in stream.getvalue()

    async def test_suspended_pauses_the_drawing(self):
        stream = FakeStream(tty=True)
        async with ui.Status(self.interactive(), "Thinking", stream=stream, interval=0.01):
            await asyncio.sleep(0.03)
            with ui.suspended():
                size = len(stream.getvalue())
                assert stream.getvalue().endswith("\r\x1b[2K")
                await asyncio.sleep(0.06)
                assert len(stream.getvalue()) == size
            await asyncio.sleep(0.03)
            assert len(stream.getvalue()) > size

    def test_suspended_without_a_status_is_a_noop(self):
        with ui.suspended():
            pass

    async def test_cancelling_the_owner_leaves_nothing_behind(self):
        stream = FakeStream(tty=True)

        async def owner() -> None:
            async with ui.Status(self.interactive(), "Thinking", stream=stream, interval=0.01):
                await asyncio.sleep(30)

        task = asyncio.ensure_future(owner())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert ui._active is None
        assert stream.getvalue().endswith("\r\x1b[2K")
        assert self.leftovers() == []

    async def test_a_failing_stream_never_breaks_the_run(self):
        status = ui.Status(self.interactive(), "Thinking", stream=BrokenStream(tty=True), interval=0.01)
        status.start()
        await asyncio.sleep(0.04)
        status.write("hola")
        await status.stop()
        assert self.leftovers() == []

    async def test_stop_twice_is_fine(self):
        status = ui.Status(self.interactive(), "Thinking", stream=FakeStream(tty=True), interval=0.01)
        status.start()
        await status.stop()
        await status.stop()

    async def test_a_long_label_is_truncated_to_the_line(self):
        stream = FakeStream(tty=True)
        status = ui.Status(ui.Term(width=40, interactive=True), "x" * 200, hint="Ctrl+C to stop", stream=stream)
        status.start()
        await asyncio.sleep(0.02)
        await status.stop()
        frame = status.frame()
        assert ui.display_width(ui.strip_ansi(frame)) <= 39


class TestDemo:
    def test_demo_without_color_has_no_escape_codes_and_every_component(self, capsys, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "1")
        monkeypatch.setenv("COLUMNS", "80")
        ui.main([])
        out = capsys.readouterr().out
        assert "\x1b" not in out
        for expected in ["█▄▄", "── Steps", "✓ goto", "✗ click", "╭─ ▲ Escribir datos personales", "Necesito su ayuda", "BABAS pregunta", "Permiso para un sitio", "✓ Respuesta", "✗ Error", "Tab", "Command", "❯ Continue? (y/N):", "Thinking indicator"]:
            assert expected in out
        assert all(width_of(line) <= 80 for line in out.split("\n"))

    def test_demo_ascii(self, capsys, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "1")
        ui.main(["--ascii", "--width", "50"])
        out = capsys.readouterr().out
        assert not any(ch in out for ch in "─│╭╮╰╯✓✗●○•▲◆→❯█▄▀")
        assert "+- " in out and "|___/" in out
        assert all(width_of(line) <= 50 for line in out.split("\n"))

    def test_demo_with_forced_color(self, capsys, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("FORCE_COLOR", "1")
        ui.main([])
        out = capsys.readouterr().out
        assert "\x1b[" in out


class TestConsoleChannelRendering:
    @staticmethod
    def scripted(*answers: Any, prompts: Optional[List[str]] = None, probe: Any = None):
        queue = list(answers)

        async def ainput(prompt: str) -> str:
            if prompts is not None:
                prompts.append(prompt)
            if probe is not None:
                probe()
            item = queue.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item

        return ainput

    async def test_confirm_is_a_card_with_the_full_value(self, capsys):
        channel = ConsoleChannel(ainput=self.scripted("y"))
        request = ConfirmRequest([], ["pii_value"], f"Su CURP {CURP}", [f"Escribir «{CURP}» en «CURP» (gob.mx)", "Presionar «Enviar»"])
        assert (await channel.confirm(request)).approved is True
        out = capsys.readouterr().out
        lines = out.strip("\n").split("\n")
        assert lines[0].startswith("╭─ ▲ Escribir datos personales")
        assert f"│ • Escribir «{CURP}» en «CURP» (gob.mx)" in out
        assert "│ • Presionar «Enviar»" in out
        assert f"BABAS dice: Su CURP {MASKED_CURP}" in out and out.count(CURP) == 1
        assert lines[-1].startswith("╰")
        assert {width_of(line) for line in lines} == {79}

    async def test_confirm_without_lines_still_shows_the_explanation(self, capsys):
        channel = ConsoleChannel(ainput=self.scripted("n"))
        result = await channel.confirm(ConfirmRequest([], ["consequential"], "porque sí", []))
        assert (result.approved, result.reason) == (False, "denied")
        out = capsys.readouterr().out
        assert "BABAS dice: porque sí" in out and "Acción importante" in out

    async def test_a_color_terminal_gets_ansi_and_an_ascii_one_gets_ascii(self, capsys):
        colored = ConsoleChannel(ainput=self.scripted("y"), term=COLOR)
        await colored.confirm(ConfirmRequest([], ["payment"], "", ["Pagar"]))
        out = capsys.readouterr().out
        assert "\x1b[33m" in out and "Confirmar un pago" in out
        plain = ConsoleChannel(ainput=self.scripted("y"), term=ASCII)
        await plain.confirm(ConfirmRequest([], ["payment"], "", ["Pagar"]))
        out = capsys.readouterr().out
        assert "\x1b" not in out and "+- ! Confirmar un pago" in out and "| - Pagar" in out

    async def test_questions_and_handoffs_are_cards(self, capsys):
        prompts: List[str] = []
        channel = ConsoleChannel(ainput=self.scripted("Jalisco", "", prompts=prompts))
        assert await channel.ask(f"¿Cuál es su CURP {CURP}?") == "Jalisco"
        assert await channel.ask("Resuelva el CAPTCHA y presione «Listo».", "handoff") == "listo"
        out = capsys.readouterr().out
        assert "╭─ ? BABAS pregunta" in out and "╭─ ◆ Necesito su ayuda" in out
        assert CURP not in out and MASKED_CURP in out
        assert prompts == ["❯ ", "❯ Press Enter when you are done… "]

    async def test_site_permission_is_a_card_and_a_prompt(self, capsys):
        prompts: List[str] = []
        channel = ConsoleChannel(ainput=self.scripted("y", prompts=prompts))
        assert await channel.allow_site("imss.gob.mx", "https://imss.gob.mx/") is True
        out = capsys.readouterr().out
        assert "Permiso para un sitio" in out and "BABAS quiere usar el sitio imss.gob.mx." in out
        assert prompts == ["❯ Allow BABAS to use imss.gob.mx? (y/N): "]

    async def test_log_kinds(self, capsys):
        channel = ConsoleChannel()
        await channel.log("assistant", "Voy a buscar")
        await channel.log("step", "click [1]", True)
        await channel.log("step", "click [2]", False)
        await channel.log("step", "click [3]")
        await channel.log("result", "Listo, ya está.")
        await channel.log("error", "No pude.")
        await channel.log("info", "Detuve la tarea.")
        out = capsys.readouterr().out
        for expected in ["◆ BABAS: Voy a buscar", "✓ click [1]", "✗ click [2]", "• click [3]", "│ Listo, ya está.", "│ No pude.", "• Detuve la tarea."]:
            assert expected in out

    async def test_the_indicator_is_suspended_while_waiting_for_the_user(self, capsys):
        stream = FakeStream(tty=True)
        status = ui.Status(ui.Term(width=60, interactive=True), "Thinking", stream=stream, interval=0.01)
        status.start()
        seen: List[int] = []
        try:
            channel = ConsoleChannel(ainput=self.scripted("y", probe=lambda: seen.append(status._paused)))
            await channel.confirm(ConfirmRequest([], ["consequential"], "", ["x"]))
            assert seen == [1] and status._paused == 0
        finally:
            await status.stop()

    async def test_the_indicator_resumes_when_input_is_cancelled(self):
        stream = FakeStream(tty=True)
        status = ui.Status(ui.Term(width=60, interactive=True), "Thinking", stream=stream, interval=0.01)
        status.start()
        try:
            channel = ConsoleChannel(ainput=self.scripted(asyncio.CancelledError()))
            with pytest.raises(asyncio.CancelledError):
                await channel.confirm(ConfirmRequest([], [], "", []))
            assert status._paused == 0
        finally:
            await status.stop()


def page_state():
    return make_state(make_element(0, "button", "Buscar"))


class TestCliRendering:
    def test_help_lists_commands_and_examples(self):
        out = cli.render_help(PLAIN)
        assert "── Commands" in out and "── Examples" in out
        assert "exit, quit, q" in out and "Close program" in out
        assert '→ "open youtube and search lofi music"' in out
        assert ui.strip_ansi(cli.render_help(COLOR)) == out

    def test_tabs_table(self):
        out = cli.render_tabs(PLAIN, [["→", "main", "Google"], ["", "mail", "Gmail"]])
        assert out.split("\n")[0].split() == ["Tab", "Title"]
        assert "→  main" in out and "   mail" in out

    def test_browser_label(self):
        assert cli._browser_label("connect", {"port": 9222, "browser": "chrome"}) == "chrome (port 9222)"
        assert cli._browser_label("connect", {}) == "your browser"
        assert cli._browser_label("saved", {}).startswith("built-in Chromium")
        assert "temporary" in cli._browser_label("new", {})

    async def test_run_task_renders_the_whole_run(self, monkeypatch, capsys):
        llm = FakeLLM(
            [
                {"steps": [{"action": "click", "index": 0}, {"action": "scroll", "value": "down"}], "reasoning": "t"},
                {"steps": [{"action": "done", "value": "Listo, ya busqué."}], "reasoning": "t"},
            ]
        )
        monkeypatch.setattr(cli, "make_llm", lambda settings: llm)
        result = await cli.run_task(FakeDriver(page_state()), SafetyPolicy(), "busca", settings=Settings(llm_api_key="sk-test"))
        out = capsys.readouterr().out
        assert result is not None and result.status == "done"
        assert "○ Step 1/2: click [0]" in out and "○ Step 2/2: scroll down" in out
        assert out.index("Step 1/2") < out.index("✓ click [0]") < out.index("Respuesta")
        assert "│ Listo, ya busqué." in out
        assert f"✓ Done  {result.steps} step" in out and " s" in out.split("✓ Done", 1)[1]
        assert "\x1b" not in out

    async def test_run_task_reports_a_failed_run(self, monkeypatch, capsys):
        llm = FakeLLM([{"steps": [{"action": "done", "value": "No pude.", "success": False}], "reasoning": "t"}])
        monkeypatch.setattr(cli, "make_llm", lambda settings: llm)
        result = await cli.run_task(FakeDriver(page_state()), SafetyPolicy(), "x", settings=Settings(llm_api_key="sk-test"))
        out = capsys.readouterr().out
        assert result is not None and result.status == "failed"
        assert "✗ Could not complete the task" in out

    async def test_run_task_without_a_key_shows_an_error_panel(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "make_llm", lambda settings: pytest.fail("the model must not be built"))
        result = await cli.run_task(FakeDriver(page_state()), SafetyPolicy(), "x", settings=Settings(llm_api_key=None))
        out = capsys.readouterr().out
        assert result is None
        assert "✗ Error" in out and "BABAS_LLM_API_KEY" in out and "Error: " not in out.replace("✗ Error", "")

    @pytest.mark.skipif(sys.platform == "win32", reason="the SIGINT handler is not installed on Windows")
    async def test_ctrl_c_cancels_the_run_and_cleans_up(self, monkeypatch, capsys):
        llm = FakeLLM([{"steps": [{"action": "done", "value": "tarde"}], "reasoning": "t"}], delay=30)
        monkeypatch.setattr(cli, "make_llm", lambda settings: llm)
        before = signal.getsignal(signal.SIGINT)
        task = asyncio.ensure_future(cli.run_task(FakeDriver(page_state()), SafetyPolicy(), "x", settings=Settings(llm_api_key="sk-test")))
        await asyncio.sleep(0.1)
        assert signal.getsignal(signal.SIGINT) is not before, "the run must own SIGINT while it works"
        os.kill(os.getpid(), signal.SIGINT)
        assert await asyncio.wait_for(task, 5) is None
        assert signal.getsignal(signal.SIGINT) is before
        assert "▲ Stopped" in capsys.readouterr().out
        assert ui._active is None
        assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task()] == []

    async def test_the_printer_updates_the_indicator_label(self):
        status = ui.Status(ui.Term(), "Thinking")
        on_event = cli._make_printer(None, status)
        await on_event("plan", {"steps": ["click [0]", "click [1]"]})
        assert status.label == "Working on step 1/2"
        await on_event("step", {"count": 1, "summary": "click [0]", "ok": True})
        assert status.label == "Working on step 2/2"
        await on_event("step", {"count": 2, "summary": "click [1]", "ok": True})
        assert status.label == "Thinking"
        await on_event("plan", {"steps": ["a", "b"]})
        await on_event("step", {"count": 3, "summary": "a", "ok": False})
        assert status.label == "Thinking"

    async def test_the_printer_ignores_other_events_and_feeds_the_gui(self, capsys):
        shown: List[str] = []
        gui = types.SimpleNamespace(show_message=shown.append)
        on_event = cli._make_printer(gui)
        await on_event("other", {"steps": ["x"]})
        await on_event("plan", {"steps": []})
        assert capsys.readouterr().out == ""
        await on_event("plan", {"steps": [f"type_text [1] {CURP}"]})
        out = capsys.readouterr().out
        assert f"Step 1/1: type_text [1] {MASKED_CURP}" in out and CURP not in out
        assert "Step 1/1" in shown[0]


class TestDevreplRendering:
    async def run(self, driver: FakeDriver, *lines: str) -> str:
        repl = ReplSession(driver)
        for line in lines:
            await repl.run(line)
        return ""

    async def test_help_is_a_table(self, capsys):
        await ReplSession(FakeDriver(page_state())).run("help")
        out = capsys.readouterr().out
        assert "Command" in out and "What it does" in out
        assert "click N" in out and "quit, exit, q" in out

    async def test_tabs_is_a_table(self, capsys):
        await ReplSession(FakeDriver(page_state())).run("tabs")
        out = capsys.readouterr().out
        assert "Tab" in out and "→  main" in out

    async def test_errors_and_usage_are_marked(self, capsys):
        repl = ReplSession(FakeDriver(page_state()))
        await repl.run("click 0")
        await repl.run("click")
        await repl.run("frobnicate")
        out = capsys.readouterr().out
        assert "✗ ERROR: run ls first" in out
        assert "✗ ERROR: usage: click N" in out
        assert "▲ Unknown command 'frobnicate'. Type help to see the commands." in out

    async def test_ls_and_actions(self, capsys):
        driver = FakeDriver(make_state(make_element(0, "button", "Buscar"), make_element(1, "textbox", "Nombre", flags=["pii"]), url="https://www.gob.mx/"))
        repl = ReplSession(driver)
        await repl.run("ls")
        await repl.run("click 0")
        out = capsys.readouterr().out
        assert "URL: https://www.gob.mx/" in out
        assert '[0] button "Buscar"' in out and '[1] textbox "Nombre" {pii}' in out
        assert "✓ OK" in out

    def test_elements_keep_the_same_text_with_color(self):
        element = make_element(3, "textbox", "CURP", flags=["pii", "submit"], value="hola")
        plain = _format_element(element)
        assert plain == '[3] textbox "CURP" {pii,submit} = hola'
        colored = _format_element(element, COLOR)
        assert "\x1b[" in colored and ui.strip_ansi(colored) == plain

    async def test_page_text_cannot_inject_escape_codes(self, capsys):
        evil = "\x1b[2J\x1b]0;pwned\x07"
        driver = FakeDriver(make_state(make_element(0, "button", f"Pagar{evil}", value=f"v{evil}"), title=f"T{evil}"), text=f"texto{evil}")
        repl = ReplSession(driver)
        await repl.run("ls")
        await repl.run("read")
        await repl.run("read 0")
        out = capsys.readouterr().out
        assert "\x1b" not in out and "\x07" not in out
        assert '[0] button "Pagar"' in out and "texto" in out


class TestPairAndHelp:
    @pytest.fixture
    def home(self, monkeypatch, tmp_path):
        monkeypatch.setenv("BABAS_HOME", str(tmp_path / "home"))
        monkeypatch.delenv("FORCE_COLOR", raising=False)
        return tmp_path / "home"

    def test_pair_output_has_no_escape_codes_in_a_pipe(self, home, capsys):
        babas_main(["pair"])
        out = capsys.readouterr().out
        assert "\x1b" not in out
        assert f"No pairing file: {home / 'pairing.json'}" in out

    def test_pair_output_is_colored_when_forced(self, home, capsys, monkeypatch):
        monkeypatch.setenv("FORCE_COLOR", "1")
        babas_main(["pair"])
        out = capsys.readouterr().out
        assert "\x1b[33mNo pairing file" in out

    def test_help_shows_examples(self, capsys):
        with pytest.raises(SystemExit) as info:
            babas_main(["--help"])
        assert info.value.code == 0
        out = capsys.readouterr().out
        assert "examples:" in out and "python -m babas serve --repl" in out
        assert out.isascii()
