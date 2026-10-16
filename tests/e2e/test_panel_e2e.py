import asyncio
import time
from typing import Callable, List, Tuple

import pytest
from playwright.async_api import expect

from babas.agent import DENIED_ANSWER
from babas.drivers.base import DriverError, ErrorCode

pytestmark = pytest.mark.e2e

UI_TIMEOUT_MS = 10000
RUN_TIMEOUT_MS = 30000
STOP_BUDGET_S = 1.0
EMAIL = "ana@example.com"
CURP = "GOMC800101HDFRRL09"
PLACEHOLDER = "¿Qué quiere hacer? Por ejemplo: busque videos de recetas en YouTube"
HELP_TEXT = "Abra la aplicación BABAS en su computadora (python -m babas serve)."


async def log_entries(panel) -> List[Tuple[str, str]]:
    rows = await panel.eval_on_selector_all(
        "[data-testid=log-entry]", "els => els.map(e => [e.dataset.kind, e.textContent])"
    )
    return [(kind, text) for kind, text in rows]


async def wait_run_state(panel, text: str, timeout: int = RUN_TIMEOUT_MS) -> None:
    await expect(panel.get_by_test_id("run-state")).to_contain_text(text, timeout=timeout)


async def wait_until(predicate: Callable[[], bool], timeout: float = 10.0, what: str = "the condition") -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout:.0f} s waiting for {what}")
        await asyncio.sleep(0.05)


async def click_stop_and_time(panel, session) -> float:
    started = time.monotonic()
    await panel.get_by_test_id("stop").click(timeout=UI_TIMEOUT_MS)
    await expect(panel.get_by_test_id("run-state")).to_contain_text("Detenido", timeout=int(STOP_BUDGET_S * 1000))
    await wait_until(lambda: not session.running, timeout=STOP_BUDGET_S, what="the run task to end")
    return time.monotonic() - started


async def expect_halted(bridge) -> None:
    with pytest.raises(DriverError) as caught:
        await bridge.request("page.state", timeout=5)
    assert caught.value.code == ErrorCode.HALTED



async def test_task_runs_and_logs(session, panel, work_page, fake_llm, fixture_url, submit_task):
    answer = "Abrí el formulario de prueba."
    fake_llm.push(
        {"steps": [{"action": "goto", "value": fixture_url("form.html"), "explanation": "Voy a abrir el formulario."}]},
        {"steps": [{"action": "done", "value": answer}]},
    )

    await submit_task("Abre el formulario")

    run_state = panel.get_by_test_id("run-state")
    await expect(run_state).to_contain_text("Listo.", timeout=RUN_TIMEOUT_MS)
    await expect(run_state).to_contain_text(answer)
    entries = await log_entries(panel)
    assert ("user", "Abre el formulario") in entries
    assert ("assistant", "Voy a abrir el formulario.") in entries
    assert ("step", f"goto {fixture_url('form.html')}") in entries
    assert ("result", answer) in entries
    assert work_page.url == fixture_url("form.html")
    assert len(fake_llm.calls) == 2
    await expect(panel.get_by_test_id("send")).to_be_enabled()
    await expect(panel.get_by_test_id("stop")).to_be_hidden()
    await expect(panel.get_by_test_id("status")).to_have_text("Conectado")


async def test_failed_run_is_reported_in_spanish(session, panel, fake_llm, open_fixture, submit_task):
    await open_fixture("form.html")
    fake_llm.push(*[{"steps": [{"action": "click", "text": "Botón que no existe"}]}] * 5)

    await submit_task("Presiona un botón")

    await wait_run_state(panel, "No pude terminar la tarea.")
    await expect(panel.get_by_test_id("send")).to_be_enabled()
    entries = await log_entries(panel)
    assert any(kind == "error" for kind, _ in entries), entries


async def test_confirm_yes_types_value(session, panel, work_page, fake_llm, open_fixture, field_value, submit_task):
    await open_fixture("form.html")
    fake_llm.push(
        {
            "steps": [
                {"action": "type_text", "text": "Correo", "value": EMAIL, "explanation": "Voy a escribir su correo."},
                {"action": "done", "value": "Escribí su correo."},
            ]
        }
    )

    await submit_task("Escribe mi correo en el formulario")

    card = panel.get_by_test_id("confirm-card")
    await expect(card).to_be_visible(timeout=UI_TIMEOUT_MS)
    await expect(card).to_contain_text(f"«{EMAIL}»")
    await expect(card).to_contain_text("«Correo»")
    assert await field_value("#correo") == ""

    await panel.get_by_test_id("confirm-yes").click(timeout=UI_TIMEOUT_MS)

    await wait_run_state(panel, "Listo.")
    assert await field_value("#correo") == EMAIL
    await expect(card).to_have_count(0)


async def test_confirm_card_shows_full_value_and_label(session, panel, fake_llm, open_fixture, field_value, submit_task):
    await open_fixture("form.html")
    fake_llm.push({"steps": [{"action": "type_text", "text": "CURP", "value": CURP, "explanation": "Voy a escribir su CURP."}]})

    await submit_task("Escribe mi CURP")

    card = panel.get_by_test_id("confirm-card")
    await expect(card).to_be_visible(timeout=UI_TIMEOUT_MS)
    await expect(panel.get_by_test_id("confirm-title")).to_have_text("Escribir datos personales")
    await expect(panel.get_by_test_id("confirm-line")).to_have_text([f"Escribir «{CURP}» en «CURP» (127.0.0.1)"])
    await expect(panel.get_by_test_id("confirm-explanation")).to_have_text("BABAS dice: Voy a escribir su CURP.")
    await expect(panel.get_by_test_id("confirm-yes")).to_have_text("Sí, continuar")
    await expect(panel.get_by_test_id("confirm-no")).to_have_text("No")
    assert "GO**" not in await card.inner_text()
    assert await field_value("#curp") == ""

    await panel.get_by_test_id("confirm-no").click(timeout=UI_TIMEOUT_MS)
    await wait_run_state(panel, "Detenido")


async def test_confirm_no_stops_run(session, panel, fake_llm, open_fixture, field_value, submit_task, bridge_log):
    await open_fixture("form.html")
    fake_llm.push(
        {
            "steps": [
                {"action": "type_text", "text": "Correo", "value": EMAIL, "explanation": "Voy a escribir su correo."},
                {"action": "done", "value": "Escribí su correo."},
            ]
        }
    )

    await submit_task("Escribe mi correo en el formulario")
    await expect(panel.get_by_test_id("confirm-card")).to_be_visible(timeout=UI_TIMEOUT_MS)
    await panel.get_by_test_id("confirm-no").click(timeout=UI_TIMEOUT_MS)

    run_state = panel.get_by_test_id("run-state")
    await expect(run_state).to_contain_text("Detenido", timeout=UI_TIMEOUT_MS)
    await expect(run_state).to_contain_text(DENIED_ANSWER)
    await wait_until(lambda: not session.running, what="the run to end")
    assert await field_value("#correo") == ""
    assert [p for p in bridge_log.of("page.act") if p["op"] == "type"] == []
    assert len(fake_llm.calls) == 1
    assert ("info", DENIED_ANSWER) in await log_entries(panel)
    await expect(panel.get_by_test_id("confirm-card")).to_have_count(0)
    await expect(panel.get_by_test_id("send")).to_be_enabled()



async def test_stop_within_one_second(session, panel, ext_env, fake_llm, open_fixture, submit_task, bridge_log):
    await open_fixture("form.html")
    fake_llm.delay = 10.0
    await submit_task("Haz algo que tarda")
    await expect(panel.get_by_test_id("stop")).to_be_visible(timeout=UI_TIMEOUT_MS)
    await wait_until(lambda: session.running, what="the run to start")
    await asyncio.sleep(0.3)

    elapsed = await click_stop_and_time(panel, session)

    assert elapsed < STOP_BUDGET_S, f"stop took {elapsed:.2f} s"
    await expect_halted(ext_env.bridge)
    assert fake_llm.calls == []
    assert bridge_log.of("page.act") == []
    await expect(panel.get_by_test_id("status")).to_have_text("Detenido")


async def test_stop_during_confirmation(session, panel, ext_env, fake_llm, open_fixture, field_value, submit_task, bridge_log):
    await open_fixture("form.html")
    fake_llm.push({"steps": [{"action": "type_text", "text": "Correo", "value": EMAIL, "explanation": "Voy a escribir su correo."}]})

    await submit_task("Escribe mi correo en el formulario")
    card = panel.get_by_test_id("confirm-card")
    await expect(card).to_be_visible(timeout=UI_TIMEOUT_MS)

    elapsed = await click_stop_and_time(panel, session)

    assert elapsed < STOP_BUDGET_S, f"stop took {elapsed:.2f} s"
    await expect(card).to_have_count(0)
    await asyncio.sleep(0.3)
    assert await field_value("#correo") == ""
    assert [p for p in bridge_log.of("page.act") if p["op"] == "type"] == []
    await expect_halted(ext_env.bridge)
    await expect(panel.get_by_test_id("run-state")).to_contain_text("Detenido")


async def test_stop_during_wait(session, panel, ext_env, fake_llm, open_fixture, submit_task):
    await open_fixture("form.html")
    fake_llm.push({"steps": [{"action": "wait", "ms": 10000, "explanation": "Espero un momento."}]})

    await submit_task("Espera un momento")
    await wait_until(lambda: len(fake_llm.calls) == 1, what="the plan")
    await asyncio.sleep(0.3)
    assert session.running

    elapsed = await click_stop_and_time(panel, session)

    assert elapsed < STOP_BUDGET_S, f"stop took {elapsed:.2f} s"
    await expect_halted(ext_env.bridge)
    assert len(fake_llm.calls) == 1


async def test_resume_after_stop(session, panel, ext_env, fake_llm, open_fixture, submit_task):
    await open_fixture("form.html")
    fake_llm.delay = 10.0
    await submit_task("Haz algo que tarda")
    await expect(panel.get_by_test_id("stop")).to_be_visible(timeout=UI_TIMEOUT_MS)
    await click_stop_and_time(panel, session)
    status = panel.get_by_test_id("status")
    resume = panel.get_by_test_id("resume")
    await expect(status).to_have_text("Detenido")
    await expect(resume).to_be_visible()
    await expect(resume).to_have_text("Reanudar")
    await expect_halted(ext_env.bridge)

    await resume.click(timeout=UI_TIMEOUT_MS)

    await expect(status).to_have_text("Conectado", timeout=UI_TIMEOUT_MS)
    await expect(resume).to_be_hidden()
    state = await ext_env.bridge.request("page.state", timeout=10)
    assert state["url"].endswith("/form.html")


async def test_new_task_after_stop_needs_no_resume(session, panel, fake_llm, open_fixture, submit_task):
    await open_fixture("form.html")
    fake_llm.delay = 10.0
    await submit_task("Haz algo que tarda")
    await expect(panel.get_by_test_id("stop")).to_be_visible(timeout=UI_TIMEOUT_MS)
    await click_stop_and_time(panel, session)
    fake_llm.delay = 0.0
    fake_llm.push({"steps": [{"action": "done", "value": "Aquí estoy."}]})

    await submit_task("Ahora otra cosa")

    await wait_run_state(panel, "Listo.")
    await wait_run_state(panel, "Aquí estoy.")
    await expect(panel.get_by_test_id("status")).to_have_text("Conectado")



async def test_side_panel_strings_es(panel):
    assert await panel.evaluate("document.documentElement.lang") == "es"
    assert await panel.title() == "BABAS"
    await expect(panel.get_by_test_id("status")).to_have_text("Conectado")
    await expect(panel.get_by_test_id("task-input")).to_have_attribute("placeholder", PLACEHOLDER)
    await expect(panel.get_by_test_id("send")).to_have_text("Enviar")
    await expect(panel.get_by_test_id("stop")).to_have_text("Detener")
    await expect(panel.get_by_test_id("resume")).to_have_text("Reanudar")
    await expect(panel.get_by_test_id("settings").locator("summary")).to_have_text("Ajustes")
    await expect(panel.get_by_test_id("help")).to_be_hidden()
    await expect(panel.get_by_test_id("stop")).to_be_hidden()
    await expect(panel.get_by_test_id("resume")).to_be_hidden()


async def test_panel_shows_help_when_python_is_not_running(ext_env, panel):
    await ext_env.bridge.stop()

    await expect(panel.get_by_test_id("status")).to_have_text("Sin conexión con BABAS", timeout=UI_TIMEOUT_MS)
    await expect(panel.get_by_test_id("help")).to_have_text(HELP_TEXT)
    await expect(panel.get_by_test_id("send")).to_be_disabled()


async def test_settings_lists_sites_and_removal_takes_effect(ext_env, panel, work_page, fixture_url):
    await panel.get_by_test_id("settings").locator("summary").click()
    sites = panel.get_by_test_id("site")
    await expect(sites.filter(has_text="127.0.0.1")).to_have_count(1)
    await expect(sites.filter(has_text="gob.mx")).to_have_count(1)
    await ext_env.bridge.request("tab.goto", {"url": fixture_url("form.html")}, timeout=10)

    await panel.get_by_role("button", name="Quitar 127.0.0.1").click(timeout=UI_TIMEOUT_MS)

    await expect(sites.filter(has_text="127.0.0.1")).to_have_count(0)
    with pytest.raises(DriverError) as caught:
        await ext_env.bridge.request("tab.goto", {"url": fixture_url("form.html")}, timeout=10)
    assert caught.value.code == ErrorCode.DOMAIN_NOT_ALLOWED
