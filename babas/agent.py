import json
import os
from typing import Any, Dict, List, Optional, Protocol

from dotenv import load_dotenv
from openai import AsyncOpenAI

from babas.debug import save_debug_screenshot
from babas.prompts import RECOVERY_PROMPT, SYSTEM_PROMPT
from babas.schemas import Plan, Step
from babas.voice import get_butler

MODEL = "gpt-5.2"
MAX_RETRIES = 2

_client: Optional[AsyncOpenAI] = None


class PlanExecutor(Protocol):
    @property
    def driver(self) -> Any: ...

    @property
    def page(self) -> Any: ...

    @property
    def current_tab_name(self) -> str: ...

    async def execute(self, step: Step) -> Any: ...

    async def get_current_context(self) -> Dict[str, Any]: ...


def get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        load_dotenv()
        _client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        print("OpenAI client ready")
    return _client


async def create_plan(user_instruction: str, context: Optional[Dict[str, Any]] = None) -> Plan:
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
    response = await get_client().chat.completions.create(
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


async def create_recovery_plan(original_task: str, error: str, failed_step: Step, completed_steps: List[Step], context: Dict[str, Any]) -> Plan:
    print(f"\nAI analyzing error...")
    prompt = RECOVERY_PROMPT.format(
        error=error,
        failed_step=failed_step.model_dump_json(),
        context=json.dumps(context, indent=2),
        original_task=original_task,
        completed_steps=[s.model_dump() for s in completed_steps]
    )
    prompt += "\n\nRespond ONLY with valid JSON."
    response = await get_client().chat.completions.create(
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


async def run_plan_with_recovery(executor: PlanExecutor, plan: Plan, original_task: str, max_retries: int = MAX_RETRIES, voice_gui: Any = None) -> bool:
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
            tab_info = f" [{executor.current_tab_name}]" if len(executor.driver.list_tabs()) > 1 else ""
            print(f"[{i+1}/{len(current_plan.steps)}]{tab_info} {step.action}")
            if step.value:
                print(f"   -> {step.value}")
            if step.selector:
                print(f"   -> selector: {step.selector}")

            try:
                await executor.execute(step)
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
                screenshot_path = await save_debug_screenshot(executor.page, f"error_{retry_count}_{i}")
                if screenshot_path:
                    print(f"   Debug screenshot: {os.path.basename(screenshot_path)}")
                if retry_count < max_retries:
                    print(f"\nAuto-recovery attempt ({retry_count + 1}/{max_retries})...")
                    if butler:
                        butler.say("One moment sir. I found an obstacle but I have an alternative plan.")
                    try:
                        context = await executor.get_current_context()
                    except Exception:
                        context = {"error": "Could not get context"}
                    try:
                        recovery_plan = await create_recovery_plan(
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
