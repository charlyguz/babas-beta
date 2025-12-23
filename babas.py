# preview beta V0.1 tested done formating done
import os
import json
import re
from typing import List, Literal, Optional, Dict, Callable, Any
from functools import wraps
from datetime import datetime
from dotenv import load_dotenv
from pydantic import BaseModel
from openai import OpenAI
from playwright.sync_api import sync_playwright, Page, BrowserContext

print("Loading environment...")
load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
print("OpenAI client ready")

try:
    from babas_voice import init_butler, shutdown_butler, get_butler
    VOICE_OUTPUT_AVAILABLE = True
except ImportError:
    VOICE_OUTPUT_AVAILABLE = False
    def init_butler(): return None
    def shutdown_butler(): pass
    def get_butler(): return None

try:
    from babas_listen import get_voice_input, VoiceInputMode
    VOICE_INPUT_AVAILABLE = True
except ImportError:
    VOICE_INPUT_AVAILABLE = False
    def get_voice_input(): return None
    VoiceInputMode = None

try:
    from babas_gui import get_voice_gui, VoiceInputGUI
    VOICE_GUI_AVAILABLE = True
except ImportError:
    VOICE_GUI_AVAILABLE = False
    def get_voice_gui(): return None
    VoiceInputGUI = None

MODEL = "gpt-5.2"
MAX_RETRIES = 2

class PageAnalyzer:
    @staticmethod
    def get_interactive_elements(page: Page, max_per_type: int = 15) -> Dict:
        elements = {"inputs": [], "buttons": [], "links": [], "search_boxes": []}
        try:
            for inp in page.locator("input:visible, textarea:visible").all()[:max_per_type]:
                try:
                    info = {
                        "type": inp.get_attribute("type") or "text",
                        "name": inp.get_attribute("name"),
                        "id": inp.get_attribute("id"),
                        "placeholder": inp.get_attribute("placeholder"),
                        "aria-label": inp.get_attribute("aria-label"),
                        "class": inp.get_attribute("class"),
                    }
                    if info["id"]:
                        info["selector"] = f"#{info['id']}"
                    elif info["name"]:
                        info["selector"] = f"input[name='{info['name']}']"
                    elif info["placeholder"]:
                        info["selector"] = f"input[placeholder='{info['placeholder']}']"
                    elif info["aria-label"]:
                        info["selector"] = f"input[aria-label='{info['aria-label']}']"
                    else:
                        info["selector"] = None
                    elements["inputs"].append({k: v for k, v in info.items() if v})
                except:
                    continue
            search_selectors = [
                "input[type='search']", "input[name*='search']", "input[name*='query']",
                "input[placeholder*='Search']", "input[placeholder*='Buscar']",
                "input[aria-label*='Search']", "input[aria-label*='Buscar']",
                "[role='searchbox']", "[role='combobox']"
            ]
            for sel in search_selectors:
                try:
                    elem = page.locator(sel).first
                    if elem.is_visible(timeout=500):
                        elements["search_boxes"].append({
                            "selector": sel,
                            "placeholder": elem.get_attribute("placeholder"),
                            "aria-label": elem.get_attribute("aria-label")
                        })
                except:
                    continue
            for btn in page.locator("button:visible, [role='button']:visible").all()[:max_per_type]:
                try:
                    text = btn.inner_text().strip()[:50]
                    if text:
                        elements["buttons"].append({
                            "text": text,
                            "selector": f"button:has-text('{text[:20]}')" if text else None
                        })
                except:
                    continue
            for link in page.locator("a:visible").all()[:max_per_type]:
                try:
                    text = link.inner_text().strip()[:50]
                    href = link.get_attribute("href")
                    if text and href and not href.startswith("#"):
                        elements["links"].append({"text": text, "href": href[:100]})
                except:
                    continue
        except Exception as e:
            elements["error"] = str(e)
        return elements

    @staticmethod
    def get_page_summary(page: Page) -> Dict:
        try:
            return {
                "url": page.url,
                "title": page.title(),
                "elements": PageAnalyzer.get_interactive_elements(page)
            }
        except Exception as e:
            return {"error": str(e), "url": "unknown"}

class ActionRegistry:
    _actions: Dict[str, Callable] = {}
    _descriptions: Dict[str, str] = {}

    @classmethod
    def register(cls, action_name: str, description: str = ""):
        def decorator(func: Callable):
            @wraps(func)
            def wrapper(*args, **kwargs):
                return func(*args, **kwargs)
            cls._actions[action_name] = wrapper
            cls._descriptions[action_name] = description
            return wrapper
        return decorator

    @classmethod
    def get(cls, action_name: str) -> Optional[Callable]:
        return cls._actions.get(action_name)

    @classmethod
    def get_all_descriptions(cls) -> str:
        lines = []
        for name, desc in cls._descriptions.items():
            lines.append(f"- {name}: {desc}")
        return "\n".join(lines)

class TabManager:
    def __init__(self, context: BrowserContext):
        self.context = context
        self.tabs: Dict[str, Page] = {}
        self._tab_counter = 0

    def create_tab(self, name: str = None) -> tuple[str, Page]:
        self._tab_counter += 1
        tab_name = name or f"tab_{self._tab_counter}"
        page = self.context.new_page()
        self.tabs[tab_name] = page
        print(f"   New tab: '{tab_name}'")
        return tab_name, page

    def get_tab(self, name: str) -> Optional[Page]:
        return self.tabs.get(name)

    def close_tab(self, name: str) -> bool:
        if name in self.tabs and name != "main":
            self.tabs[name].close()
            del self.tabs[name]
            print(f"   Tab closed: '{name}'")
            return True
        return False

    def list_tabs(self) -> List[str]:
        return list(self.tabs.keys())

ActionType = Literal[
    "goto", "wait", "click", "type_text", "press_key", "screenshot", "scroll",
    "smart_search", "smart_click", "new_tab", "switch_tab", "close_tab",
    "search_youtube", "click_first_video", "open_gmail", "compose_email", "send_email",
    "open_whatsapp", "search_contact", "send_whatsapp_message", "analyze_page"
]

class Step(BaseModel):
    action: ActionType
    value: Optional[str] = None
    selector: Optional[str] = None
    ms: Optional[int] = None
    to: Optional[str] = None
    subject: Optional[str] = None
    body: Optional[str] = None
    tab_name: Optional[str] = None

class Plan(BaseModel):
    steps: List[Step]
    reasoning: Optional[str] = None

class ActionExecutor:
    def __init__(self, tab_manager: TabManager, initial_page: Page):
        self.tab_manager = tab_manager
        self.current_page = initial_page
        self.current_tab_name = "main"
        self.tab_manager.tabs["main"] = initial_page
        self.last_error = None
        self.last_context = None
        self._register_all_actions()

    def _register_all_actions(self):
        @ActionRegistry.register("goto", "Navigate to URL")
        def goto(executor, step):
            print(f"   Navigating to {step.value}...")
            executor.current_page.goto(step.value, wait_until="domcontentloaded", timeout=30000)
            executor.current_page.wait_for_timeout(2000)
            print(f"   Page loaded")

        @ActionRegistry.register("wait", "Wait X milliseconds")
        def wait(executor, step):
            ms = step.ms or 2000
            print(f"   Waiting {ms}ms...")
            executor.current_page.wait_for_timeout(ms)

        @ActionRegistry.register("click", "Click element by CSS selector")
        def click(executor, step):
            print(f"   Click: {step.selector}")
            executor.current_page.locator(step.selector).first.click(timeout=10000)
            print(f"   Done")

        @ActionRegistry.register("type_text", "Type text in field")
        def type_text(executor, step):
            print(f"   Typing: '{step.value}' in {step.selector}")
            executor.current_page.locator(step.selector).first.fill(step.value, timeout=10000)
            print(f"   Done")

        @ActionRegistry.register("press_key", "Press key")
        def press_key(executor, step):
            print(f"   Key: {step.value}")
            executor.current_page.keyboard.press(step.value)

        @ActionRegistry.register("screenshot", "Take screenshot")
        def screenshot(executor, step):
            filename = f"screenshot_{datetime.now().strftime('%H%M%S')}.png"
            executor.current_page.screenshot(path=filename)
            print(f"   Saved: {filename}")

        @ActionRegistry.register("scroll", "Scroll up/down")
        def scroll(executor, step):
            amount = step.ms or 500
            direction = 1 if step.value == "down" else -1
            executor.current_page.mouse.wheel(0, amount * direction)
            print(f"   Scroll {step.value}")

        @ActionRegistry.register("smart_search", "Auto-find search box and type")
        def smart_search(executor, step):
            print(f"   Searching for: '{step.value}'")
            search_selectors = [
                "input[type='search']", "input[name*='search']", "input[name*='query']", "input[name*='q']",
                "input[placeholder*='Search']", "input[placeholder*='Buscar']",
                "input[aria-label*='Search']", "input[aria-label*='Buscar']",
                "[role='searchbox']", "[role='combobox']",
                "input[id*='search']", "input[class*='search']", "textarea[name*='search']"
            ]
            for selector in search_selectors:
                try:
                    elem = executor.current_page.locator(selector).first
                    if elem.is_visible(timeout=1000):
                        elem.click()
                        elem.fill(step.value)
                        elem.press("Enter")
                        print(f"   Search done with: {selector}")
                        return
                except:
                    continue
            raise RuntimeError("Search box not found")

        @ActionRegistry.register("smart_click", "Click by visible text")
        def smart_click(executor, step):
            print(f"   Looking for: '{step.value}'")
            strategies = [
                f"text='{step.value}'", f"button:has-text('{step.value}')",
                f"a:has-text('{step.value}')", f"[role='button']:has-text('{step.value}')",
                f"*:has-text('{step.value}')"
            ]
            for strategy in strategies:
                try:
                    elem = executor.current_page.locator(strategy).first
                    if elem.is_visible(timeout=1000):
                        elem.click()
                        print(f"   Clicked: {strategy}")
                        return
                except:
                    continue
            raise RuntimeError(f"Element not found: {step.value}")

        @ActionRegistry.register("analyze_page", "Analyze page elements")
        def analyze_page(executor, step):
            print(f"   Analyzing page...")
            summary = PageAnalyzer.get_page_summary(executor.current_page)
            executor.last_context = summary
            print(f"   URL: {summary.get('url', 'unknown')}")
            print(f"   Title: {summary.get('title', 'unknown')}")
            if summary.get("elements", {}).get("search_boxes"):
                print(f"   Search boxes found: {len(summary['elements']['search_boxes'])}")
            return summary

        @ActionRegistry.register("new_tab", "Open new tab")
        def new_tab(executor, step):
            name, page = executor.tab_manager.create_tab(step.tab_name or step.value)
            executor.current_tab_name = name
            executor.current_page = page

        @ActionRegistry.register("switch_tab", "Switch to tab")
        def switch_tab(executor, step):
            tab_name = step.tab_name or step.value
            page = executor.tab_manager.get_tab(tab_name)
            if page:
                executor.current_tab_name = tab_name
                executor.current_page = page
                print(f"   Switched to: '{tab_name}'")
            else:
                raise ValueError(f"Tab '{tab_name}' not found")

        @ActionRegistry.register("close_tab", "Close tab")
        def close_tab(executor, step):
            tab_name = step.tab_name or step.value or executor.current_tab_name
            if executor.tab_manager.close_tab(tab_name):
                if tab_name == executor.current_tab_name:
                    executor.current_tab_name = "main"
                    executor.current_page = executor.tab_manager.get_tab("main")

        @ActionRegistry.register("search_youtube", "Search on YouTube")
        def search_youtube(executor, step):
            print(f"   YouTube: '{step.value}'")
            executor._close_popups()
            search = executor._find_element(['input[name="search_query"]', 'input#search', 'input[placeholder*="Search"]'])
            search.fill(step.value)
            search.press("Enter")
            executor.current_page.wait_for_timeout(2000)
            print(f"   Search done")

        @ActionRegistry.register("click_first_video", "Click first YouTube video")
        def click_first_video(executor, step):
            print(f"   Selecting video...")
            executor.current_page.wait_for_selector("ytd-video-renderer", timeout=10000)
            executor.current_page.locator("ytd-video-renderer a#thumbnail").first.click()
            print(f"   Video selected")

        @ActionRegistry.register("open_gmail", "Open Gmail")
        def open_gmail(executor, step):
            executor.current_page.goto("https://mail.google.com", wait_until="domcontentloaded")
            executor.current_page.wait_for_timeout(3000)
            print(f"   Gmail opened")

        @ActionRegistry.register("compose_email", "Compose email")
        def compose_email(executor, step):
            print(f"   Composing email...")
            executor.current_page.locator('div[role="button"]:has-text("Compose"), div[role="button"]:has-text("Redactar")').first.click()
            executor.current_page.wait_for_timeout(2000)
            executor.current_page.locator('input[name="to"]').fill(step.to)
            if step.subject:
                executor.current_page.locator('input[name="subjectbox"]').fill(step.subject)
            if step.body:
                executor.current_page.locator('div[role="textbox"]').last.fill(step.body)
            print(f"   Email ready")

        @ActionRegistry.register("send_email", "Send email")
        def send_email(executor, step):
            executor.current_page.locator('div[role="button"]:has-text("Send"), div[role="button"]:has-text("Enviar")').first.click()
            executor.current_page.wait_for_timeout(2000)
            print(f"   Email sent")

        @ActionRegistry.register("open_whatsapp", "Open WhatsApp Web")
        def open_whatsapp(executor, step):
            executor.current_page.goto("https://web.whatsapp.com", wait_until="domcontentloaded")
            print(f"   WhatsApp - Scan QR if needed")
            executor.current_page.wait_for_timeout(5000)

        @ActionRegistry.register("search_contact", "Search WhatsApp contact")
        def search_contact(executor, step):
            search = executor._find_element(['div[contenteditable="true"][data-tab="3"]', 'div[title*="Search"]'])
            search.fill(step.value)
            executor.current_page.wait_for_timeout(2000)
            executor.current_page.locator('div[role="listitem"]').first.click()

        @ActionRegistry.register("send_whatsapp_message", "Send WhatsApp message")
        def send_whatsapp_message(executor, step):
            msg_box = executor._find_element(['div[contenteditable="true"][data-tab="10"]', 'footer div[contenteditable="true"]'])
            msg_box.fill(step.body)
            msg_box.press("Enter")
            print(f"   Message sent")

    def _find_element(self, selectors: List[str]):
        for selector in selectors:
            try:
                elem = self.current_page.locator(selector).first
                if elem.is_visible(timeout=2000):
                    return elem
            except:
                continue
        raise RuntimeError("Element not found")

    def _close_popups(self):
        popups = ['button[aria-label*="Accept"]', 'button:has-text("Accept")']
        for sel in popups:
            try:
                if self.current_page.locator(sel).is_visible(timeout=1000):
                    self.current_page.locator(sel).first.click()
                    break
            except:
                continue

    def execute(self, step: Step) -> Any:
        action_func = ActionRegistry.get(step.action)
        if action_func:
            return action_func(self, step)
        else:
            print(f"   Unknown action: {step.action}")
            return None

    def get_current_context(self) -> Dict:
        return PageAnalyzer.get_page_summary(self.current_page)

SYSTEM_PROMPT = """You are an intelligent web automation agent. Create step-by-step plans.

## AVAILABLE ACTIONS:

### BASIC:
- {"action": "goto", "value": "URL"} - Navigate to URL
- {"action": "wait", "ms": 2000} - Wait milliseconds
- {"action": "click", "selector": "CSS"} - Click by selector
- {"action": "type_text", "selector": "CSS", "value": "text"} - Type in field
- {"action": "press_key", "value": "Enter"} - Press key
- {"action": "scroll", "value": "down|up"} - Scroll

### SMART (USE THESE!):
- {"action": "smart_search", "value": "text"} - Auto-find search box and type
- {"action": "smart_click", "value": "visible text"} - Click by visible text
- {"action": "analyze_page"} - Analyze page for elements

### TABS:
- {"action": "new_tab", "tab_name": "name"} - New tab
- {"action": "switch_tab", "tab_name": "name"} - Switch tab
- {"action": "close_tab"} - Close current tab

### YOUTUBE:
- {"action": "search_youtube", "value": "query"} - Search YouTube
- {"action": "click_first_video"} - Click first video

### GMAIL:
- {"action": "open_gmail"} - Open Gmail
- {"action": "compose_email", "to": "email", "subject": "subject", "body": "message"}
- {"action": "send_email"}

### WHATSAPP:
- {"action": "open_whatsapp"} - Open WhatsApp
- {"action": "search_contact", "value": "name"}
- {"action": "send_whatsapp_message", "body": "message"}

## RULES:
1. Use smart_search instead of type_text for searches
2. Use smart_click for clicks by visible text
3. Add waits after navigations (2000-3000ms)
4. For unknown sites, use analyze_page first if failing

## FORMAT:
{"steps": [...], "reasoning": "explanation"}

## EXAMPLE:
User: "search two sum on leetcode"
{
  "steps": [
    {"action": "goto", "value": "https://leetcode.com"},
    {"action": "wait", "ms": 3000},
    {"action": "smart_search", "value": "two sum"}
  ],
  "reasoning": "Navigate to LeetCode and use smart_search to find the search box"
}
"""

RECOVERY_PROMPT = """There was an ERROR executing the plan. Create a NEW PLAN to complete the task.

## ERROR:
{error}

## FAILED STEP:
{failed_step}

## CURRENT PAGE CONTEXT:
{context}

## ORIGINAL TASK:
{original_task}

## COMPLETED STEPS:
{completed_steps}

## INSTRUCTIONS:
1. Analyze the error and current context
2. Create a new plan to complete the task from where it failed
3. Use real selectors from the context
4. If there are search_boxes in context, use those selectors
5. You can use analyze_page for more info

Return ONLY valid JSON with the new plan.
"""

def create_plan(user_instruction: str, context: Dict = None) -> Plan:
    print(f"Analyzing: '{user_instruction}'")
    butler = get_butler()
    if butler:
        butler.planning()
    user_message = f"{user_instruction}\n\nRespond ONLY with valid JSON."
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_message}
    ]
    if context:
        messages.append({"role": "system", "content": f"Current context (json): {json.dumps(context)}"})
    response = client.chat.completions.create(
        model=MODEL,
        messages=messages,
        response_format={"type": "json_object"}
    )
    data = json.loads(response.choices[0].message.content)
    plan = Plan(**data)
    print(f"Plan: {len(plan.steps)} steps")
    if plan.reasoning:
        print(f"{plan.reasoning}\n")
    return plan

def create_recovery_plan(original_task: str, error: str, failed_step: Step, completed_steps: List[Step], context: Dict) -> Plan:
    print(f"\nAI analyzing error...")
    prompt = RECOVERY_PROMPT.format(
        error=error,
        failed_step=failed_step.model_dump_json(),
        context=json.dumps(context, indent=2),
        original_task=original_task,
        completed_steps=[s.model_dump() for s in completed_steps]
    )
    prompt += "\n\nRespond ONLY with valid JSON."
    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt}
        ],
        response_format={"type": "json_object"}
    )
    data = json.loads(response.choices[0].message.content)
    plan = Plan(**data)
    print(f"Recovery plan: {len(plan.steps)} steps")
    if plan.reasoning:
        print(f"{plan.reasoning}")
    return plan

def run_plan_with_recovery(executor: ActionExecutor, plan: Plan, original_task: str, max_retries: int = MAX_RETRIES, voice_gui = None) -> bool:
    completed_steps = []
    current_plan = plan
    retry_count = 0
    butler = get_butler()
    stopped_by_user = False

    while retry_count <= max_retries:
        print(f"\nExecuting {len(current_plan.steps)} steps...\n")
        if butler and retry_count == 0:
            butler.executing()

        for i, step in enumerate(current_plan.steps):
            if voice_gui and voice_gui.is_stop_requested():
                print(f"\nSTOPPED by user")
                if butler:
                    butler.say("Understood sir. Stopping execution.")
                stopped_by_user = True
                break

            print(f"{'='*50}")
            tab_info = f" [{executor.current_tab_name}]" if len(executor.tab_manager.tabs) > 1 else ""
            print(f"[{i+1}/{len(current_plan.steps)}]{tab_info} {step.action}")
            if step.value:
                print(f"   -> {step.value}")
            if step.selector:
                print(f"   -> selector: {step.selector}")

            try:
                executor.execute(step)
                completed_steps.append(step)
                if voice_gui and voice_gui.is_stop_requested():
                    print(f"\nSTOPPED by user")
                    if butler:
                        butler.say("Understood sir. Stopping execution.")
                    stopped_by_user = True
                    break
                total_steps = len(current_plan.steps)
                current_step = i + 1
                if butler and total_steps >= 4:
                    if current_step == total_steps // 2:
                        butler.say("Making good progress sir.")
            except Exception as e:
                error_msg = f"{type(e).__name__}: {str(e)}"
                print(f"   ERROR: {error_msg}")
                screenshot_path = save_debug_screenshot(executor.current_page, f"error_{retry_count}_{i}")
                if screenshot_path:
                    print(f"   Debug screenshot: {os.path.basename(screenshot_path)}")
                if retry_count < max_retries:
                    print(f"\nAuto-recovery attempt ({retry_count + 1}/{max_retries})...")
                    if butler:
                        butler.say("One moment sir. I found an obstacle but I have an alternative plan.")
                    try:
                        context = executor.get_current_context()
                    except:
                        context = {"error": "Could not get context"}
                    try:
                        recovery_plan = create_recovery_plan(
                            original_task=original_task,
                            error=error_msg,
                            failed_step=step,
                            completed_steps=completed_steps,
                            context=context
                        )
                        print("\nNew plan:")
                        for j, s in enumerate(recovery_plan.steps, 1):
                            print(f"   {j}. {s.action}" + (f" -> {s.value}" if s.value else ""))
                        current_plan = recovery_plan
                        retry_count += 1
                        break
                    except Exception as recovery_error:
                        print(f"   Error creating recovery plan: {recovery_error}")
                        retry_count += 1
                        continue
                else:
                    print(f"\nMax retries reached ({max_retries})")
                    print("   Could not complete this task after several attempts.")
                    if butler:
                        butler.say("I'm very sorry sir. I couldn't complete this task despite my attempts. Ready for your next command.")
                    return False
        else:
            print(f"\n{'='*50}")
            print(f"Plan completed successfully!")
            if butler:
                butler.success()
            return True
        if stopped_by_user:
            return False
    return False

import socket
import subprocess
import platform
import shutil
import tempfile
import time

DEBUG_PORTS = [9222, 9223, 9224, 9225]
TEMP_DEBUG_DIR = None

def get_temp_debug_dir() -> str:
    global TEMP_DEBUG_DIR
    if TEMP_DEBUG_DIR is None or not os.path.exists(TEMP_DEBUG_DIR):
        TEMP_DEBUG_DIR = tempfile.mkdtemp(prefix="babas_debug_")
    return TEMP_DEBUG_DIR

def cleanup_temp_dir():
    global TEMP_DEBUG_DIR
    if TEMP_DEBUG_DIR and os.path.exists(TEMP_DEBUG_DIR):
        try:
            shutil.rmtree(TEMP_DEBUG_DIR, ignore_errors=True)
            print(f"   Temp files cleaned")
        except:
            pass
        TEMP_DEBUG_DIR = None

def save_debug_screenshot(page, name: str = "error") -> str:
    debug_dir = get_temp_debug_dir()
    filename = os.path.join(debug_dir, f"{name}_{datetime.now().strftime('%H%M%S')}.png")
    try:
        page.screenshot(path=filename)
        return filename
    except:
        return ""

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

def run_hacker_mode(page, butler=None):
    import random
    import time as _time
    print("\n" + "="*60)
    print("INITIATING INTRUSION PROTOCOL...")
    print("="*60)
    if butler and butler.enabled:
        butler.say("Initiating intrusion protocol sir. Accessing the mainframe.")
    try:
        _time.sleep(0.5)
        page.goto("https://hackertyper.com/", wait_until="domcontentloaded")
        _time.sleep(1)
        try:
            page.keyboard.press("Escape")
            _time.sleep(0.3)
        except:
            pass
        print("\nHACKING...")
        for i in range(random.randint(150, 250)):
            key = random.choice("asdfghjklqwertyuiopzxcvbnm1234567890")
            page.keyboard.press(key)
            delay = random.uniform(0.01, 0.05)
            _time.sleep(delay)
            if i == 30:
                print(f"   {random.choice(HACKER_PHRASES)}")
            elif i == 70:
                print(f"   {random.choice(HACKER_PHRASES)}")
            elif i == 120:
                print(f"   {random.choice(HACKER_PHRASES)}")
            elif i == 180:
                print(f"   {random.choice(HACKER_PHRASES)}")
        _time.sleep(0.5)
        print("\n" + "="*60)
        success_phrase = random.choice(HACKER_SUCCESS_PHRASES)
        print(f"   {success_phrase}")
        print("="*60 + "\n")
        if butler and butler.enabled:
            butler.say(success_phrase)
        _time.sleep(2)
    except Exception as e:
        print(f"   Error in hacker mode: {e}")
        if butler and butler.enabled:
            butler.say("We've been detected sir. Aborting mission.")

def check_port(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(('localhost', port)) == 0

def scan_debug_ports() -> Optional[int]:
    for port in DEBUG_PORTS:
        if check_port(port):
            return port
    return None

def get_browser_info() -> Dict[str, Dict]:
    system = platform.system().lower()
    if system == "windows":
        chrome_paths = [
            os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]
        brave_paths = [
            os.path.expandvars(r"%PROGRAMFILES%\BraveSoftware\Brave-Browser\Application\brave.exe"),
            os.path.expandvars(r"%PROGRAMFILES(X86)%\BraveSoftware\Brave-Browser\Application\brave.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"),
        ]
        edge_paths = [
            os.path.expandvars(r"%PROGRAMFILES%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe"),
        ]
        chrome_path = next((p for p in chrome_paths if os.path.exists(p)), None)
        brave_path = next((p for p in brave_paths if os.path.exists(p)), None)
        edge_path = next((p for p in edge_paths if os.path.exists(p)), None)
        chrome_profile = os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data")
        brave_profile = os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data")
        edge_profile = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\User Data")
        return {
            "brave": {"path": brave_path, "profile": brave_profile, "port": 9222},
            "chrome": {"path": chrome_path, "profile": chrome_profile, "port": 9223},
            "edge": {"path": edge_path, "profile": edge_profile, "port": 9224},
        }
    else:
        return {
            "brave": {
                "path": "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser" if system == "darwin" else "/usr/bin/brave-browser",
                "profile": os.path.expanduser("~/.config/BraveSoftware/Brave-Browser"),
                "port": 9222
            },
            "chrome": {
                "path": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" if system == "darwin" else "/usr/bin/google-chrome",
                "profile": os.path.expanduser("~/.config/google-chrome"),
                "port": 9223
            },
        }

def get_available_browsers() -> List[str]:
    browsers = get_browser_info()
    available = []
    for name in ["brave", "chrome", "edge"]:
        info = browsers.get(name, {})
        if info.get("path") and os.path.exists(info["path"]):
            available.append(name)
    return available

def get_preferred_browser() -> Optional[str]:
    available = get_available_browsers()
    return available[0] if available else None

def try_connect_to_browser(playwright) -> tuple[Optional[Any], Optional[int], Optional[str]]:
    port = scan_debug_ports()
    if port:
        try:
            browser = playwright.chromium.connect_over_cdp(f"http://localhost:{port}")
            return browser, port, "detected"
        except:
            pass
    return None, None, None

def is_profile_locked(profile_path: str) -> bool:
    lock_files = [
        os.path.join(profile_path, "lockfile"),
        os.path.join(profile_path, "SingletonLock"),
        os.path.join(profile_path, "SingletonSocket"),
    ]
    for lock in lock_files:
        if os.path.exists(lock):
            return True
    return False

def get_babas_profile_path(browser_name: str) -> str:
    system = platform.system().lower()
    if system == "windows":
        return os.path.expandvars(rf"%LOCALAPPDATA%\babas-{browser_name}-profile")
    else:
        return os.path.expanduser(f"~/.babas-{browser_name}-profile")

def launch_browser_with_debug(browser_name: str) -> tuple[bool, int, str]:
    browsers = get_browser_info()
    info = browsers.get(browser_name)
    if not info or not info.get("path") or not os.path.exists(info["path"]):
        return False, 0, "Browser not found"
    port = info.get("port", 9222)
    system = platform.system().lower()
    profile = get_babas_profile_path(browser_name)
    print(f"   BABAS profile: {os.path.basename(profile)}")
    args = [
        info["path"],
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
    ]
    try:
        if system == "windows":
            subprocess.Popen(args, creationflags=subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return True, port, "ok"
    except Exception as e:
        return False, 0, str(e)

def try_launch_and_connect(playwright, browser_name: str) -> tuple[bool, Any, int]:
    print(f"\nLaunching {browser_name.capitalize()}...")
    success, port, msg = launch_browser_with_debug(browser_name)
    if not success:
        print(f"   Error: {msg}")
        return False, None, 0
    print(f"   Connecting...")
    for i in range(30):
        time.sleep(0.5)
        if check_port(port):
            time.sleep(1)
            try:
                browser = playwright.chromium.connect_over_cdp(f"http://localhost:{port}")
                contexts = browser.contexts
                pages_count = len(contexts[0].pages) if contexts else 0
                print(f"   Connected! ({pages_count} tabs)")
                return True, browser, port
            except:
                continue
    print("   Could not connect")
    return False, None, 0

def select_session_mode(playwright) -> tuple[str, dict, Any]:
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
            browser = playwright.chromium.connect_over_cdp(f"http://localhost:{port}")
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
    choice = input(f"\nOption [ENTER = {preferred.capitalize() if preferred else 'Chrome'}]: ").strip().lower()
    if choice == "n":
        return "new", {}, None
    if choice == "" and preferred:
        browser_name = preferred
    else:
        try:
            idx = int(choice) - 1
            browser_name = available[idx] if 0 <= idx < len(available) else preferred or "chrome"
        except:
            browser_name = preferred or "chrome"
    success, browser, port = try_launch_and_connect(playwright, browser_name)
    if success:
        return "connect", {"port": port, "browser": browser_name}, browser
    for fallback in available:
        if fallback != browser_name:
            print(f"\nTrying {fallback.capitalize()}...")
            success, browser, port = try_launch_and_connect(playwright, fallback)
            if success:
                return "connect", {"port": port, "browser": fallback}, browser
    print("\nCould not connect to any browser.")
    print("   Using Playwright's built-in browser...")
    return "saved", {}, None

def show_help():
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

def main():
    print("\n" + "="*60)
    print("STARTING BABAS SYSTEM...")
    print("="*60)
    butler = None
    if VOICE_OUTPUT_AVAILABLE:
        butler = init_butler()
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
        mode_choice = input("\n>>> Option (1/2) [1]: ").strip() or "1"
        if mode_choice == "1":
            print("\nTEXT mode selected")
            if butler and butler.enabled:
                butler.say("Text mode activated sir.")
            break
        elif mode_choice == "2":
            if VOICE_GUI_AVAILABLE:
                use_voice_mode = True
                voice_gui = get_voice_gui()
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
        with sync_playwright() as p:
            mode, config, browser = select_session_mode(p)
            context = None
            is_connected = False
            if mode == "connect" and browser:
                is_connected = True
                contexts = browser.contexts
                if contexts:
                    context = contexts[0]
                else:
                    context = browser.new_context()
            if context is None:
                if mode == "saved":
                    profile_path = os.path.join(os.path.dirname(__file__), "babas-profile")
                    print(f"\nUsing saved BABAS session...")
                else:
                    profile_path = os.path.join(get_temp_debug_dir(), "temp-profile")
                    print(f"\nTemporary session")
                context = p.chromium.launch_persistent_context(
                    user_data_dir=profile_path,
                    headless=False,
                    args=['--disable-blink-features=AutomationControlled', '--start-maximized'],
                    viewport=None,
                    no_viewport=True
                )
            if is_connected:
                print("\nCreating new tab for BABAS...")
                initial_page = context.new_page()
                print(f"   Tab created (your other tabs won't be touched)")
            else:
                if context.pages:
                    initial_page = context.pages[0]
                else:
                    initial_page = context.new_page()
            tab_manager = TabManager(context)
            executor = ActionExecutor(tab_manager, initial_page)
            print("\n" + "="*60)
            print("READY!")
            print("="*60)
            if use_voice_mode and voice_gui:
                print("\nOpening voice control panel...")
                voice_gui.open(butler)
                time.sleep(1)
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
                        voice_command = voice_gui.get_voice_command(timeout=0.5)
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
                        user_input = input(">>> ").strip()
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
                        for name in tab_manager.list_tabs():
                            page = tab_manager.get_tab(name)
                            marker = "->" if name == executor.current_tab_name else "  "
                            try:
                                title = page.title()[:40] or page.url[:40]
                                print(f"   {marker} [{name}] {title}")
                            except:
                                print(f"   {marker} [{name}] (error)")
                        continue
                    if cmd == "new":
                        name, page = tab_manager.create_tab()
                        executor.current_tab_name = name
                        executor.current_page = page
                        print(f"   New tab: {name}")
                        continue
                    if cmd in ["screenshot", "ss"]:
                        filename = f"screenshot_{datetime.now().strftime('%H%M%S')}.png"
                        executor.current_page.screenshot(path=filename)
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
                        run_hacker_mode(executor.current_page, butler)
                        if use_voice_mode and voice_gui and voice_gui.is_open:
                            voice_gui.set_status("Ready", "#00ff88")
                            voice_gui.show_message("Hack complete\n\nRecord another command.")
                        continue
                    print()
                    try:
                        if use_voice_mode and voice_gui and voice_gui.is_open:
                            voice_gui.set_status("Creating plan...", "#00d4ff")
                        plan = create_plan(user_input)
                        plan_lines = []
                        for i, step in enumerate(plan.steps, 1):
                            details = step.value or step.selector or step.tab_name or ""
                            if details:
                                details = f" -> {details[:35]}"
                            plan_lines.append(f"{i}. {step.action}{details}")
                        plan_text = "\n".join(plan_lines)
                        print("\nPlan:")
                        for line in plan_lines:
                            print(f"   {line}")
                        confirmed = False
                        if use_voice_mode and voice_gui and voice_gui.is_open:
                            confirm_result = voice_gui.show_plan_and_confirm(
                                f"Plan for:\n\"{user_input[:50]}\"\n\n" + plan_text,
                                timeout=120
                            )
                            confirmed = confirm_result == True
                            if not confirmed:
                                print("Cancelled from GUI")
                                voice_gui.show_message("Cancelled.\n\nRecord another command.")
                                continue
                        else:
                            confirm = input("\nExecute? (y/n) [y]: ").strip().lower()
                            if confirm == 'n':
                                print("Cancelled")
                                continue
                            confirmed = True
                        if not confirmed:
                            continue
                        if use_voice_mode and voice_gui and voice_gui.is_open:
                            voice_gui.set_status("Executing...", "#ff9f43")
                            voice_gui.show_message("Executing...\n(Press STOP to cancel)\n\n" + plan_text)
                            voice_gui.start_execution()
                        try:
                            gui_for_stop = voice_gui if (use_voice_mode and voice_gui and voice_gui.is_open) else None
                            success = run_plan_with_recovery(executor, plan, user_input, voice_gui=gui_for_stop)
                        finally:
                            if use_voice_mode and voice_gui and voice_gui.is_open:
                                voice_gui.end_execution()
                                voice_gui.clear_stop()
                        if success:
                            print("\nDone!")
                            if use_voice_mode and voice_gui and voice_gui.is_open:
                                voice_gui.set_status("Complete", "#00ff88")
                                voice_gui.show_message("Complete!\n\nRecord another command.")
                        else:
                            if use_voice_mode and voice_gui and voice_gui.is_open:
                                voice_gui.set_status("Stopped", "#ff9f43")
                            else:
                                print("\nPartially completed")
                    except Exception as e:
                        print(f"\nError: {e}")
                        if use_voice_mode and voice_gui and voice_gui.is_open:
                            voice_gui.set_status("Error", "#ff4757")
                            voice_gui.show_message(f"Error: {e}\n\nRecord another command.")
                        continue
                except KeyboardInterrupt:
                    print("\n\n(Ctrl+C - type 'exit' to close)")
                    continue
            print("\nClosing session...")
            tabs_closed = 0
            for name, page in list(tab_manager.tabs.items()):
                try:
                    if not page.is_closed():
                        page.close()
                        tabs_closed += 1
                except:
                    pass
            if tabs_closed > 0:
                print(f"   {tabs_closed} tab(s) closed")
            if is_connected:
                print("   Your browser is still open (without BABAS tabs)")
                try:
                    browser.close()
                except:
                    pass
            else:
                try:
                    context.close()
                    print("   Browser closed")
                except:
                    pass
            cleanup_temp_dir()
            if mode == "new" and profile_path:
                try:
                    shutil.rmtree(profile_path, ignore_errors=True)
                    print("   Temp profile deleted")
                except:
                    pass
            print("   Session ended")
            shutdown_butler()
    except KeyboardInterrupt:
        print("\nCancelled")
        cleanup_temp_dir()
        shutdown_butler()
    except Exception as e:
        print(f"\nFatal error: {e}")
        cleanup_temp_dir()
        shutdown_butler()
        raise

if __name__ == "__main__":
    main()
