import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional, Set, Union

from pydantic import ValidationError

from babas.channels import UserChannel
from babas.drivers.base import DriverError, ErrorCode, PageState, clip_text
from babas.gate import ActionDenied, HandoffRequired, SafeDriver
from babas.llm import LLMClient, LLMError
from babas.prompts import HISTORY_DETAIL_CHARS, LAST_ERROR_CHARS, build_system_prompt, build_turn_message
from babas.safety import CAPTCHA_TEXT, redact
from babas.schemas import (
    AskUserStep,
    CloseTabStep,
    ComposeEmailStep,
    DoneStep,
    GotoStep,
    NewTabStep,
    Plan,
    PressKeyStep,
    ReadTextStep,
    ScrollStep,
    SearchContactStep,
    SearchYoutubeStep,
    SelectOptionStep,
    SendWhatsappMessageStep,
    SmartClickStep,
    SmartSearchStep,
    StepBase,
    SwitchTabStep,
    Targeted,
    TypeTextStep,
    WaitStep,
    changes_page,
    plan_json_schema,
)

log = logging.getLogger("babas.agent")

DENIED_ANSWER = "De acuerdo, no lo hice."
FAILED_ANSWER = "No pude completar la tarea. Puede intentarlo de nuevo o pedirlo de otra manera."
MAX_STEPS_ANSWER = "Llegué al límite de pasos y no pude terminar la tarea. Puede pedirme que continúe."
STOPPED_ANSWER = "Detuve la tarea."
UNREADABLE_TEXT = "BABAS cannot read this page. Use goto to open a website."

OBSERVE_RETRY_DELAY_S = 0.5
SUMMARY_VALUE_CHARS = 60
SUMMARY_URL_CHARS = 120

EventCallback = Callable[[str, Dict[str, Any]], Awaitable[None]]
_BATCHED = (TypeTextStep, SelectOptionStep)


@dataclass
class HistoryItem:
    summary: str
    outcome: Literal["ok", "error", "denied", "answer", "info"]
    detail: str = ""


@dataclass
class RunResult:
    status: Literal["done", "failed", "stopped", "max_steps"]
    answer: str = ""
    steps: int = 0


@dataclass
class _Run:
    task: str
    history: List[HistoryItem] = field(default_factory=list)
    failures: int = 0
    last_error: str = ""
    steps_done: int = 0
    turn: int = 0
    handed_off: Set[str] = field(default_factory=set)


def _clean(text: Optional[str], limit: int) -> str:
    return clip_text(redact(text or ""), limit)


def _error_text(code: str, message: str) -> str:
    return _clean(f"{code}: {message}" if message else code, LAST_ERROR_CHARS)


def _quoted(text: str) -> str:
    return '"' + text.replace('"', "'") + '"'


def _target_text(step: Targeted) -> str:
    if step.index is not None:
        return f"[{step.index}]"
    if step.selector:
        return "selector=" + _quoted(_clean(step.selector, SUMMARY_VALUE_CHARS))
    if step.text:
        return "text=" + _quoted(_clean(step.text, SUMMARY_VALUE_CHARS))
    return ""


def summarize_step(step: StepBase) -> str:
    name = str(getattr(step, "action", type(step).__name__))
    parts: List[str] = [name]
    if isinstance(step, GotoStep):
        parts.append(_clean(step.value, SUMMARY_URL_CHARS))
    elif isinstance(step, WaitStep):
        parts.append(f"{step.ms} ms")
    elif isinstance(step, ScrollStep):
        parts.extend([step.value, str(step.ms)])
    elif isinstance(step, PressKeyStep):
        parts.extend([_clean(step.value, SUMMARY_VALUE_CHARS), _target_text(step)])
    elif isinstance(step, (TypeTextStep, SelectOptionStep)):
        parts.extend([_target_text(step), _quoted(_clean(step.value, SUMMARY_VALUE_CHARS))])
    elif isinstance(step, Targeted):
        parts.append(_target_text(step))
    elif isinstance(step, (SmartSearchStep, SmartClickStep, SearchYoutubeStep, SearchContactStep, AskUserStep)):
        parts.append(_quoted(_clean(step.value, SUMMARY_VALUE_CHARS)))
    elif isinstance(step, (NewTabStep, SwitchTabStep, CloseTabStep)):
        parts.append(_clean(step.tab_name or step.value or "", SUMMARY_VALUE_CHARS))
    elif isinstance(step, ComposeEmailStep):
        parts.append("to=" + _clean(step.to, SUMMARY_VALUE_CHARS))
        if step.subject:
            parts.append("subject=" + _quoted(_clean(step.subject, SUMMARY_VALUE_CHARS)))
    elif isinstance(step, SendWhatsappMessageStep):
        parts.append(f"({len(step.body)} characters)")
    return redact(" ".join(part for part in parts if part))


def _validation_summary(exc: ValidationError) -> str:
    # Built from loc + msg only: str(exc) echoes the invalid input, which may hold typed personal data.
    problems: List[str] = []
    for item in exc.errors(include_url=False, include_context=False, include_input=False)[:5]:
        where = ".".join(str(part) for part in item.get("loc", ()))
        message = str(item.get("msg", "invalid"))
        problems.append(f"{where}: {message}" if where else message)
    return _clean("; ".join(problems) or "the plan does not match the schema", LAST_ERROR_CHARS)


class Agent:
    def __init__(
        self,
        driver: SafeDriver,
        llm: LLMClient,
        channel: UserChannel,
        *,
        max_steps: int = 30,
        max_retries: int = 2,
        language: str = "en",
        on_event: Optional[EventCallback] = None,
        executor: Optional[Any] = None,
    ):
        self.driver = driver
        self.llm = llm
        self.channel = channel
        self.max_steps = max_steps
        self.max_retries = max_retries
        self.language = language
        self.on_event = on_event
        if executor is None:
            from babas.actions import ActionExecutor

            executor = ActionExecutor(driver)
        self.executor = executor
        self._system = build_system_prompt(language)
        self._schema = plan_json_schema()

    async def run(self, task: str) -> RunResult:
        run = _Run(task=task)
        while True:
            observed = await self._observe(run)
            if isinstance(observed, RunResult):
                return observed
            if observed is None:
                continue
            state = observed
            if state.has_captcha:
                key = state.url.split("#", 1)[0]
                if key not in run.handed_off:
                    run.handed_off.add(key)
                    if await self._ask(CAPTCHA_TEXT, "handoff") is None:
                        return await self._stopped(run)
                    run.history.append(HistoryItem("handoff captcha", "info", "the user says it is done"))
                    continue
            planned = await self._plan(run, state)
            if isinstance(planned, RunResult):
                return planned
            if planned is None:
                continue
            run.turn += 1
            summaries = [summarize_step(step) for step in planned.steps]
            log.debug("turn %d: %d planned steps", run.turn, len(summaries))
            await self._emit("plan", {"turn": run.turn, "steps": summaries})
            finished = await self._act(run, state, planned)
            if finished is not None:
                return finished

    async def _log(self, kind: str, text: str, ok: Optional[bool] = None) -> None:
        try:
            await self.channel.log(kind, text, ok)
        except Exception as exc:
            log.debug("channel.log failed: %s", type(exc).__name__)

    async def _ask(self, question: str, kind: str) -> Optional[str]:
        try:
            return await self.channel.ask(question, kind)
        except Exception as exc:
            log.warning("channel.ask failed: %s", type(exc).__name__)
            return None

    async def _emit(self, name: str, data: Dict[str, Any]) -> None:
        if self.on_event is None:
            return
        try:
            await self.on_event(name, data)
        except Exception as exc:
            log.warning("on_event(%s) failed: %s", name, type(exc).__name__)

    async def _stopped(self, run: _Run) -> RunResult:
        await self._log("info", STOPPED_ANSWER)
        return RunResult("stopped", STOPPED_ANSWER, run.steps_done)

    async def _denied(self, run: _Run, summary: str, exc: ActionDenied) -> RunResult:
        log.info("action denied: %s", exc.reason)
        run.history.append(HistoryItem(summary, "denied", _clean(exc.reason, HISTORY_DETAIL_CHARS)))
        await self._log("info", DENIED_ANSWER)
        return RunResult("stopped", DENIED_ANSWER, run.steps_done)

    async def _failed(self, run: _Run) -> RunResult:
        log.warning("run failed after %d consecutive failures: %s", run.failures, run.last_error)
        await self._log("error", FAILED_ANSWER)
        return RunResult("failed", FAILED_ANSWER, run.steps_done)

    async def _limit_reached(self, run: _Run) -> Optional[RunResult]:
        if run.steps_done < self.max_steps:
            return None
        log.info("step limit reached: %d", run.steps_done)
        await self._log("error", MAX_STEPS_ANSWER)
        return RunResult("max_steps", MAX_STEPS_ANSWER, run.steps_done)

    async def _observe(self, run: _Run) -> Union[PageState, RunResult, None]:
        try:
            return await self.driver.get_page_state()
        except ActionDenied as exc:
            return await self._denied(run, "observe page", exc)
        except DriverError as exc:
            if exc.code == ErrorCode.HALTED:
                return await self._stopped(run)
            if exc.code == ErrorCode.UNSCRIPTABLE:
                return await self._unreadable(exc)
            error = _error_text(exc.code, exc.message)
        except Exception as exc:
            log.warning("observation raised %s", type(exc).__name__)
            error = _error_text(type(exc).__name__, str(exc))
        run.failures += 1
        run.last_error = error
        run.history.append(HistoryItem("observe page", "error", error))
        if run.failures > self.max_retries:
            return await self._failed(run)
        await asyncio.sleep(OBSERVE_RETRY_DELAY_S)
        return None

    async def _unreadable(self, exc: DriverError) -> PageState:
        raw = exc.data.get("url")
        url = raw if isinstance(raw, str) else ""
        if not url:
            try:
                url = await self.driver.current_url()
            except Exception as err:
                log.debug("current_url failed: %s", type(err).__name__)
        return PageState(snapshot_id="none", url=url, text=UNREADABLE_TEXT)

    async def _plan(self, run: _Run, state: PageState) -> Union[Plan, RunResult, None]:
        message = build_turn_message(run.task, state, run.history, run.last_error, self.max_steps - run.steps_done)
        kind = "InvalidPlan"
        problem = ""
        for _ in range(2):
            prompt = message
            if problem:
                prompt = f"{message}\n\nYour previous answer was invalid: {problem.rstrip('. ')}. Reply again with ONLY valid JSON."
            try:
                raw = await self.llm.complete_json(self._system, prompt, self._schema)
                return Plan.model_validate(raw)
            except LLMError as exc:
                kind, problem = "LLMError", _clean(str(exc), LAST_ERROR_CHARS)
            except ValidationError as exc:
                kind, problem = "InvalidPlan", _validation_summary(exc)
            except Exception as exc:
                log.warning("planning raised %s", type(exc).__name__)
                return await self._plan_failed(run, _error_text(type(exc).__name__, str(exc)))
        return await self._plan_failed(run, f"{kind}: {problem}")

    async def _plan_failed(self, run: _Run, error: str) -> Optional[RunResult]:
        run.failures += 1
        run.last_error = _clean(error, LAST_ERROR_CHARS)
        run.history.append(HistoryItem("plan", "error", run.last_error))
        if run.failures > self.max_retries:
            return await self._failed(run)
        return None

    async def _act(self, run: _Run, state: PageState, plan: Plan) -> Optional[RunResult]:
        steps = plan.steps
        confirmations: Dict[int, Optional[str]] = {}
        for position, step in enumerate(steps):
            if isinstance(step, DoneStep):
                await self._log("result", step.value)
                return RunResult("done" if step.success else "failed", step.value, run.steps_done)
            if isinstance(step, AskUserStep):
                return await self._ask_step(run, step)
            outcome = await self._attempt(run, state, steps, position, confirmations)
            if isinstance(outcome, RunResult):
                return outcome
            if not outcome:
                return None
        return None

    async def _ask_step(self, run: _Run, step: AskUserStep) -> Optional[RunResult]:
        if step.explanation:
            await self._log("assistant", redact(step.explanation))
        answer = await self._ask(step.value, step.kind)
        if answer is None:
            return await self._stopped(run)
        if step.kind == "handoff":
            detail = "the user says it is done"
        else:
            detail = _clean(answer, HISTORY_DETAIL_CHARS) or "(empty answer)"
        run.history.append(HistoryItem(summarize_step(step), "answer", detail))
        return None

    async def _confirm_run(
        self, state: PageState, steps: List[Any], position: int, confirmations: Dict[int, Optional[str]]
    ) -> None:
        end = position
        while end < len(steps) and isinstance(steps[end], _BATCHED):
            end += 1
        confirmation: Optional[str] = None
        batch = getattr(self.driver, "confirm_batch", None)
        if batch is not None:
            items = []
            for step in steps[position:end]:
                target = step.to_target(state.snapshot_id)
                if isinstance(step, TypeTextStep):
                    items.append(("type", target, {"text": step.value}))
                else:
                    items.append(("select", target, {"value": step.value}))
            confirmation = await batch(items, explanation=steps[position].explanation or "")
        for index in range(position, end):
            confirmations[index] = confirmation

    async def _attempt(
        self,
        run: _Run,
        state: PageState,
        steps: List[Any],
        position: int,
        confirmations: Dict[int, Optional[str]],
    ) -> Union[RunResult, bool]:
        step = steps[position]
        summary = summarize_step(step)
        if step.explanation:
            await self._log("assistant", redact(step.explanation))
        try:
            if isinstance(step, _BATCHED) and position not in confirmations:
                await self._confirm_run(state, steps, position, confirmations)
            outcome = await self.executor.execute(step, state, confirmation=confirmations.get(position))
        except ActionDenied as exc:
            return await self._denied(run, summary, exc)
        except HandoffRequired as exc:
            return await self._handed_off(run, summary, exc)
        except DriverError as exc:
            if exc.code == ErrorCode.HALTED:
                return await self._stopped(run)
            return await self._step_failed(run, summary, _error_text(exc.code, exc.message))
        except Exception as exc:
            log.warning("step %s raised %s", getattr(step, "action", "?"), type(exc).__name__)
            return await self._step_failed(run, summary, _error_text(type(exc).__name__, str(exc)))
        detail = str(getattr(outcome, "detail", "") or "")
        if getattr(outcome, "ok", True) is False:
            return await self._step_failed(run, summary, _error_text("STEP_FAILED", detail))
        run.steps_done += 1
        run.failures = 0
        run.last_error = ""
        run.history.append(HistoryItem(summary, "ok", _clean(detail, HISTORY_DETAIL_CHARS)))
        await self._log("step", summary, True)
        await self._emit("step", {"count": run.steps_done, "summary": summary, "ok": True})
        limit = await self._limit_reached(run)
        if limit is not None:
            return limit
        return not (changes_page(step) or isinstance(step, ReadTextStep))

    async def _step_failed(self, run: _Run, summary: str, error: str) -> Union[RunResult, bool]:
        run.steps_done += 1
        run.failures += 1
        run.last_error = error
        run.history.append(HistoryItem(summary, "error", error))
        await self._log("step", summary, False)
        await self._emit("step", {"count": run.steps_done, "summary": summary, "ok": False})
        if run.failures > self.max_retries:
            return await self._failed(run)
        limit = await self._limit_reached(run)
        return limit if limit is not None else False

    async def _handed_off(self, run: _Run, summary: str, exc: HandoffRequired) -> Union[RunResult, bool]:
        run.steps_done += 1
        if await self._ask(exc.message, "handoff") is None:
            return await self._stopped(run)
        run.history.append(HistoryItem(summary, "info", f"handoff done ({exc.kind})"))
        limit = await self._limit_reached(run)
        return limit if limit is not None else False
