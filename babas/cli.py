import asyncio
import os
import random
import shutil
import signal
import sys
import threading
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from dotenv import load_dotenv
from playwright.async_api import Page, Playwright, async_playwright

from babas import voice
from babas.agent import Agent, RunResult
from babas.channels import ConsoleChannel
from babas.debug import cleanup_temp_dir, get_temp_debug_dir
from babas.drivers.playwright_driver import (
    PlaywrightDriver,
    get_available_browsers,
    get_preferred_browser,
    scan_debug_ports,
    try_launch_and_connect,
)
from babas.gate import SafeDriver
from babas.llm import OpenAIClient
from babas.safety import SafetyPolicy, redact

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
MISSING_KEY_MESSAGE = "Error: OPENAI_API_KEY is not set. Put it in .env or the environment."


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
            line = input(prompt)
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
    print("\n" + "="*60)
    print("INITIATING INTRUSION PROTOCOL...")
    print("="*60)
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
        print("\nHACKING...")
        for i in range(random.randint(150, 250)):
            key = random.choice("asdfghjklqwertyuiopzxcvbnm1234567890")
            await page.keyboard.press(key)
            delay = random.uniform(0.01, 0.05)
            await asyncio.sleep(delay)
            if i == 30:
                print(f"   {random.choice(HACKER_PHRASES)}")
            elif i == 70:
                print(f"   {random.choice(HACKER_PHRASES)}")
            elif i == 120:
                print(f"   {random.choice(HACKER_PHRASES)}")
            elif i == 180:
                print(f"   {random.choice(HACKER_PHRASES)}")
        await asyncio.sleep(0.5)
        print("\n" + "="*60)
        success_phrase = random.choice(HACKER_SUCCESS_PHRASES)
        print(f"   {success_phrase}")
        print("="*60 + "\n")
        if butler and butler.enabled:
            butler.say(success_phrase)
        await asyncio.sleep(2)
    except Exception as e:
        print(f"   Error in hacker mode: {e}")
        if butler and butler.enabled:
            butler.say("We've been detected sir. Aborting mission.")


async def select_session_mode(playwright: Playwright) -> Tuple[str, dict, Any]:
    print("\nBROWSER CONFIGURATION")
    print("="*50)
    available = get_available_browsers()
    preferred = get_preferred_browser()
    print(f"\nAvailable: {', '.join(b.capitalize() for b in available) if available else 'none'}")
    if preferred:
        print(f"   Default: {preferred.capitalize()}")
    port = scan_debug_ports()
    if port:
        print(f"\nBrowser detected on port {port}")
        try:
            browser = await playwright.chromium.connect_over_cdp(f"http://localhost:{port}")
            contexts = browser.contexts
            pages = contexts[0].pages if contexts else []
            print(f"   Connected! {len(pages)} tabs")
            return "connect", {"port": port}, browser
        except Exception as e:
            print(f"   Error connecting: {e}")
    print("\nWhich browser to use?\n")
    if preferred:
        print(f"  [ENTER] {preferred.capitalize()} (recommended)")
    for i, name in enumerate(available, 1):
        if name != preferred:
            print(f"  [{i}] {name.capitalize()}")
    print(f"  [n] New temporary session")
    choice = (await _ainput(f"\nOption [ENTER = {preferred.capitalize() if preferred else 'Chrome'}]: ")).strip().lower()
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
            print(f"\nTrying {fallback.capitalize()}...")
            success, browser, port = await try_launch_and_connect(playwright, fallback)
            if success:
                return "connect", {"port": port, "browser": fallback}, browser
    print("\nCould not connect to any browser.")
    print("   Using Playwright's built-in browser...")
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


def _make_printer(voice_gui: Any) -> Callable[[str, Dict[str, Any]], Awaitable[None]]:
    async def on_event(name: str, data: Dict[str, Any]) -> None:
        steps = data.get("steps")
        if name != "plan" or not isinstance(steps, list) or not steps:
            return
        lines = [f"Step {number}/{len(steps)}: {redact(str(summary))}" for number, summary in enumerate(steps, 1)]
        for line in lines:
            print(f"   {line}")
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
) -> Optional[RunResult]:
    if not os.environ.get("OPENAI_API_KEY"):
        print(MISSING_KEY_MESSAGE)
        _gui_result(voice_gui, "Error", "#ff4757", MISSING_KEY_MESSAGE)
        return None
    channel = ConsoleChannel(voice_gui, ainput=_ainput)
    llm = OpenAIClient()
    agent = Agent(SafeDriver(driver, policy, channel), llm, channel, language="en", on_event=_make_printer(voice_gui))
    if voice_gui is not None:
        voice_gui.set_status("Executing...", "#ff9f43")
        voice_gui.show_message(f'Executing...\n(Press STOP to cancel)\n\n"{redact(text)[:50]}"')
        voice_gui.start_execution()
    if butler and butler.enabled:
        butler.executing()
    loop = asyncio.get_running_loop()
    task = asyncio.ensure_future(agent.run(text))
    watcher = asyncio.ensure_future(_watch_stop(voice_gui, task)) if voice_gui is not None else None
    handled, previous = _install_sigint(loop, task)
    try:
        await asyncio.wait({task})
    except asyncio.CancelledError:
        task.cancel()
        await asyncio.wait({task})
        raise
    finally:
        if handled:
            _remove_sigint(loop, previous)
        if watcher is not None:
            watcher.cancel()
        if voice_gui is not None:
            voice_gui.end_execution()
            voice_gui.clear_stop()
        await _close_llm(llm)
    if task.cancelled():
        print("\nStopped")
        _gui_result(voice_gui, "Stopped", "#ff9f43", "Stopped.")
        if butler and butler.enabled:
            butler.say("Understood sir. Stopping execution.")
        return None
    result = task.result()
    answer = redact(result.answer)
    if result.status == "done":
        print("\nDone!")
        _gui_result(voice_gui, "Complete", "#00ff88", answer or "Complete!")
        if butler and butler.enabled:
            butler.success()
    elif result.status == "stopped":
        print("\nStopped")
        _gui_result(voice_gui, "Stopped", "#ff9f43", answer or "Stopped.")
    elif result.status == "failed":
        print("\nCould not complete the task")
        _gui_result(voice_gui, "Error", "#ff4757", answer or "Could not complete the task.")
        if butler and butler.enabled:
            butler.say("I'm very sorry sir. I couldn't complete this task despite my attempts. Ready for your next command.")
    else:
        print("\nPartially completed")
        _gui_result(voice_gui, "Stopped", "#ff9f43", answer or "Step limit reached.")
    return result


def show_help() -> None:
    print("\nCOMMANDS:")
    print("   exit, quit, q    -> Close program")
    print("   help, ?          -> Show this help")
    print("   tabs             -> Show open tabs")
    print("   new              -> Open new tab")
    print("   screenshot, ss   -> Take screenshot")
    print("   mute, shh        -> Mute butler")
    print("\nEXAMPLES:")
    print('   "open youtube and search lofi music"')
    print('   "go to gmail"')
    print('   "in new tab open twitter"')
    print('   "search two sum on leetcode"')


async def amain() -> None:
    print("\n" + "="*60)
    print("STARTING BABAS SYSTEM...")
    print("="*60)
    butler = None
    if voice.voice_output_available():
        butler = voice.init_butler()
        if butler and butler.enabled:
            print("Voice butler activated")
            butler.say("Starting BABAS system sir.")
    print("\nCapabilities:")
    print("   - Smart web navigation")
    print("   - AI auto-recovery")
    print("   - Multiple tabs")
    print("   - Voice commands")
    print("\n" + "-"*40)
    print("SELECT INPUT MODE:")
    print("-"*40)
    print("\n   [1] TEXT  - Type commands")
    print("   [2] VOICE - Speak commands (opens window)")
    if butler and butler.enabled:
        butler.say("What mode do you prefer sir? Text or voice?")
    use_voice_mode = False
    voice_gui = None
    while True:
        mode_choice = (await _ainput("\n>>> Option (1/2) [1]: ")).strip() or "1"
        if mode_choice == "1":
            print("\nTEXT mode selected")
            if butler and butler.enabled:
                butler.say("Text mode activated sir.")
            break
        elif mode_choice == "2":
            if voice.voice_gui_available():
                use_voice_mode = True
                voice_gui = voice.get_voice_gui()
                print("\nVOICE mode selected")
                print("   -> Control panel will open after connecting")
                if butler and butler.enabled:
                    butler.say("Voice mode activated sir. Opening control panel.")
                break
            else:
                print("   Voice GUI not available. Using text.")
                print("   Install: pip install customtkinter sounddevice")
                break
        else:
            print("   Please choose 1 or 2")
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
                    print(f"\nUsing saved BABAS session...")
                else:
                    profile_path = os.path.join(get_temp_debug_dir(), "temp-profile")
                    print(f"\nTemporary session")
                context = await p.chromium.launch_persistent_context(
                    user_data_dir=profile_path,
                    headless=False,
                    args=['--disable-blink-features=AutomationControlled', '--start-maximized'],
                    viewport=None,
                    no_viewport=True
                )
            if is_connected:
                print("\nCreating new tab for BABAS...")
                initial_page = await context.new_page()
                print(f"   Tab created (your other tabs won't be touched)")
            else:
                if context.pages:
                    initial_page = context.pages[0]
                else:
                    initial_page = await context.new_page()
            driver = PlaywrightDriver(context, initial_page, attached=is_connected, browser=browser)
            await driver.start()
            policy = SafetyPolicy.from_env()
            print("\n" + "="*60)
            print("READY!")
            print("="*60)
            if use_voice_mode and voice_gui:
                print("\nOpening voice control panel...")
                await asyncio.to_thread(voice_gui.open, butler)
                await asyncio.sleep(1)
            if butler and butler.enabled:
                butler.greet()
            while True:
                try:
                    if use_voice_mode and voice_gui and voice_gui.is_open:
                        if voice_gui.close_requested():
                            print("\nClosing BABAS...")
                            if butler and butler.enabled:
                                butler.goodbye()
                            break
                        voice_command = await asyncio.to_thread(voice_gui.get_voice_command, timeout=0.5)
                        if not voice_command:
                            continue
                        if voice_command == "__CLOSE__":
                            print("\nClosing BABAS...")
                            if butler and butler.enabled:
                                butler.goodbye()
                            break
                        print(f"\nCommand: \"{voice_command}\"")
                        user_input = voice_command
                        if butler and butler.enabled:
                            butler.say(f"Understood: {voice_command[:30]}")
                    else:
                        print("\nWhat would you like to do? (or 'exit')")
                        user_input = (await _ainput(">>> ")).strip()
                        if not user_input:
                            continue
                    cmd = user_input.lower()
                    if cmd in ["exit", "quit", "q"]:
                        if voice_gui and voice_gui.is_open:
                            voice_gui.close()
                        if butler and butler.enabled:
                            butler.goodbye()
                        print("\nGoodbye!")
                        break
                    if cmd in ["help", "?"]:
                        show_help()
                        continue
                    if cmd == "tabs":
                        print("\nTabs:")
                        for name in driver.list_tabs():
                            page = driver.get_tab_page(name)
                            marker = "->" if name == driver.current_tab else "  "
                            try:
                                title = (await page.title())[:40] or page.url[:40]
                                print(f"   {marker} [{name}] {title}")
                            except Exception:
                                print(f"   {marker} [{name}] (error)")
                        continue
                    if cmd == "new":
                        await driver.new_tab()
                        continue
                    if cmd in ["screenshot", "ss"]:
                        filename = f"screenshot_{datetime.now().strftime('%H%M%S')}.png"
                        image = await driver.screenshot()
                        with open(filename, "wb") as f:
                            f.write(image)
                        print(f"   Saved: {filename}")
                        continue
                    if cmd in ["mute", "shh", "shut up", "silence"]:
                        if butler and butler.enabled:
                            butler.shut_up()
                            print("   Muted")
                        continue
                    if any(word in cmd for word in ["hack", "hacker"]):
                        if use_voice_mode and voice_gui and voice_gui.is_open:
                            voice_gui.set_status("Hacking...", "#ff4757")
                        await run_hacker_mode(driver.page, butler)
                        if use_voice_mode and voice_gui and voice_gui.is_open:
                            voice_gui.set_status("Ready", "#00ff88")
                            voice_gui.show_message("Hack complete\n\nRecord another command.")
                        continue
                    print()
                    gui = voice_gui if (use_voice_mode and voice_gui and voice_gui.is_open) else None
                    try:
                        await run_task(driver, policy, user_input, gui, butler)
                    except Exception as e:
                        print(f"\nError: {redact(str(e))}")
                        _gui_result(gui, "Error", "#ff4757", f"Error: {redact(str(e))}")
                        continue
                except KeyboardInterrupt:
                    print("\n\n(Ctrl+C - type 'exit' to close)")
                    continue
            print("\nClosing session...")
            tabs_closed = await driver.close()
            if tabs_closed > 0:
                print(f"   {tabs_closed} tab(s) closed")
            if driver.attached:
                print("   Your browser is still open (without BABAS tabs)")
            else:
                print("   Browser closed")
            cleanup_temp_dir()
            if mode == "new" and profile_path:
                try:
                    shutil.rmtree(profile_path, ignore_errors=True)
                    print("   Temp profile deleted")
                except Exception:
                    pass
            print("   Session ended")
            voice.shutdown_butler()
    except Exception as e:
        print(f"\nFatal error: {e}")
        cleanup_temp_dir()
        voice.shutdown_butler()
        raise


def main() -> None:
    print("Loading environment...")
    load_dotenv()
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        print("\nCancelled")
        cleanup_temp_dir()
        voice.shutdown_butler()
        _exit_if_input_pending()
