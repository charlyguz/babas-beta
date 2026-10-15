import asyncio
import logging
import os
import random
import shutil
import signal
import sys
import threading
import time
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from playwright.async_api import Page, Playwright, async_playwright

from babas import __version__, ui, voice
from babas.agent import Agent, RunResult
from babas.channels import ConsoleChannel
from babas.config import Settings
from babas.debug import cleanup_temp_dir, get_temp_debug_dir
from babas.drivers.playwright_driver import (
    PlaywrightDriver,
    get_available_browsers,
    get_preferred_browser,
    scan_debug_ports,
    try_launch_and_connect,
)
from babas.gate import SafeDriver
from babas.llm import make_llm
from babas.logs import setup_logging
from babas.safety import SafetyPolicy, redact

log = logging.getLogger("babas.cli")

HACKER_PHRASES = [
    "Accessing mainframe...",
    "Bypassing firewall...",
    "Injecting payload...",
    "Decrypting credentials...",
    "Escalating privileges...",
    "Establishing backdoor...",
    "Cleaning logs...",
]

HACKER_SUCCESS_PHRASES = [
    "I'm in.",
    "Access granted sir.",
    "The system is ours.",
    "We've penetrated their defenses sir.",
    "Root access obtained. We own it now.",
    "Mission complete. The mainframe has fallen.",
]


_input_pending = threading.Event()
_pending_read: Optional["asyncio.Future[str]"] = None

STOP_POLL_S = 0.3

HELP_COMMANDS = [
    ("exit, quit, q", "Close program"),
    ("help, ?", "Show this help"),
    ("tabs", "Show open tabs"),
    ("new", "Open new tab"),
    ("screenshot, ss", "Take screenshot"),
    ("mute, shh", "Mute butler"),
]

HELP_EXAMPLES = [
    "open youtube and search lofi music",
    "go to gmail",
    "in new tab open twitter",
    "search two sum on leetcode",
]


def _term() -> ui.Term:
    return ui.detect(sys.stdout)


def _line(status: str, text: str, **kwargs: Any) -> None:
    ui.emit(ui.step(_term(), status, text, **kwargs))


def _option(term: ui.Term, key: str, text: str) -> str:
    return f"    {term.paint(key, 'bold', 'bright_cyan')} {text}"


def preflight_problem(settings: Settings) -> Optional[str]:
    if settings.needs_api_key and not settings.llm_api_key:
        fallback = "ANTHROPIC_API_KEY" if settings.llm_provider == "anthropic" else "OPENAI_API_KEY"
        return f"Error: BABAS_LLM_API_KEY (or {fallback}) is not set. Put it in .env or the environment."
    if not settings.llm_model:
        return "Error: BABAS_LLM_MODEL is not set. Put it in .env or the environment."
    return None


def _start_read(prompt: str) -> "asyncio.Future[str]":
    # A daemon thread, not asyncio.to_thread: a pool worker blocked in input() keeps the process alive after Ctrl+C.
    loop = asyncio.get_running_loop()
    future: "asyncio.Future[str]" = loop.create_future()

    def deliver(line: Optional[str], error: Optional[Exception]) -> None:
        if future.done():
            return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(line or "")

    def read() -> None:
        line: Optional[str] = None
        error: Optional[Exception] = None
        try:
            line = input(ui.for_input(prompt))
        except Exception as e:
            error = e
        finally:
            _input_pending.clear()
        try:
            loop.call_soon_threadsafe(deliver, line, error)
        except RuntimeError:
            pass

    _input_pending.set()
    threading.Thread(target=read, name="babas-input", daemon=True).start()
    return future


async def _ainput(prompt: str = "") -> str:
    # A read abandoned by Ctrl+C is still blocked in input(); the next prompt must adopt it, or it would swallow the next line.
    global _pending_read
    pending = _pending_read
    if pending is None or pending.get_loop() is not asyncio.get_running_loop():
        pending = _pending_read = _start_read(prompt)
    elif not pending.done():
        sys.stdout.write(prompt)
        sys.stdout.flush()
    try:
        line = await asyncio.shield(pending)
    except asyncio.CancelledError:
        raise
    except Exception:
        if _pending_read is pending:
            _pending_read = None
        raise
    if _pending_read is pending:
        _pending_read = None
    return line


def _exit_if_input_pending() -> None:
    # A daemon thread still blocked in input() holds the C stdin lock, and the normal interpreter exit then deadlocks on it.
    if _input_pending.is_set():
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(130)


async def run_hacker_mode(page: Page, butler: Any = None) -> None:
    term = _term()

    def say(text: str = "") -> None:
        ui.emit(term.paint(text, "green"))

    say()
    say("=" * 60)
    say("INITIATING INTRUSION PROTOCOL...")
    say("=" * 60)
    if butler and butler.enabled:
        butler.say("Initiating intrusion protocol sir. Accessing the mainframe.")
    try:
        await asyncio.sleep(0.5)
        await page.goto("https://hackertyper.com/", wait_until="domcontentloaded")
        await asyncio.sleep(1)
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.3)
        except Exception:
            pass
        say()
        say("HACKING...")
        for i in range(random.randint(150, 250)):
            key = random.choice("asdfghjklqwertyuiopzxcvbnm1234567890")
            await page.keyboard.press(key)
            delay = random.uniform(0.01, 0.05)
            await asyncio.sleep(delay)
            if i in (30, 70, 120, 180):
                say(f"   {random.choice(HACKER_PHRASES)}")
        await asyncio.sleep(0.5)
        say()
        say("=" * 60)
        success_phrase = random.choice(HACKER_SUCCESS_PHRASES)
        say(f"   {success_phrase}")
        say("=" * 60)
        say()
        if butler and butler.enabled:
            butler.say(success_phrase)
        await asyncio.sleep(2)
    except Exception as e:
        say(f"   Error in hacker mode: {redact(str(e))}")
        if butler and butler.enabled:
            butler.say("We've been detected sir. Aborting mission.")


async def select_session_mode(playwright: Playwright) -> Tuple[str, dict, Any]:
    term = _term()
    ui.emit()
    ui.emit(ui.rule(term, "Browser"))
    available = get_available_browsers()
    preferred = get_preferred_browser()
    pairs = [("available", ", ".join(b.capitalize() for b in available) if available else "none")]
    if preferred:
        pairs.append(("default", preferred.capitalize()))
    ui.emit(ui.info_row(term, pairs))
    port = scan_debug_ports()
    if port:
        _line("info", f"Browser detected on port {port}")
        try:
            browser = await playwright.chromium.connect_over_cdp(f"http://localhost:{port}")
            contexts = browser.contexts
            pages = contexts[0].pages if contexts else []
            _line("success", f"Connected! {len(pages)} tabs")
            return "connect", {"port": port}, browser
        except Exception as e:
            _line("failure", f"Error connecting: {redact(str(e))}")
    ui.emit()
    ui.emit("  " + term.paint("Which browser to use?", "bold"))
    if preferred:
        ui.emit(_option(term, "[ENTER]", f"{preferred.capitalize()} (recommended)"))
    for i, name in enumerate(available, 1):
        if name != preferred:
            ui.emit(_option(term, f"[{i}]", name.capitalize()))
    ui.emit(_option(term, "[n]", "New temporary session"))
    ui.emit()
    choice = (await _ainput(ui.question(term, f"Option [ENTER = {preferred.capitalize() if preferred else 'Chrome'}]: "))).strip().lower()
    if choice == "n":
        return "new", {}, None
    if choice == "" and preferred:
        browser_name = preferred
    else:
        try:
            idx = int(choice) - 1
            browser_name = available[idx] if 0 <= idx < len(available) else preferred or "chrome"
        except Exception:
            browser_name = preferred or "chrome"
    success, browser, port = await try_launch_and_connect(playwright, browser_name)
    if success:
        return "connect", {"port": port, "browser": browser_name}, browser
    for fallback in available:
        if fallback != browser_name:
            _line("running", f"Trying {fallback.capitalize()}...")
            success, browser, port = await try_launch_and_connect(playwright, fallback)
            if success:
                return "connect", {"port": port, "browser": fallback}, browser
    _line("failure", "Could not connect to any browser.")
    _line("info", "Using Playwright's built-in browser...")
    return "saved", {}, None


def _install_sigint(loop: asyncio.AbstractEventLoop, task: "asyncio.Future[Any]") -> Tuple[bool, Any]:
    if sys.platform == "win32":
        return False, None
    previous = signal.getsignal(signal.SIGINT)
    try:
        loop.add_signal_handler(signal.SIGINT, task.cancel)
    except (NotImplementedError, RuntimeError, ValueError):
        return False, None
    return True, previous


def _remove_sigint(loop: asyncio.AbstractEventLoop, previous: Any) -> None:
    try:
        loop.remove_signal_handler(signal.SIGINT)
        if previous is not None:
            signal.signal(signal.SIGINT, previous)
    except (NotImplementedError, RuntimeError, ValueError, OSError):
        pass


async def _watch_stop(voice_gui: Any, task: "asyncio.Future[Any]") -> None:
    while not task.done():
        await asyncio.sleep(STOP_POLL_S)
        try:
            requested = bool(voice_gui.is_stop_requested())
        except Exception:
            requested = False
        if requested:
            task.cancel()
            return


def _make_printer(voice_gui: Any, status: Optional[ui.Status] = None) -> Callable[[str, Dict[str, Any]], Awaitable[None]]:
    progress = {"total": 0, "done": 0}

    def relabel(text: str) -> None:
        if status is not None:
            status.label = text

    async def on_event(name: str, data: Dict[str, Any]) -> None:
        if name == "step":
            progress["done"] += 1
            more = data.get("ok") is not False and progress["done"] < progress["total"]
            relabel(f"Working on step {progress['done'] + 1}/{progress['total']}" if more else "Thinking")
            return
        steps = data.get("steps")
        if name != "plan" or not isinstance(steps, list) or not steps:
            return
        progress.update(total=len(steps), done=0)
        relabel(f"Working on step 1/{len(steps)}")
        term = _term()
        lines = [f"Step {number}/{len(steps)}: {redact(str(summary))}" for number, summary in enumerate(steps, 1)]
        for line in lines:
            ui.emit(ui.step(term, "pending", line))
        if voice_gui is not None:
            voice_gui.show_message("Executing...\n(Press STOP to cancel)\n\n" + "\n".join(lines))

    return on_event


def _gui_result(voice_gui: Any, status: str, color: str, message: str) -> None:
    if voice_gui is not None:
        voice_gui.set_status(status, color)
        voice_gui.show_message(f"{message}\n\nRecord another command.")


async def _close_llm(llm: Any) -> None:
    close = getattr(llm, "close", None)
    if close is None:
        return
    try:
        await close()
    except Exception:
        pass


async def run_task(
    driver: PlaywrightDriver,
    policy: SafetyPolicy,
    text: str,
    voice_gui: Any = None,
    butler: Any = None,
    *,
    settings: Settings,
) -> Optional[RunResult]:
    term = _term()
    problem = preflight_problem(settings)
    if problem:
        ui.emit(ui.error_panel(term, problem.removeprefix("Error:").strip()))
        _gui_result(voice_gui, "Error", "#ff4757", problem)
        return None
    channel = ConsoleChannel(voice_gui, ainput=_ainput)
    status = ui.Status(term, "Thinking", hint="Ctrl+C to stop")
    llm = make_llm(settings)
    agent = Agent(
        SafeDriver(driver, policy, channel),
        llm,
        channel,
        language=settings.language,
        max_steps=settings.max_steps,
        max_retries=settings.max_retries,
        on_event=_make_printer(voice_gui, status),
    )
    if voice_gui is not None:
        voice_gui.set_status("Executing...", "#ff9f43")
        voice_gui.show_message(f'Executing...\n(Press STOP to cancel)\n\n"{redact(text)[:50]}"')
        voice_gui.start_execution()
    if butler and butler.enabled:
        butler.executing()
    loop = asyncio.get_running_loop()
    started = time.monotonic()
    task = asyncio.ensure_future(agent.run(text))
    watcher = asyncio.ensure_future(_watch_stop(voice_gui, task)) if voice_gui is not None else None
    handled, previous = _install_sigint(loop, task)
    status.start()
    try:
        await asyncio.wait({task})
    except asyncio.CancelledError:
        task.cancel()
        await asyncio.wait({task})
        raise
    finally:
        await status.stop()
        if handled:
            _remove_sigint(loop, previous)
        if watcher is not None:
            watcher.cancel()
        if voice_gui is not None:
            voice_gui.end_execution()
            voice_gui.clear_stop()
        await _close_llm(llm)
    if task.cancelled():
        ui.emit()
        _line("warn", "Stopped")
        _gui_result(voice_gui, "Stopped", "#ff9f43", "Stopped.")
        if butler and butler.enabled:
            butler.say("Understood sir. Stopping execution.")
        return None
    result = task.result()
    answer = redact(result.answer)
    summary = f"{result.steps} step{'' if result.steps == 1 else 's'} in {time.monotonic() - started:.1f} s"
    ui.emit()
    if result.status == "done":
        _line("success", "Done", detail=summary)
        _gui_result(voice_gui, "Complete", "#00ff88", answer or "Complete!")
        if butler and butler.enabled:
            butler.success()
    elif result.status == "stopped":
        _line("warn", "Stopped", detail=summary)
        _gui_result(voice_gui, "Stopped", "#ff9f43", answer or "Stopped.")
    elif result.status == "failed":
        _line("failure", "Could not complete the task", detail=summary)
        _gui_result(voice_gui, "Error", "#ff4757", answer or "Could not complete the task.")
        if butler and butler.enabled:
            butler.say("I'm very sorry sir. I couldn't complete this task despite my attempts. Ready for your next command.")
    else:
        _line("warn", "Partially completed", detail=summary)
        _gui_result(voice_gui, "Stopped", "#ff9f43", answer or "Step limit reached.")
    return result


def render_help(term: ui.Term) -> str:
    pointer = term.paint(term.glyph("pointer"), "dim")
    parts = [
        ui.rule(term, "Commands"),
        ui.table(term, ["Command", "What it does"], HELP_COMMANDS, styles=[("bold", "bright_cyan"), ()]),
        "",
        ui.rule(term, "Examples"),
    ]
    parts.extend(f'  {pointer} "{example}"' for example in HELP_EXAMPLES)
    return "\n".join(parts)


def render_tabs(term: ui.Term, rows: List[List[str]]) -> str:
    return ui.table(term, ["", "Tab", "Title"], rows, styles=[("bright_cyan",), ("bold",), ()])


def show_help() -> None:
    ui.emit()
    ui.emit(render_help(_term()))


def _browser_label(mode: str, config: Dict[str, Any]) -> str:
    if mode == "connect":
        name = str(config.get("browser") or "your browser")
        return f"{name} (port {config['port']})" if config.get("port") else name
    return "built-in Chromium (saved profile)" if mode == "saved" else "built-in Chromium (temporary)"


def _configure_voice(settings: Settings) -> None:
    try:
        import babas_listen

        babas_listen.configure(settings)
    except Exception as exc:
        log.debug("voice input is not configured: %s", type(exc).__name__)


async def amain(settings: Settings) -> None:
    term = _term()
    ui.emit()
    ui.emit(ui.banner(term, [("model", settings.llm_model or "not set"), ("lang", settings.language), ("safety", "on")], version=__version__))
    butler = None
    if voice.voice_output_available():
        butler = voice.init_butler()
        if butler and butler.enabled:
            _line("success", "Voice butler activated")
            butler.say("Starting BABAS system sir.")
    ui.emit()
    ui.emit(ui.rule(term, "Input mode"))
    ui.emit(_option(term, "[1]", "TEXT   type commands"))
    ui.emit(_option(term, "[2]", "VOICE  speak commands (opens a window)"))
    if butler and butler.enabled:
        butler.say("What mode do you prefer sir? Text or voice?")
    use_voice_mode = False
    voice_gui = None
    while True:
        ui.emit()
        mode_choice = (await _ainput(ui.question(term, "Option (1/2) [1]: "))).strip() or "1"
        if mode_choice == "1":
            _line("success", "TEXT mode selected")
            if butler and butler.enabled:
                butler.say("Text mode activated sir.")
            break
        elif mode_choice == "2":
            if voice.voice_gui_available():
                use_voice_mode = True
                voice_gui = voice.get_voice_gui()
                _configure_voice(settings)
                _line("success", "VOICE mode selected", detail="the control panel opens after connecting")
                if butler and butler.enabled:
                    butler.say("Voice mode activated sir. Opening control panel.")
                break
            else:
                _line("warn", "Voice GUI not available. Using text.")
                ui.emit(ui.muted(term, "    Install: pip install customtkinter sounddevice"))
                break
        else:
            _line("warn", "Please choose 1 or 2")
    profile_path = None
    try:
        async with async_playwright() as p:
            mode, config, browser = await select_session_mode(p)
            context = None
            is_connected = False
            if mode == "connect" and browser:
                is_connected = True
                contexts = browser.contexts
                if contexts:
                    context = contexts[0]
                else:
                    context = await browser.new_context()
            if context is None:
                if mode == "saved":
                    profile_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "babas-profile")
                    _line("info", "Using saved BABAS session...")
                else:
                    profile_path = os.path.join(get_temp_debug_dir(), "temp-profile")
                    _line("info", "Temporary session")
                context = await p.chromium.launch_persistent_context(
                    user_data_dir=profile_path,
                    headless=False,
                    args=['--disable-blink-features=AutomationControlled', '--start-maximized'],
                    viewport=None,
                    no_viewport=True
                )
            if is_connected:
                _line("running", "Creating new tab for BABAS...")
                initial_page = await context.new_page()
                _line("success", "Tab created", detail="your other tabs won't be touched")
            else:
                if context.pages:
                    initial_page = context.pages[0]
                else:
                    initial_page = await context.new_page()
            driver = PlaywrightDriver(context, initial_page, attached=is_connected, browser=browser)
            await driver.start()
            policy = SafetyPolicy(settings.allowed_domains)
            ui.emit()
            ui.emit(ui.rule(term, "Ready"))
            ui.emit(
                ui.info_row(
                    term,
                    [
                        ("mode", "voice" if use_voice_mode and voice_gui else "text"),
                        ("browser", _browser_label(mode, config)),
                        ("model", settings.llm_model or "not set"),
                        ("lang", settings.language),
                        ("safety", "on"),
                    ],
                )
            )
            ui.emit(ui.muted(term, "  Say what you want BABAS to do. Type help for commands, exit to close, Ctrl+C to stop a task."))
            if use_voice_mode and voice_gui:
                _line("running", "Opening voice control panel...")
                await asyncio.to_thread(voice_gui.open, butler)
                await asyncio.sleep(1)
            if butler and butler.enabled:
                butler.greet()
            while True:
                try:
                    if use_voice_mode and voice_gui and voice_gui.is_open:
                        if voice_gui.close_requested():
                            ui.emit()
                            _line("info", "Closing BABAS...")
                            if butler and butler.enabled:
                                butler.goodbye()
                            break
                        voice_command = await asyncio.to_thread(voice_gui.get_voice_command, timeout=0.5)
                        if not voice_command:
                            continue
                        if voice_command == "__CLOSE__":
                            ui.emit()
                            _line("info", "Closing BABAS...")
                            if butler and butler.enabled:
                                butler.goodbye()
                            break
                        ui.emit()
                        _line("info", f"Command: \"{redact(voice_command)}\"")
                        user_input = voice_command
                        if butler and butler.enabled:
                            butler.say(f"Understood: {redact(voice_command)[:30]}")
                    else:
                        ui.emit()
                        user_input = (await _ainput(ui.prompt(term))).strip()
                        if not user_input:
                            continue
                    cmd = user_input.lower()
                    if cmd in ["exit", "quit", "q"]:
                        if voice_gui and voice_gui.is_open:
                            voice_gui.close()
                        if butler and butler.enabled:
                            butler.goodbye()
                        _line("success", "Goodbye!")
                        break
                    if cmd in ["help", "?"]:
                        show_help()
                        continue
                    if cmd == "tabs":
                        rows = []
                        for name in driver.list_tabs():
                            page = driver.get_tab_page(name)
                            marker = term.glyph("pointer") if name == driver.current_tab else ""
                            try:
                                title = redact(await page.title() or page.url)
                            except Exception:
                                title = "(error)"
                            rows.append([marker, name, title])
                        ui.emit()
                        ui.emit(render_tabs(term, rows))
                        continue
                    if cmd == "new":
                        new_name = await driver.new_tab()
                        _line("success", f"New tab: '{new_name}'" if isinstance(new_name, str) else "New tab opened")
                        continue
                    if cmd in ["screenshot", "ss"]:
                        filename = f"screenshot_{datetime.now().strftime('%H%M%S')}.png"
                        image = await driver.screenshot()
                        with open(filename, "wb") as f:
                            f.write(image)
                        _line("success", f"Saved: {filename}")
                        continue
                    if cmd in ["mute", "shh", "shut up", "silence"]:
                        if butler and butler.enabled:
                            butler.shut_up()
                            _line("success", "Muted")
                        continue
                    if any(word in cmd for word in ["hack", "hacker"]):
                        if use_voice_mode and voice_gui and voice_gui.is_open:
                            voice_gui.set_status("Hacking...", "#ff4757")
                        await run_hacker_mode(driver.page, butler)
                        if use_voice_mode and voice_gui and voice_gui.is_open:
                            voice_gui.set_status("Ready", "#00ff88")
                            voice_gui.show_message("Hack complete\n\nRecord another command.")
                        continue
                    ui.emit()
                    gui = voice_gui if (use_voice_mode and voice_gui and voice_gui.is_open) else None
                    try:
                        await run_task(driver, policy, user_input, gui, butler, settings=settings)
                    except Exception as e:
                        ui.emit()
                        ui.emit(ui.error_panel(term, redact(str(e))))
                        _gui_result(gui, "Error", "#ff4757", f"Error: {redact(str(e))}")
                        continue
                except KeyboardInterrupt:
                    ui.emit()
                    _line("info", "(Ctrl+C - type 'exit' to close)")
                    continue
            ui.emit()
            ui.emit(ui.rule(term, "Closing"))
            tabs_closed = await driver.close()
            if tabs_closed > 0:
                _line("success", f"{tabs_closed} tab(s) closed")
            if driver.attached:
                _line("info", "Your browser is still open (without BABAS tabs)")
            else:
                _line("info", "Browser closed")
            cleanup_temp_dir()
            if mode == "new" and profile_path:
                try:
                    shutil.rmtree(profile_path, ignore_errors=True)
                    _line("success", "Temp profile deleted")
                except Exception:
                    pass
            _line("success", "Session ended")
            voice.shutdown_butler()
    except Exception as e:
        ui.emit()
        ui.emit(ui.error_panel(term, redact(str(e)), title="Fatal error"))
        cleanup_temp_dir()
        voice.shutdown_butler()
        raise


def main(settings: Optional[Settings] = None) -> None:
    if settings is None:
        try:
            settings = Settings.from_env()
        except ValueError as exc:
            print(f"Invalid configuration: {exc}", file=sys.stderr)
            raise SystemExit(2) from None
    setup_logging(settings.log_level)
    try:
        asyncio.run(amain(settings))
    except KeyboardInterrupt:
        ui.emit()
        _line("warn", "Cancelled")
        cleanup_temp_dir()
        voice.shutdown_butler()
        _exit_if_input_pending()
