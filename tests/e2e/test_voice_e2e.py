import asyncio
import base64
import copy
import logging
import struct
import time
from typing import Any, Dict, List

import pytest
from fake_openai_server import CHAT_PATH, TRANSCRIPTIONS_PATH, FakeOpenAIServer
from helpers import FakeSTT
from playwright.async_api import expect

from babas.bridge.protocol import PINNED_EXTENSION_ID
from babas.config import Settings
from babas.stt import BAD_AUDIO_TEXT, PROVIDER_TEXT, STTError, make_stt

pytestmark = pytest.mark.e2e

FAKE_MICROPHONE = ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"]
# Without the fake UI flag headless Chromium never answers the permission prompt.
UNANSWERED_PROMPT = ["--use-fake-device-for-media-stream"]
PANEL_URL = f"chrome-extension://{PINNED_EXTENSION_ID}/sidepanel.html"
PERMISSION_URL = f"chrome-extension://{PINNED_EXTENSION_ID}/micpermission.html"

UI_TIMEOUT_MS = 10000
RECONNECT_TIMEOUT_MS = 30000
PROMPT_WATCHDOG_TIMEOUT_MS = 40000  # the panel gives up on an unanswered prompt after 20 s
SPEECH_S = 0.8
LONG_SPEECH_S = 1.5
SETTLE_S = 0.4
SAMPLE_RATE = 16000
SECRET_KEY = "sk-test-SECRET-0123456789"
TRANSCRIPT = "busque recetas de pozole"

LABEL_START = "Hablar"
LABEL_FINISH = "Terminar"
STATUS_STARTING = "Preparando el micrófono…"
STATUS_LISTENING = "Estoy escuchando…"
HINT_LISTENING = (
    "Cuando termine de hablar, presione «Terminar». "
    "Si hace una pausa de unos segundos, la grabación se detiene sola."
)
STATUS_TRANSCRIBING = "Escribiendo lo que dijo…"
GRANT_LABEL = "Dar permiso"
PERMISSION_TITLE = "Permiso del micrófono"
PERMISSION_DONE = "Listo. Ya puede cerrar esta pestaña y volver a BABAS."
TEXT_NOT_CONFIGURED = "La escritura por voz todavía no está lista. Por ahora puede escribir con el teclado."
TEXT_PROVIDER = "No pude escribir lo que dijo. Intente de nuevo en un momento o escriba con el teclado."
TEXT_EMPTY = "No alcancé a escuchar nada. Acérquese al micrófono, presione «Hablar» y hable con calma."
TEXT_BUSY = "Todavía estoy escribiendo su grabación anterior. Espere un momento e intente de nuevo."
TEXT_BAD_AUDIO = "La grabación no se escuchó bien. Intente de nuevo."
TEXT_NO_CONNECTION = (
    "No hay conexión con BABAS, por eso no pude escribir lo que dijo. "
    "Revise que la aplicación esté abierta e intente de nuevo."
)
TEXT_PERMISSION = (
    "BABAS necesita su permiso para usar el micrófono. Presione «Dar permiso». "
    "Cuando el navegador le pregunte por el micrófono, elija permitirlo. "
    "Luego regrese aquí y presione «Hablar»."
)

WATCH_MICROPHONE = """() => {
  const devices = navigator.mediaDevices;
  const original = devices.getUserMedia.bind(devices);
  window.__streams = [];
  devices.getUserMedia = async (constraints) => {
    const stream = await original(constraints);
    window.__streams.push(stream);
    return stream;
  };
}"""
COUNT_MICROPHONE_REQUESTS = """() => {
  const devices = navigator.mediaDevices;
  const original = devices.getUserMedia.bind(devices);
  window.__requests = 0;
  devices.getUserMedia = (constraints) => { window.__requests += 1; return original(constraints); };
}"""
TRACK_STATES = "() => window.__streams.flatMap((stream) => stream.getTracks().map((track) => track.readyState))"
DENY_MICROPHONE = """() => {
  navigator.mediaDevices.getUserMedia = () => Promise.reject(new DOMException('Permission denied', 'NotAllowedError'));
}"""
RESTORE_MICROPHONE = "() => { delete navigator.mediaDevices.getUserMedia; }"


@pytest.fixture
def chromium_extra_args(request):
    return list(getattr(request, "param", FAKE_MICROPHONE))


class CapturingSTT(FakeSTT):
    def __init__(self, result: Any = TRANSCRIPT, **kwargs: Any):
        super().__init__(result, **kwargs)
        self.audios: List[bytes] = []

    async def transcribe(self, audio: bytes, mime: str, language: str) -> str:
        self.audios.append(audio)
        return await super().transcribe(audio, mime, language)


@pytest.fixture
def stt(session):
    fake = CapturingSTT()
    session.language = "es"
    session.stt_factory = lambda: fake
    return fake


@pytest.fixture
def bridge_events(ext_env):
    seen: Dict[str, List[Dict[str, Any]]] = {"stt.transcribe": [], "task.start": []}

    def recorder(kind: str):
        async def record(payload: Dict[str, Any]) -> None:
            seen[kind].append(copy.deepcopy(payload))

        return record

    for kind in seen:
        ext_env.bridge.on(kind, recorder(kind))
    return seen


@pytest.fixture
def http_stt(session, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

    def install(server: FakeOpenAIServer, **env: str) -> None:
        backend = make_stt(Settings.from_env({"BABAS_STT_BASE_URL": server.base_url, "BABAS_STT_API_KEY": SECRET_KEY, **env}))
        session.language = "es"
        session.stt_factory = lambda: backend

    return install


@pytest.fixture
def fake_http_server():
    with FakeOpenAIServer() as running:
        yield running


async def watch_microphone(page) -> None:
    await page.evaluate(WATCH_MICROPHONE)


async def track_states(page) -> List[str]:
    return await page.evaluate(TRACK_STATES)


async def speak(page, seconds: float = SPEECH_S) -> None:
    mic = page.get_by_test_id("mic")
    await mic.click()
    await expect(mic).to_have_attribute("data-phase", "recording", timeout=UI_TIMEOUT_MS)
    await asyncio.sleep(seconds)
    await mic.click()


async def dictate(page, expected_text: str, seconds: float = SPEECH_S) -> None:
    await speak(page, seconds)
    await expect(page.get_by_test_id("task-input")).to_have_value(expected_text, timeout=UI_TIMEOUT_MS)
    await expect(page.get_by_test_id("mic")).to_have_attribute("data-phase", "idle")


async def open_second_panel(ext_env):
    other = await ext_env.context.new_page()
    await other.goto(PANEL_URL)
    await expect(other.get_by_test_id("status")).to_have_text("Conectado", timeout=UI_TIMEOUT_MS)
    return other


async def log_entry_count(page) -> int:
    return await page.locator("[data-testid=log-entry]").count()


async def expect_mic_ready(page) -> None:
    mic = page.get_by_test_id("mic")
    await expect(mic).to_be_enabled()
    await expect(mic).to_have_attribute("data-phase", "idle")
    await expect(page.get_by_test_id("mic-label")).to_have_text(LABEL_START)


async def expect_voice_error(page, text: str) -> None:
    await expect(page.get_by_test_id("voice-error")).to_have_text(text, timeout=UI_TIMEOUT_MS)
    await expect(page.get_by_test_id("voice-text")).to_have_count(0)
    await expect(page.get_by_test_id("voice-permission")).to_be_hidden()


async def expect_nothing_sent(session, panel, fake_llm, bridge_events) -> None:
    await asyncio.sleep(SETTLE_S)
    assert bridge_events["task.start"] == []
    assert fake_llm.calls == []
    assert not session.running
    assert await log_entry_count(panel) == 0
    await expect(panel.get_by_test_id("run-state")).to_be_hidden()


def parse_wav(wav: bytes) -> Dict[str, Any]:
    assert len(wav) >= 44, f"a WAV needs a 44 byte header, got {len(wav)} bytes"
    fields = struct.unpack("<4sI4s4sIHHIIHH4sI", wav[:44])
    names = ("riff", "riff_size", "wave", "fmt", "fmt_size", "format", "channels", "rate", "byte_rate", "align", "bits", "data", "data_size")
    return dict(zip(names, fields))


def peak_of(wav: bytes) -> int:
    count = (len(wav) - 44) // 2
    return max((abs(sample) for sample in struct.unpack(f"<{count}h", wav[44 : 44 + count * 2])), default=0)


async def test_microphone_is_opened_only_after_the_click(session, stt, panel, ext_env):
    await panel.add_init_script(f"({COUNT_MICROPHONE_REQUESTS})()")
    await panel.reload()
    await expect(panel.get_by_test_id("status")).to_have_text("Conectado", timeout=UI_TIMEOUT_MS)
    await expect_mic_ready(panel)
    await asyncio.sleep(SETTLE_S)
    assert await panel.evaluate("window.__requests") == 0

    await panel.get_by_test_id("mic").click()
    await expect(panel.get_by_test_id("mic")).to_have_attribute("data-phase", "recording", timeout=UI_TIMEOUT_MS)
    assert await panel.evaluate("window.__requests") == 1
    await asyncio.sleep(SPEECH_S)
    await panel.get_by_test_id("mic").click()
    await expect(panel.get_by_test_id("task-input")).to_have_value(TRANSCRIPT, timeout=UI_TIMEOUT_MS)
    assert await panel.evaluate("window.__requests") == 1


async def test_dictation_fills_the_text_box_and_does_not_send(session, stt, panel, fake_llm, bridge_events):
    mic = panel.get_by_test_id("mic")
    task_input = panel.get_by_test_id("task-input")
    await watch_microphone(panel)
    await expect_mic_ready(panel)
    await expect(mic).to_be_visible()
    await expect(panel.get_by_test_id("voice-level")).to_be_hidden()

    await mic.click()

    await expect(mic).to_have_attribute("data-phase", "recording", timeout=UI_TIMEOUT_MS)
    await expect(panel.get_by_test_id("mic-label")).to_have_text(LABEL_FINISH)
    await expect(panel.get_by_test_id("voice-text")).to_have_text(STATUS_LISTENING)
    await expect(panel.get_by_test_id("voice-hint")).to_have_text(HINT_LISTENING)
    await expect(panel.get_by_test_id("voice-level")).to_be_visible()
    await expect(mic).to_be_enabled()
    assert await track_states(panel) == ["live"]
    assert await task_input.input_value() == ""
    await asyncio.sleep(SPEECH_S)

    await mic.click()

    await expect(task_input).to_have_value(TRANSCRIPT, timeout=UI_TIMEOUT_MS)
    await expect_mic_ready(panel)
    await expect(panel.get_by_test_id("voice-status")).to_have_text("")
    await expect(panel.get_by_test_id("voice-error")).to_have_count(0)
    await expect(panel.get_by_test_id("voice-level")).to_be_hidden()
    assert await panel.evaluate("document.activeElement.dataset.testid") == "task-input"
    assert await track_states(panel) == ["ended"]
    assert len(stt.calls) == 1
    assert stt.calls[0][1:] == ("audio/wav", "es")
    await expect(panel.get_by_test_id("send")).to_be_enabled()
    await expect_nothing_sent(session, panel, fake_llm, bridge_events)
    assert await task_input.input_value() == TRANSCRIPT


async def test_the_text_box_is_filled_by_python_and_not_before(session, stt, panel, fake_llm, bridge_events):
    gate = asyncio.Event()
    stt.gate = gate
    mic = panel.get_by_test_id("mic")
    task_input = panel.get_by_test_id("task-input")
    try:
        await speak(panel)

        await expect(mic).to_have_attribute("data-phase", "transcribing", timeout=UI_TIMEOUT_MS)
        await asyncio.wait_for(stt.started.wait(), 10)
        await expect(panel.get_by_test_id("voice-text")).to_have_text(STATUS_TRANSCRIBING)
        await expect(mic).to_be_disabled()
        await expect(panel.get_by_test_id("voice-level")).to_be_hidden()
        await asyncio.sleep(SETTLE_S)
        assert await task_input.input_value() == ""
        assert len(bridge_events["stt.transcribe"]) == 1
    finally:
        gate.set()

    await expect(task_input).to_have_value(TRANSCRIPT, timeout=UI_TIMEOUT_MS)
    await expect_mic_ready(panel)
    await expect_nothing_sent(session, panel, fake_llm, bridge_events)


async def test_existing_text_gets_a_single_space_separator(session, stt, panel, fake_llm, bridge_events):
    task_input = panel.get_by_test_id("task-input")
    await task_input.fill("hola")

    await dictate(panel, f"hola {TRANSCRIPT}")

    stt.result = "  quiero   ver\nvideos  "
    await dictate(panel, f"hola {TRANSCRIPT} quiero ver videos")

    await task_input.fill("hola ")
    stt.result = "gracias"
    await dictate(panel, "hola gracias")
    await expect_nothing_sent(session, panel, fake_llm, bridge_events)


async def test_python_receives_a_valid_16khz_mono_wav(session, stt, panel, bridge_events):
    started = time.monotonic()
    await speak(panel, LONG_SPEECH_S)
    await expect(panel.get_by_test_id("task-input")).to_have_value(TRANSCRIPT, timeout=UI_TIMEOUT_MS)
    wall_ms = (time.monotonic() - started) * 1000

    (event,) = bridge_events["stt.transcribe"]
    assert set(event) == {"request_id", "audio_base64", "mime", "duration_ms"}
    assert isinstance(event["request_id"], str) and 1 <= len(event["request_id"]) <= 128
    assert event["mime"] == "audio/wav"
    wav = base64.b64decode(event["audio_base64"], validate=True)
    assert stt.audios == [wav]

    header = parse_wav(wav)
    assert (header["riff"], header["wave"], header["fmt"], header["data"]) == (b"RIFF", b"WAVE", b"fmt ", b"data")
    assert header["fmt_size"] == 16 and header["format"] == 1
    assert (header["channels"], header["rate"], header["bits"]) == (1, SAMPLE_RATE, 16)
    assert header["byte_rate"] == SAMPLE_RATE * 2 and header["align"] == 2
    assert header["riff_size"] == len(wav) - 8
    assert header["data_size"] == len(wav) - 44 and header["data_size"] % 2 == 0
    samples = header["data_size"] // 2
    assert isinstance(event["duration_ms"], int)
    assert abs(event["duration_ms"] - samples * 1000 / SAMPLE_RATE) <= 1
    assert LONG_SPEECH_S * 1000 * 0.6 <= event["duration_ms"] <= wall_ms + 50, (event["duration_ms"], wall_ms)
    assert len(wav) <= 3 * 1024 * 1024
    assert peak_of(wav) > 1000, "the recording is silent: the fake microphone signal did not reach the WAV"


CLICK_FINISH_AS_SOON_AS_RECORDING = """() => new Promise((resolve) => {
  const mic = document.querySelector('[data-testid=mic]');
  const check = () => { if (mic.dataset.phase === 'recording') { mic.click(); resolve(); } else { setTimeout(check, 2); } };
  check();
})"""


async def test_a_recording_that_is_too_short_is_never_sent(session, stt, panel, fake_llm, bridge_events):
    await watch_microphone(panel)
    await panel.get_by_test_id("mic").click()

    await panel.evaluate(CLICK_FINISH_AS_SOON_AS_RECORDING)

    await expect_voice_error(panel, TEXT_EMPTY)
    await expect_mic_ready(panel)
    assert await track_states(panel) == ["ended"]
    assert bridge_events["stt.transcribe"] == [] and stt.calls == []
    await expect_nothing_sent(session, panel, fake_llm, bridge_events)

    await dictate(panel, TRANSCRIPT)
    await expect(panel.get_by_test_id("voice-error")).to_have_count(0)


@pytest.mark.parametrize(
    "result, text",
    [
        pytest.param(None, TEXT_NOT_CONFIGURED, id="not_configured"),
        pytest.param(RuntimeError("boom"), TEXT_PROVIDER, id="provider_crash"),
        pytest.param(STTError("provider", PROVIDER_TEXT), TEXT_PROVIDER, id="provider"),
        pytest.param("   ", TEXT_EMPTY, id="empty"),
        pytest.param(STTError("bad_audio", BAD_AUDIO_TEXT), TEXT_BAD_AUDIO, id="bad_audio"),
    ],
)
async def test_backend_errors_show_a_spanish_message_and_the_mic_works_again(session, stt, panel, fake_llm, bridge_events, result, text):
    task_input = panel.get_by_test_id("task-input")
    await watch_microphone(panel)
    await task_input.fill("hola")
    if result is None:
        session.stt_factory = None
    else:
        stt.result = result

    await speak(panel)

    await expect_voice_error(panel, text)
    await expect_mic_ready(panel)
    assert await task_input.input_value() == "hola"
    assert await track_states(panel) == ["ended"]
    assert len(bridge_events["stt.transcribe"]) == 1
    assert len(stt.calls) == (0 if result is None else 1)
    await expect_nothing_sent(session, panel, fake_llm, bridge_events)

    session.stt_factory = lambda: stt
    stt.result = "ya funciona"
    await dictate(panel, "hola ya funciona")
    await expect(panel.get_by_test_id("voice-error")).to_have_count(0)
    assert await track_states(panel) == ["ended", "ended"]


async def test_a_second_dictation_while_python_is_busy_gets_the_busy_message(session, stt, panel, ext_env, bridge_events):
    gate = asyncio.Event()
    stt.gate = gate
    other = None
    try:
        await speak(panel)
        await expect(panel.get_by_test_id("mic")).to_have_attribute("data-phase", "transcribing", timeout=UI_TIMEOUT_MS)
        await asyncio.wait_for(stt.started.wait(), 10)
        other = await open_second_panel(ext_env)

        await speak(other)

        await expect_voice_error(other, TEXT_BUSY)
        await expect_mic_ready(other)
        assert len(stt.calls) == 1, "the busy recording must never reach the speech backend"
        assert len(bridge_events["stt.transcribe"]) == 2
        assert await other.get_by_test_id("task-input").input_value() == ""
        await expect(panel.get_by_test_id("mic")).to_have_attribute("data-phase", "transcribing")
        assert await panel.get_by_test_id("task-input").input_value() == ""
    finally:
        gate.set()

    await expect(panel.get_by_test_id("task-input")).to_have_value(TRANSCRIPT, timeout=UI_TIMEOUT_MS)
    assert await other.get_by_test_id("task-input").input_value() == ""
    stt.gate = None
    stt.result = "otra vez"
    await dictate(other, "otra vez")
    await other.close()


async def test_losing_the_connection_while_recording_says_so_and_releases_the_microphone(session, stt, panel, ext_env, bridge_events):
    mic = panel.get_by_test_id("mic")
    await watch_microphone(panel)
    await mic.click()
    await expect(mic).to_have_attribute("data-phase", "recording", timeout=UI_TIMEOUT_MS)
    assert await track_states(panel) == ["live"]

    await ext_env.bridge.stop()

    await expect_voice_error(panel, TEXT_NO_CONNECTION)
    await expect(panel.get_by_test_id("status")).to_have_text("Sin conexión con BABAS")
    await expect(mic).to_be_disabled()
    await expect(panel.get_by_test_id("mic-label")).to_have_text(LABEL_START)
    assert await track_states(panel) == ["ended"]
    assert bridge_events["stt.transcribe"] == [] and stt.calls == []
    assert await panel.get_by_test_id("task-input").input_value() == ""

    await ext_env.bridge.start()

    await expect(panel.get_by_test_id("status")).to_have_text("Conectado", timeout=RECONNECT_TIMEOUT_MS)
    await expect_mic_ready(panel)
    await dictate(panel, TRANSCRIPT)
    await expect(panel.get_by_test_id("voice-error")).to_have_count(0)


async def test_losing_the_connection_while_waiting_for_the_text_says_so(session, stt, panel, ext_env, bridge_events):
    gate = asyncio.Event()
    stt.gate = gate
    try:
        await speak(panel)
        await expect(panel.get_by_test_id("mic")).to_have_attribute("data-phase", "transcribing", timeout=UI_TIMEOUT_MS)
        await asyncio.wait_for(stt.started.wait(), 10)

        await ext_env.bridge.stop()

        await expect_voice_error(panel, TEXT_NO_CONNECTION)
        assert await panel.get_by_test_id("task-input").input_value() == ""
    finally:
        gate.set()
    await asyncio.sleep(SETTLE_S)

    await ext_env.bridge.start()

    await expect(panel.get_by_test_id("status")).to_have_text("Conectado", timeout=RECONNECT_TIMEOUT_MS)
    await expect_mic_ready(panel)
    stt.gate = None
    await dictate(panel, TRANSCRIPT)
    assert await panel.get_by_test_id("task-input").input_value() == TRANSCRIPT


async def test_a_dictated_task_is_sent_like_a_typed_one_and_nothing_is_logged(session, stt, panel, fake_llm, bridge_events, caplog):
    spoken = "quiero ver recetas de pozole rojo"
    answer = "Ya encontré las recetas."
    stt.result = spoken
    fake_llm.push({"steps": [{"action": "done", "value": answer}]})
    task_input = panel.get_by_test_id("task-input")

    with caplog.at_level(logging.DEBUG, logger="babas"):
        await dictate(panel, spoken)
        await expect_nothing_sent(session, panel, fake_llm, bridge_events)
        dictation_log = caplog.text

        await panel.get_by_test_id("send").click()

        run_state = panel.get_by_test_id("run-state")
        await expect(run_state).to_contain_text("Listo.", timeout=30000)
        await expect(run_state).to_contain_text(answer)

    assert bridge_events["task.start"] == [{"text": spoken}]
    assert len(fake_llm.calls) == 1 and spoken in fake_llm.calls[0][1]
    rows = await panel.eval_on_selector_all("[data-testid=log-entry]", "els => els.map(e => [e.dataset.kind, e.textContent])")
    assert ["user", spoken] in rows and ["result", answer] in rows
    await expect(task_input).to_have_value("")
    await expect(panel.get_by_test_id("send")).to_be_enabled()
    await expect(panel.get_by_test_id("status")).to_have_text("Conectado")
    await expect_mic_ready(panel)

    assert "voice transcription" in dictation_log, "the dictation did not log anything: this check would pass vacuously"
    audio = bridge_events["stt.transcribe"][0]["audio_base64"]
    for logged in (dictation_log, caplog.text):
        assert spoken not in logged
        assert "pozole" not in logged
        assert audio[:48] not in logged

    stt.result = "gracias"
    await dictate(panel, "gracias")


async def test_a_whisper_endpoint_receives_the_recorded_wav(session, panel, bridge_events, fake_http_server, http_stt, caplog):
    fake_http_server.queue_transcription(TRANSCRIPT)
    http_stt(fake_http_server, BABAS_STT_PROVIDER="whisper")

    with caplog.at_level(logging.DEBUG, logger="babas"):
        await dictate(panel, TRANSCRIPT)

    (request,) = fake_http_server.requests
    sent = base64.b64decode(bridge_events["stt.transcribe"][0]["audio_base64"])
    assert request.path == TRANSCRIPTIONS_PATH
    assert (request.model, request.language) == ("whisper-1", "es")
    assert (request.audio_filename, request.audio_content_type) == ("audio.wav", "audio/wav")
    assert request.audio_bytes == len(sent) and parse_wav(sent)["rate"] == SAMPLE_RATE
    assert request.headers["authorization"] == f"Bearer {SECRET_KEY}"
    assert SECRET_KEY not in caplog.text and TRANSCRIPT not in caplog.text
    assert SECRET_KEY not in await panel.content()


async def test_a_chat_audio_endpoint_receives_the_recorded_wav(session, panel, bridge_events, fake_http_server, http_stt, caplog):
    fake_http_server.queue_completion(f"  «{TRANSCRIPT}»  ")
    http_stt(fake_http_server, BABAS_STT_PROVIDER="chat", BABAS_STT_MODEL="omni-model")

    with caplog.at_level(logging.DEBUG, logger="babas"):
        await dictate(panel, TRANSCRIPT)

    (request,) = fake_http_server.requests
    sent = base64.b64decode(bridge_events["stt.transcribe"][0]["audio_base64"])
    assert (request.path, request.model) == (CHAT_PATH, "omni-model")
    assert request.audio_format == "wav" and request.audio_bytes == len(sent)
    assert base64.b64decode(request.input_audio["data"]) == sent
    assert request.text_prompt and "Transcribe" in request.text_prompt
    assert SECRET_KEY not in caplog.text and TRANSCRIPT not in caplog.text


async def test_a_chat_model_that_heard_no_speech_gives_the_empty_message(session, panel, fake_http_server, http_stt):
    fake_http_server.queue_completion("[SIN_VOZ]")
    http_stt(fake_http_server, BABAS_STT_PROVIDER="chat", BABAS_STT_MODEL="omni-model")

    await speak(panel)

    await expect_voice_error(panel, TEXT_EMPTY)
    await expect_mic_ready(panel)
    assert await panel.get_by_test_id("task-input").input_value() == ""


async def test_a_rejected_key_gives_the_provider_message_without_leaking_it(session, panel, fake_http_server, http_stt, caplog):
    fake_http_server.queue_error(401, f"Incorrect API key provided: {SECRET_KEY}", code="invalid_api_key")
    http_stt(fake_http_server, BABAS_STT_PROVIDER="whisper")

    with caplog.at_level(logging.DEBUG, logger="babas"):
        await speak(panel)
        await expect_voice_error(panel, TEXT_PROVIDER)

    await expect_mic_ready(panel)
    assert len(fake_http_server.requests) == 1
    assert SECRET_KEY not in caplog.text
    assert SECRET_KEY not in await panel.content()
    assert await panel.get_by_test_id("task-input").input_value() == ""


@pytest.mark.parametrize("chromium_extra_args", [FAKE_MICROPHONE], indirect=True)
async def test_a_refused_microphone_offers_the_permission_page_and_then_works(session, stt, panel, ext_env, bridge_events):
    mic = panel.get_by_test_id("mic")
    await watch_microphone(panel)
    await panel.evaluate(DENY_MICROPHONE)

    await mic.click()

    await expect(panel.get_by_test_id("voice-error")).to_have_text(TEXT_PERMISSION, timeout=UI_TIMEOUT_MS)
    grant = panel.get_by_test_id("voice-permission")
    await expect(grant).to_be_visible()
    await expect(grant).to_have_text(GRANT_LABEL)
    await expect_mic_ready(panel)
    assert await track_states(panel) == []
    assert bridge_events["stt.transcribe"] == []

    async with ext_env.context.expect_page() as opened:
        await grant.click()
    permission = await opened.value
    await permission.wait_for_load_state()
    assert permission.url == PERMISSION_URL
    assert await permission.title() == PERMISSION_TITLE
    await permission.get_by_test_id("allow-mic").click()
    await expect(permission.get_by_test_id("result")).to_have_text(PERMISSION_DONE, timeout=UI_TIMEOUT_MS)
    await expect(permission.get_by_test_id("allow-mic")).to_be_hidden()
    await permission.close()

    await panel.bring_to_front()
    await panel.evaluate(RESTORE_MICROPHONE)
    await dictate(panel, TRANSCRIPT)
    await expect(grant).to_be_hidden()
    await expect(panel.get_by_test_id("voice-error")).to_have_count(0)


@pytest.mark.parametrize("chromium_extra_args", [UNANSWERED_PROMPT], indirect=True)
async def test_a_permission_prompt_nobody_answers_ends_with_the_grant_button(session, stt, panel, bridge_events):
    mic = panel.get_by_test_id("mic")

    await mic.click()

    await expect(mic).to_have_attribute("data-phase", "starting", timeout=UI_TIMEOUT_MS)
    await expect(panel.get_by_test_id("voice-text")).to_have_text(STATUS_STARTING)
    await expect(mic).to_be_disabled()
    await expect(panel.get_by_test_id("voice-permission")).to_be_hidden()
    await expect(panel.get_by_test_id("voice-permission")).to_be_visible(timeout=PROMPT_WATCHDOG_TIMEOUT_MS)
    await expect(panel.get_by_test_id("voice-permission")).to_have_text(GRANT_LABEL)
    await expect(panel.get_by_test_id("voice-error")).to_have_text(TEXT_PERMISSION)
    await expect_mic_ready(panel)
    assert bridge_events["stt.transcribe"] == [] and stt.calls == []
