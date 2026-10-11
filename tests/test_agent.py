import asyncio
import logging
import re
import sys
import types
from dataclasses import dataclass
from typing import Any, List, Optional

import pytest

from babas import agent as agent_module
from babas.agent import (
    DENIED_ANSWER,
    FAILED_ANSWER,
    MAX_STEPS_ANSWER,
    Agent,
    RunResult,
    summarize_step,
)
from babas.channels import ConsoleChannel
from babas.drivers.base import DriverError, ErrorCode, Target
from babas.gate import ActionDenied, HandoffRequired, SafeDriver
from babas.llm import LLMError
from babas.safety import CAPTCHA_TEXT, HANDOFF_TEXTS, SafetyPolicy, mask
from babas.schemas import Plan, plan_json_schema
from tests.helpers import FakeDriver, FakeLLM, RecordingChannel, make_element, make_state

CURP = "GOMA850101HDFRRN09"
MASKED_CURP = "GO**************09"


@dataclass
class Outcome:
    ok: bool = True
    detail: str = ""
    data: Any = None


class ScriptedExecutor:
    """Stands in for babas.actions.ActionExecutor: each entry of the script is consumed per call and is
    None (success), an Outcome, an exception to raise, or a callable(step) returning one of those."""

    def __init__(self, *script: Any, delay: float = 0.0):
        self.script: List[Any] = list(script)
        self.delay = delay
        self.calls: List[tuple] = []

    async def execute(self, step: Any, state: Any, *, confirmation: Optional[str] = None) -> Outcome:
        self.calls.append((step, state, confirmation))
        if self.delay:
            await asyncio.sleep(self.delay)
        item = self.script.pop(0) if self.script else None
        if callable(item) and not isinstance(item, BaseException):
            item = item(step)
        if isinstance(item, BaseException):
            raise item
        return item if item is not None else Outcome()


def plan(*steps: dict) -> dict:
    return {"steps": list(steps), "reasoning": "test"}


def click(index: int = 0, **extra: Any) -> dict:
    return {"action": "click", "index": index, **extra}


def type_text(index: int, value: str, **extra: Any) -> dict:
    return {"action": "type_text", "index": index, "value": value, **extra}


def select_option(index: int, value: str, **extra: Any) -> dict:
    return {"action": "select_option", "index": index, "value": value, **extra}


def scroll(**extra: Any) -> dict:
    return {"action": "scroll", **extra}


def ask_user(question: str, kind: str = "text", **extra: Any) -> dict:
    return {"action": "ask_user", "value": question, "kind": kind, **extra}


def done(text: str = "Listo.", success: bool = True) -> dict:
    return {"action": "done", "value": text, "success": success}


def page(**fields: Any):
    elements = [make_element(0, "button", "Buscar"), make_element(1, "textbox", "Nombre")]
    return make_state(*elements, **fields)


def make_agent(llm: FakeLLM, *, executor=None, driver=None, channel=None, **kwargs: Any) -> Agent:
    return Agent(
        driver if driver is not None else FakeDriver(page()),
        llm,
        channel if channel is not None else RecordingChannel(),
        executor=executor if executor is not None else ScriptedExecutor(),
        **kwargs,
    )


def prompts(llm: FakeLLM) -> List[str]:
    return [user for _, user in llm.calls]


@pytest.fixture(autouse=True)
def no_observe_delay(monkeypatch):
    monkeypatch.setattr(agent_module, "OBSERVE_RETRY_DELAY_S", 0)


@pytest.fixture(autouse=True)
def bounded_runs(monkeypatch):
    # A loop regression must fail the test, not hang the suite.
    original = Agent.run

    async def bounded(self, task):
        return await asyncio.wait_for(original(self, task), 10)

    monkeypatch.setattr(Agent, "run", bounded)


class TestLoop:
    async def test_done_path(self):
        llm = FakeLLM([plan(click(0, explanation="Voy a presionar el botón.")), plan(done("Encontré las recetas."))])
        agent = make_agent(llm)
        result = await agent.run("buscar recetas")
        assert result == RunResult("done", "Encontré las recetas.", 1)
        assert len(agent.executor.calls) == 1
        step, state, confirmation = agent.executor.calls[0]
        assert step.action == "click" and state.snapshot_id == "s1" and confirmation is None
        assert agent.channel.logs == [
            ("assistant", "Voy a presionar el botón.", None),
            ("step", "click [0]", True),
            ("result", "Encontré las recetas.", None),
        ]
        first, second = prompts(llm)
        assert "TASK: buscar recetas" in first and "STEPS LEFT: 30" in first
        assert "1. click [0] -> ok" in second and "STEPS LEFT: 29" in second
        assert "RULES:" in llm.calls[0][0]
        assert llm.schemas[0] == plan_json_schema()

    async def test_done_with_success_false_is_failed(self):
        llm = FakeLLM([plan(done("No encontré la página.", success=False))])
        agent = make_agent(llm)
        result = await agent.run("t")
        assert result == RunResult("failed", "No encontré la página.", 0)
        assert agent.channel.logs == [("result", "No encontré la página.", None)]

    async def test_recovery_after_an_error(self):
        llm = FakeLLM([plan(click(9)), plan(click(0)), plan(done())])
        executor = ScriptedExecutor(DriverError(ErrorCode.NOT_FOUND, "No element matches [9]"))
        agent = make_agent(llm, executor=executor)
        result = await agent.run("t")
        assert result.status == "done" and result.steps == 2
        first, second, third = prompts(llm)
        assert "LAST ERROR" not in first
        assert "LAST ERROR: NOT_FOUND: No element matches [9]" in second
        assert "1. click [9] -> error: NOT_FOUND: No element matches [9]" in second
        assert "LAST ERROR" not in third
        assert ("step", "click [9]", False) in agent.channel.logs

    async def test_fails_after_three_consecutive_errors(self):
        error = DriverError(ErrorCode.NOT_FOUND, "No element matches [9]")
        llm = FakeLLM([plan(click(9))] * 5)
        agent = make_agent(llm, executor=ScriptedExecutor(*[error] * 5))
        result = await agent.run("t")
        assert result == RunResult("failed", FAILED_ANSWER, 3)
        assert len(llm.calls) == 3 and len(agent.executor.calls) == 3
        assert ("error", FAILED_ANSWER, None) in agent.channel.logs

    async def test_a_success_resets_the_failure_count(self):
        error = DriverError(ErrorCode.TIMEOUT, "slow")
        llm = FakeLLM([plan(click(0))] * 6 + [plan(done())])
        executor = ScriptedExecutor(error, error, None, error, error, None)
        result = await make_agent(llm, executor=executor).run("t")
        assert result.status == "done" and result.steps == 6

    async def test_max_retries_zero_fails_on_the_first_error(self):
        llm = FakeLLM([plan(click(0))] * 3)
        executor = ScriptedExecutor(RuntimeError("boom"))
        result = await make_agent(llm, executor=executor, max_retries=0).run("t")
        assert result.status == "failed" and len(llm.calls) == 1

    async def test_any_exception_is_a_failure_with_type_and_message(self):
        llm = FakeLLM([plan(click(0)), plan(done())])
        executor = ScriptedExecutor(ValueError("ask_user is handled by the agent"))
        await make_agent(llm, executor=executor).run("t")
        assert "LAST ERROR: ValueError: ask_user is handled by the agent" in prompts(llm)[1]

    async def test_outcome_not_ok_is_a_failure(self):
        llm = FakeLLM([plan(click(0)), plan(done())])
        executor = ScriptedExecutor(Outcome(ok=False, detail="the button did nothing"))
        agent = make_agent(llm, executor=executor)
        await agent.run("t")
        assert "LAST ERROR: STEP_FAILED: the button did nothing" in prompts(llm)[1]
        assert ("step", "click [0]", False) in agent.channel.logs

    async def test_max_steps(self):
        llm = FakeLLM([plan(click(0))] * 10)
        agent = make_agent(llm, max_steps=3)
        result = await agent.run("t")
        assert result == RunResult("max_steps", MAX_STEPS_ANSWER, 3)
        assert len(agent.executor.calls) == 3 and len(llm.calls) == 3
        assert [re.search(r"STEPS LEFT: (\d+)", user).group(1) for user in prompts(llm)] == ["3", "2", "1"]
        assert ("error", MAX_STEPS_ANSWER, None) in agent.channel.logs

    async def test_max_steps_is_enforced_inside_a_plan(self):
        llm = FakeLLM([plan(scroll(), scroll(), scroll(), scroll())])
        agent = make_agent(llm, max_steps=2)
        result = await agent.run("t")
        assert result.status == "max_steps" and result.steps == 2
        assert len(agent.executor.calls) == 2

    async def test_page_changing_step_ends_the_plan(self):
        llm = FakeLLM([plan(type_text(1, "Ana"), scroll(), click(0), click(0)), plan(done())])
        agent = make_agent(llm)
        result = await agent.run("t")
        assert [call[0].action for call in agent.executor.calls] == ["type_text", "scroll", "click"]
        assert result.steps == 3 and len(llm.calls) == 2

    async def test_enter_ends_the_plan_but_tab_does_not(self):
        enter = {"action": "press_key", "value": "Enter"}
        tab = {"action": "press_key", "value": "Tab"}
        llm = FakeLLM([plan(tab, enter, tab), plan(done())])
        agent = make_agent(llm)
        await agent.run("t")
        assert [call[0].value for call in agent.executor.calls] == ["Tab", "Enter"]

    async def test_read_text_result_reaches_the_next_prompt(self):
        llm = FakeLLM([plan({"action": "read_text", "index": 0}, done("adivinado")), plan(done("Cierra hoy."))])
        executor = ScriptedExecutor(Outcome(detail="Aviso: cerrado hoy"))
        result = await make_agent(llm, executor=executor).run("t")
        assert result.answer == "Cierra hoy."
        assert "1. read_text [0] -> ok: Aviso: cerrado hoy" in prompts(llm)[1]

    async def test_plan_without_done_plans_again(self):
        llm = FakeLLM([plan(type_text(1, "Ana")), plan(done())])
        result = await make_agent(llm).run("t")
        assert result.status == "done" and len(llm.calls) == 2

    async def test_agent_can_run_twice(self):
        llm = FakeLLM([plan(click(0)), plan(done("uno")), plan(done("dos"))])
        agent = make_agent(llm)
        assert (await agent.run("primera")).answer == "uno"
        assert (await agent.run("segunda")).answer == "dos"
        last = prompts(llm)[-1]
        assert "TASK: segunda" in last and "click [0]" not in last and "STEPS LEFT: 30" in last

    async def test_language_reaches_the_system_prompt(self):
        llm = FakeLLM([plan(done())])
        await make_agent(llm, language="es").run("t")
        assert "Spanish (Mexico)" in llm.calls[0][0]

    async def test_default_executor_is_imported_lazily(self, monkeypatch):
        class FakeExecutor:
            def __init__(self, driver):
                self.driver = driver

        module = types.ModuleType("babas.actions")
        module.ActionExecutor = FakeExecutor
        monkeypatch.setitem(sys.modules, "babas.actions", module)
        driver = FakeDriver(page())
        agent = Agent(driver, FakeLLM(), RecordingChannel())
        assert isinstance(agent.executor, FakeExecutor) and agent.executor.driver is driver


class TestStopsAndHandoffs:
    async def test_denial_stops_the_run_cleanly(self):
        llm = FakeLLM([plan(click(0)), plan(done())])
        executor = ScriptedExecutor(ActionDenied("denied"))
        agent = make_agent(llm, executor=executor)
        result = await agent.run("t")
        assert result == RunResult("stopped", DENIED_ANSWER, 0)
        assert DENIED_ANSWER == "De acuerdo, no lo hice."
        assert ("info", DENIED_ANSWER, None) in agent.channel.logs
        assert len(llm.calls) == 1

    async def test_denial_with_no_ui_also_stops(self):
        executor = ScriptedExecutor(ActionDenied("no_ui"))
        result = await make_agent(FakeLLM([plan(click(0))]), executor=executor).run("t")
        assert result.status == "stopped" and result.answer == DENIED_ANSWER

    async def test_handoff_then_continue(self):
        text = HANDOFF_TEXTS["password_field"]
        llm = FakeLLM([plan(click(0)), plan(click(0)), plan(done())])
        executor = ScriptedExecutor(HandoffRequired("password_field", text))
        agent = make_agent(llm, executor=executor, channel=RecordingChannel(answer="listo"))
        result = await agent.run("t")
        assert result.status == "done"
        assert agent.channel.asks == [(text, "handoff")]
        second = prompts(llm)[1]
        assert "1. click [0] -> info: handoff done (password_field)" in second
        assert "LAST ERROR" not in second

    async def test_handoff_answer_is_never_recorded(self):
        llm = FakeLLM([plan(click(0)), plan(done())])
        executor = ScriptedExecutor(HandoffRequired("password_field", "texto"))
        channel = RecordingChannel(answer="mi clave es hunter2-secret")
        await make_agent(llm, executor=executor, channel=channel).run("t")
        assert all("hunter2" not in user for user in prompts(llm))

    async def test_handoff_without_answer_stops(self):
        llm = FakeLLM([plan(click(0)), plan(done())])
        executor = ScriptedExecutor(HandoffRequired("captcha", "texto"))
        result = await make_agent(llm, executor=executor, channel=RecordingChannel(answer=None)).run("t")
        assert result.status == "stopped" and len(llm.calls) == 1

    async def test_handoffs_count_as_steps_so_the_run_cannot_loop_forever(self):
        llm = FakeLLM([plan(click(0))] * 10)
        executor = ScriptedExecutor(*[HandoffRequired("password_field", "texto")] * 10)
        result = await make_agent(llm, executor=executor, max_steps=3).run("t")
        assert result.status == "max_steps" and result.steps == 3

    async def test_captcha_is_handed_off_once_per_url(self):
        gob = "https://www.gob.mx/captcha"
        other = "https://www.gob.mx/otra"
        states = [
            page(url=gob, has_captcha=True),
            page(url=gob, has_captcha=True),
            page(url=other, has_captcha=True),
            page(url=other + "#abajo", has_captcha=True),
        ]
        llm = FakeLLM([plan(click(0)), plan(done())])
        agent = make_agent(llm, driver=FakeDriver(states, delay=0.001))
        result = await agent.run("t")
        assert result.status == "done"
        assert agent.channel.asks == [(CAPTCHA_TEXT, "handoff")] * 2
        assert len(llm.calls) == 2
        assert "1. handoff captcha -> info: the user says it is done" in prompts(llm)[0]
        assert "2. handoff captcha -> info" not in prompts(llm)[0]

    async def test_captcha_handoff_without_answer_stops_before_planning(self):
        llm = FakeLLM([plan(done())])
        agent = make_agent(llm, driver=FakeDriver(page(has_captcha=True)), channel=RecordingChannel(answer=None))
        result = await agent.run("t")
        assert result.status == "stopped" and llm.calls == []

    async def test_halted_during_a_step_stops(self):
        llm = FakeLLM([plan(click(0)), plan(done())])
        executor = ScriptedExecutor(DriverError(ErrorCode.HALTED, "Stopped by the user"))
        result = await make_agent(llm, executor=executor).run("t")
        assert result.status == "stopped" and len(llm.calls) == 1

    async def test_halted_while_observing_stops(self):
        llm = FakeLLM([plan(done())])
        driver = FakeDriver(page(), errors={"get_page_state": DriverError(ErrorCode.HALTED)})
        result = await make_agent(llm, driver=driver).run("t")
        assert result.status == "stopped" and llm.calls == []

    async def test_site_denied_while_observing_stops_cleanly(self):
        llm = FakeLLM([plan(done())])
        driver = FakeDriver(page(), errors={"get_page_state": ActionDenied("site", "example.org")})
        result = await make_agent(llm, driver=driver).run("t")
        assert result == RunResult("stopped", DENIED_ANSWER, 0) and llm.calls == []


class TestAskUser:
    async def test_text_answer_reaches_the_next_prompt(self):
        llm = FakeLLM([plan(ask_user("¿En qué ciudad vive?", explanation="Me falta un dato.")), plan(done())])
        agent = make_agent(llm, channel=RecordingChannel(answer="Guadalajara"))
        result = await agent.run("t")
        assert result.status == "done" and result.steps == 0
        assert agent.channel.asks == [("¿En qué ciudad vive?", "text")]
        assert ("assistant", "Me falta un dato.", None) in agent.channel.logs
        assert '1. ask_user "¿En qué ciudad vive?" -> answer: Guadalajara' in prompts(llm)[1]
        assert agent.executor.calls == []

    async def test_no_answer_stops(self):
        llm = FakeLLM([plan(ask_user("¿Cuál?")), plan(done())])
        result = await make_agent(llm, channel=RecordingChannel(answer=None)).run("t")
        assert result.status == "stopped" and len(llm.calls) == 1

    async def test_pii_answer_is_masked_in_the_history(self):
        llm = FakeLLM([plan(ask_user("¿Cuál es su CURP?")), plan(done())])
        await make_agent(llm, channel=RecordingChannel(answer=CURP)).run("t")
        assert CURP not in prompts(llm)[1]
        assert f"-> answer: {MASKED_CURP}" in prompts(llm)[1]

    async def test_handoff_question_answer_is_not_recorded(self):
        llm = FakeLLM([plan(ask_user("Inicie sesión", kind="handoff")), plan(done())])
        agent = make_agent(llm, channel=RecordingChannel(answer="usuario123 clave456"))
        await agent.run("t")
        assert agent.channel.asks == [("Inicie sesión", "handoff")]
        assert "clave456" not in prompts(llm)[1]
        assert "-> answer: the user says it is done" in prompts(llm)[1]

    async def test_empty_answer_is_still_an_answer(self):
        llm = FakeLLM([plan(ask_user("¿Algo más?")), plan(done())])
        result = await make_agent(llm, channel=RecordingChannel(answer="")).run("t")
        assert result.status == "done"
        assert "-> answer: (empty answer)" in prompts(llm)[1]


def form_state():
    return make_state(
        make_element(1, "textbox", "Nombre"),
        make_element(2, "textbox", "CURP", flags=["pii"]),
        make_element(3, "textbox", "Correo", flags=["pii"], type="email"),
        make_element(4, "combobox", "Estado", options=["Jalisco", "Colima"]),
        make_element(5, "button", "Enviar", flags=["submit"]),
        make_element(6, "textbox", "Clave", flags=["password"]),
    )


def gated_agent(llm: FakeLLM, channel=None, **kwargs: Any):
    channel = channel if channel is not None else RecordingChannel(mint_ids=True)
    safe = SafeDriver(FakeDriver(form_state()), SafetyPolicy(), channel)
    batches: List[tuple] = []
    original = safe.confirm_batch

    async def spy(items, **options):
        batches.append((list(items), options))
        return await original(items, **options)

    safe.confirm_batch = spy
    agent = make_agent(llm, driver=safe, channel=channel, **kwargs)
    return agent, batches


class TestBatchConfirmation:
    async def test_form_fields_use_exactly_one_confirm_batch(self):
        steps = [
            type_text(1, "Ana", explanation="Voy a llenar el formulario."),
            type_text(2, CURP),
            type_text(3, "ana@example.com"),
            select_option(4, "Jalisco"),
        ]
        llm = FakeLLM([plan(*steps), plan(done())])
        agent, batches = gated_agent(llm)
        result = await agent.run("t")
        assert result.status == "done" and result.steps == 4
        assert len(batches) == 1
        items, options = batches[0]
        assert [item[0] for item in items] == ["type", "type", "type", "select"]
        assert items[0] == ("type", Target(index=1, snapshot_id="s1"), {"text": "Ana"})
        assert items[1][2] == {"text": CURP}
        assert items[3] == ("select", Target(index=4, snapshot_id="s1"), {"value": "Jalisco"})
        assert options == {"explanation": "Voy a llenar el formulario."}
        assert len(agent.channel.confirms) == 1
        assert len(agent.channel.confirms[0].actions) == 2
        assert [call[2] for call in agent.executor.calls] == ["conf-1"] * 4

    async def test_batch_that_needs_no_confirmation_passes_no_token(self):
        llm = FakeLLM([plan(type_text(1, "Ana"), select_option(4, "Jalisco")), plan(done())])
        agent, batches = gated_agent(llm)
        await agent.run("t")
        assert len(batches) == 1 and agent.channel.confirms == []
        assert [call[2] for call in agent.executor.calls] == [None, None]

    async def test_separate_runs_are_confirmed_separately(self):
        steps = [type_text(1, "Ana"), type_text(2, CURP), scroll(), type_text(3, "ana@example.com")]
        llm = FakeLLM([plan(*steps), plan(done())])
        agent, batches = gated_agent(llm)
        await agent.run("t")
        assert [len(items) for items, _ in batches] == [2, 1]
        assert len(agent.channel.confirms) == 2
        assert [call[2] for call in agent.executor.calls] == ["conf-1", "conf-1", None, "conf-2"]

    async def test_steps_after_a_page_change_are_not_confirmed(self):
        steps = [type_text(2, CURP), click(5), type_text(3, "ana@example.com")]
        llm = FakeLLM([plan(*steps), plan(done())])
        agent, batches = gated_agent(llm)
        await agent.run("t")
        assert len(batches) == 1 and len(batches[0][0]) == 1

    async def test_refused_batch_stops_the_run(self):
        llm = FakeLLM([plan(type_text(1, "Ana"), type_text(2, CURP)), plan(done())])
        agent, batches = gated_agent(llm, channel=RecordingChannel(approve=False))
        result = await agent.run("t")
        assert result == RunResult("stopped", DENIED_ANSWER, 0)
        assert agent.executor.calls == [] and len(llm.calls) == 1
        assert ("info", DENIED_ANSWER, None) in agent.channel.logs

    async def test_password_field_in_a_batch_is_a_handoff(self):
        llm = FakeLLM([plan(type_text(1, "Ana"), type_text(6, "secreto")), plan(done())])
        agent, _ = gated_agent(llm, channel=RecordingChannel(mint_ids=True, answer="listo"))
        result = await agent.run("t")
        assert result.status == "done"
        assert agent.channel.asks == [(HANDOFF_TEXTS["password_field"], "handoff")]
        assert agent.executor.calls == []
        assert all("secreto" not in user for user in prompts(llm))

    async def test_driver_without_confirm_batch_passes_no_token(self):
        llm = FakeLLM([plan(type_text(1, "Ana"), type_text(2, CURP)), plan(done())])
        agent = make_agent(llm, driver=FakeDriver(form_state()))
        await agent.run("t")
        assert [call[2] for call in agent.executor.calls] == [None, None]


class TestPlanning:
    async def test_retry_on_invalid_json_then_success(self):
        llm = FakeLLM([LLMError("The model answer has no JSON object."), plan(done())])
        agent = make_agent(llm)
        result = await agent.run("t")
        assert result.status == "done" and len(llm.calls) == 2
        first, second = prompts(llm)
        assert second.startswith(first)
        assert "Your previous answer was invalid: The model answer has no JSON object." in second
        assert second.endswith("Reply again with ONLY valid JSON.")

    async def test_retry_on_schema_error_does_not_echo_the_input(self):
        bad = {"steps": [{"action": "click", "text": "", "value": CURP}]}
        llm = FakeLLM([bad, plan(done())])
        result = await make_agent(llm).run("t")
        assert result.status == "done"
        second = prompts(llm)[1]
        assert "Your previous answer was invalid:" in second
        assert "click needs an index" in second
        assert CURP not in second and "input_value" not in second

    async def test_unknown_action_is_reported_by_name(self):
        llm = FakeLLM([{"steps": [{"action": "teleport"}]}, plan(done())])
        await make_agent(llm).run("t")
        assert "teleport" in prompts(llm)[1]

    async def test_two_invalid_answers_count_as_one_failure(self):
        llm = FakeLLM(["no json", "still no json", plan(done())])
        agent = make_agent(llm, max_retries=1)
        result = await agent.run("t")
        assert result.status == "done" and len(llm.calls) == 3
        third = prompts(llm)[2]
        assert "LAST ERROR: " in third
        assert "1. plan -> error: " in third

    async def test_two_failed_double_attempts_exceed_one_retry(self):
        llm = FakeLLM(["a", "b", "c", "d", plan(done())])
        result = await make_agent(llm, max_retries=1).run("t")
        assert result == RunResult("failed", FAILED_ANSWER, 0)
        assert len(llm.calls) == 4

    async def test_with_no_retries_one_double_failure_ends_the_run(self):
        llm = FakeLLM([LLMError("down"), LLMError("down"), plan(done())])
        result = await make_agent(llm, max_retries=0).run("t")
        assert result.status == "failed" and len(llm.calls) == 2

    async def test_unexpected_planner_exception_is_one_failure_without_retry(self):
        llm = FakeLLM([RuntimeError("secret internal detail"), plan(done())])
        agent = make_agent(llm)
        result = await agent.run("t")
        assert result.status == "done" and len(llm.calls) == 2
        assert "LAST ERROR: RuntimeError: secret internal detail" in prompts(llm)[1]

    async def test_plan_that_is_not_an_object_is_invalid(self):
        llm = FakeLLM([["click"], plan(done())])
        assert (await make_agent(llm).run("t")).status == "done"


class TestObservation:
    async def test_unscriptable_page_gets_an_empty_state(self):
        error = DriverError(ErrorCode.UNSCRIPTABLE, "cannot script", {"url": "chrome://newtab/"})
        driver = FakeDriver(page(), errors={"get_page_state": error})
        llm = FakeLLM([plan({"action": "goto", "value": "gob.mx"}), plan(done())])
        agent = make_agent(llm, driver=driver)
        result = await agent.run("t")
        assert result.status == "done"
        first = prompts(llm)[0]
        assert "URL: chrome://newtab/" in first
        assert "BABAS cannot read this page. Use goto to open a website." in first
        assert "(none)" in first
        assert agent.executor.calls[0][1].snapshot_id == "none"
        assert "UNSCRIPTABLE" not in prompts(llm)[1]

    async def test_unscriptable_url_falls_back_to_the_driver(self):
        error = DriverError(ErrorCode.UNSCRIPTABLE, "cannot script")
        driver = FakeDriver(page(), errors={"get_page_state": error}, url="chrome://extensions/")
        llm = FakeLLM([plan(done())])
        await make_agent(llm, driver=driver).run("t")
        assert "URL: chrome://extensions/" in prompts(llm)[0]

    async def test_observation_failures_are_retried_then_fail(self):
        errors = [DriverError(ErrorCode.TIMEOUT, "slow")] * 3
        driver = FakeDriver(page(), errors={"get_page_state": errors})
        llm = FakeLLM([plan(done())])
        result = await make_agent(llm, driver=driver).run("t")
        assert result.status == "failed" and llm.calls == []
        assert len(driver.calls_of("get_page_state")) == 3

    async def test_transient_observation_failure_is_reported_to_the_model(self):
        driver = FakeDriver(page(), errors={"get_page_state": DriverError(ErrorCode.TIMEOUT, "slow")})
        llm = FakeLLM([plan(done())])
        result = await make_agent(llm, driver=driver).run("t")
        assert result.status == "done"
        assert "LAST ERROR: TIMEOUT: slow" in prompts(llm)[0]


class TestCancellation:
    @staticmethod
    async def cancel_after_start(agent: Agent) -> None:
        task = asyncio.ensure_future(agent.run("t"))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()
        assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task()] == []

    async def test_cancelled_while_planning(self):
        await self.cancel_after_start(make_agent(FakeLLM([plan(done())], delay=30)))

    async def test_cancelled_while_executing(self):
        agent = make_agent(FakeLLM([plan(click(0))]), executor=ScriptedExecutor(delay=30))
        await self.cancel_after_start(agent)

    async def test_cancelled_while_waiting_for_the_user(self):
        agent = make_agent(FakeLLM([plan(ask_user("¿Cuál?"))]), channel=RecordingChannel(delay=30))
        await self.cancel_after_start(agent)

    async def test_cancelled_while_observing(self):
        agent = make_agent(FakeLLM([plan(done())]), driver=FakeDriver(page(), delay=30))
        await self.cancel_after_start(agent)

    async def test_cancellation_is_not_a_step_failure(self):
        executor = ScriptedExecutor(asyncio.CancelledError())
        agent = make_agent(FakeLLM([plan(click(0))] * 3), executor=executor)
        with pytest.raises(asyncio.CancelledError):
            await agent.run("t")
        assert len(agent.executor.calls) == 1


class TestEvents:
    async def test_plan_and_step_events(self):
        events = []

        async def on_event(name, data):
            events.append((name, data))

        llm = FakeLLM([plan(scroll(), click(0)), plan(done())])
        error = DriverError(ErrorCode.NOT_FOUND, "gone")
        agent = make_agent(llm, on_event=on_event, executor=ScriptedExecutor(None, error))
        await agent.run("t")
        assert events == [
            ("plan", {"turn": 1, "steps": ["scroll down 600", "click [0]"]}),
            ("step", {"count": 1, "summary": "scroll down 600", "ok": True}),
            ("step", {"count": 2, "summary": "click [0]", "ok": False}),
            ("plan", {"turn": 2, "steps": ["done"]}),
        ]

    async def test_a_failing_callback_does_not_break_the_run(self):
        async def on_event(name, data):
            raise RuntimeError("panel closed")

        llm = FakeLLM([plan(click(0)), plan(done())])
        result = await make_agent(llm, on_event=on_event).run("t")
        assert result.status == "done" and result.steps == 1

    async def test_a_failing_channel_log_does_not_break_the_run(self):
        class BrokenLog(RecordingChannel):
            async def log(self, kind, text, ok=None):
                raise RuntimeError("no panel")

        llm = FakeLLM([plan(click(0)), plan(done())])
        result = await make_agent(llm, channel=BrokenLog()).run("t")
        assert result.status == "done"


class TestPrivacy:
    async def test_history_and_prompts_never_contain_the_typed_curp(self):
        llm = FakeLLM([plan(type_text(1, CURP, explanation="Voy a escribir su dato.")), plan(done())])
        agent = make_agent(llm)
        await agent.run("consulta mi dato")
        second = prompts(llm)[1]
        assert f'1. type_text [1] "{MASKED_CURP}" -> ok' in second
        for system, user in llm.calls:
            assert CURP not in system and CURP not in user
        assert ("step", f'type_text [1] "{MASKED_CURP}"', True) in agent.channel.logs
        assert agent.executor.calls[0][0].value == CURP

    async def test_errors_that_echo_the_curp_are_masked(self):
        llm = FakeLLM([plan(type_text(1, CURP)), plan(done())])
        executor = ScriptedExecutor(ValueError(f"cannot type {CURP}"))
        await make_agent(llm, executor=executor).run("t")
        second = prompts(llm)[1]
        assert CURP not in second
        assert f"LAST ERROR: ValueError: cannot type {MASKED_CURP}" in second

    async def test_logs_never_contain_pii(self, caplog):
        caplog.set_level(logging.DEBUG)
        events = []

        async def on_event(name, data):
            events.append((name, data))

        llm = FakeLLM(
            [
                plan(type_text(1, CURP, explanation=f"Escribo {CURP} en el campo.")),
                plan(click(0)),
                plan(ask_user(f"¿Es {CURP}?")),
                plan(done("Listo.")),
            ]
        )
        executor = ScriptedExecutor(None, ValueError(f"rejected {CURP}"))
        channel = RecordingChannel(answer=f"mi CURP es {CURP}")
        agent = make_agent(llm, executor=executor, channel=channel, on_event=on_event)
        result = await agent.run(f"consulta la CURP {CURP}")
        assert result.status == "done"
        assert caplog.text
        assert CURP not in caplog.text
        assert all(CURP not in text for _, text, _ in channel.logs)
        assert any(MASKED_CURP in text for _, text, _ in channel.logs)
        assert CURP not in repr(events)
        assert CURP not in repr(result)

    async def test_console_output_never_contains_the_curp(self, capsys):
        async def ainput(prompt: str) -> str:
            return f"es {CURP}"

        llm = FakeLLM([plan(type_text(1, CURP)), plan(ask_user("¿Cuál?")), plan(done(f"Su CURP es {CURP}"))])
        agent = make_agent(llm, channel=ConsoleChannel(ainput=ainput))
        await agent.run("t")
        out = capsys.readouterr().out
        assert out and CURP not in out
        assert MASKED_CURP in out


class TestSummaries:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ({"action": "goto", "value": "gob.mx"}, "goto https://gob.mx"),
            ({"action": "wait", "ms": 500}, "wait 500 ms"),
            ({"action": "click", "index": 4}, "click [4]"),
            ({"action": "click", "selector": "#go"}, 'click selector="#go"'),
            ({"action": "click", "text": "Buscar"}, 'click text="Buscar"'),
            ({"action": "type_text", "index": 3, "value": CURP}, f'type_text [3] "{MASKED_CURP}"'),
            ({"action": "type_text", "index": 2, "value": "recetas"}, 'type_text [2] "recetas"'),
            ({"action": "select_option", "index": 7, "value": "Jalisco"}, 'select_option [7] "Jalisco"'),
            ({"action": "press_key", "value": "enter", "index": 2}, "press_key Enter [2]"),
            ({"action": "press_key", "value": "Tab"}, "press_key Tab"),
            ({"action": "scroll", "value": "up", "ms": 300}, "scroll up 300"),
            ({"action": "smart_search", "value": "clima"}, 'smart_search "clima"'),
            ({"action": "search_youtube", "value": "José José"}, 'search_youtube "José José"'),
            ({"action": "new_tab", "tab_name": "correo"}, "new_tab correo"),
            ({"action": "close_tab"}, "close_tab"),
            ({"action": "read_text"}, "read_text"),
            ({"action": "read_text", "index": 12}, "read_text [12]"),
            ({"action": "send_email"}, "send_email"),
            ({"action": "send_whatsapp_message", "body": "hola mamá"}, "send_whatsapp_message (9 characters)"),
            ({"action": "ask_user", "value": "¿Cuál?"}, 'ask_user "¿Cuál?"'),
            ({"action": "done", "value": "Listo."}, "done"),
        ],
    )
    def test_summaries(self, raw, expected):
        step = Plan.model_validate({"steps": [raw]}).steps[0]
        assert summarize_step(step) == expected

    def test_email_recipient_is_masked_and_body_is_left_out(self):
        raw = {"action": "compose_email", "to": "ana@example.com", "subject": "Hola", "body": "texto privado"}
        summary = summarize_step(Plan.model_validate({"steps": [raw]}).steps[0])
        assert summary == f'compose_email to={mask("ana@example.com")} subject="Hola"'
        assert "privado" not in summary and "ana@example.com" not in summary

    def test_redaction_happens_before_clipping(self):
        value = "x" * 49 + " " + CURP
        summary = summarize_step(Plan.model_validate({"steps": [type_text(1, value)]}).steps[0])
        assert "GOMA" not in summary and CURP[:8] not in summary

    def test_pii_in_a_url_is_masked(self):
        step = Plan.model_validate({"steps": [{"action": "goto", "value": f"https://example.org/?curp={CURP}"}]}).steps[0]
        assert CURP not in summarize_step(step)
