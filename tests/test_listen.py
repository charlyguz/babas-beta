import asyncio
import sys
import threading
import types
from typing import Any, List, Optional

import pytest

import babas_listen
from babas.config import Settings
from babas_listen import VoiceInputMode, VoiceListener, VoiceTranscriber

WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + bytes(range(64))


class FakeSTTError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class FakeBackend:
    def __init__(self, result: str = "hola", error: Optional[BaseException] = None, delay: float = 0.0) -> None:
        self.result = result
        self.error = error
        self.delay = delay
        self.calls: List[Any] = []
        self.closed = 0
        self.cancelled = False
        self.loops: List[Any] = []

    async def transcribe(self, audio: bytes, mime: str, language: str) -> str:
        self.calls.append((audio, mime, language))
        self.loops.append(asyncio.get_running_loop())
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.error is not None:
            raise self.error
        return self.result

    async def close(self) -> None:
        self.closed += 1


@pytest.fixture(autouse=True)
def clean_default(monkeypatch):
    monkeypatch.setattr(babas_listen, "_default_settings", None)


def stt_threads() -> List[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == "babas-stt" and thread.is_alive()]


def make(backend: FakeBackend, language: str = "es", **kwargs: Any) -> VoiceTranscriber:
    return VoiceTranscriber(settings=Settings(language=language), stt=backend, **kwargs)


def test_it_delegates_to_the_backend_with_a_wav_and_the_language_of_the_settings():
    backend = FakeBackend(" busca recetas ")
    transcriber = make(backend, "es")
    try:
        assert transcriber.transcribe(WAV) == "busca recetas"
        assert backend.calls == [(WAV, "audio/wav", "es")]
        assert transcriber.last_error is None
        make_english = make(backend, "en")
        assert make_english.transcribe(WAV) == "busca recetas"
        assert backend.calls[-1][2] == "en"
        make_english.close()
    finally:
        transcriber.close()


def test_an_explicit_language_wins():
    backend = FakeBackend()
    transcriber = make(backend, "es")
    try:
        transcriber.transcribe(WAV, "en")
        assert backend.calls[0][2] == "en"
    finally:
        transcriber.close()


def test_the_language_comes_from_the_configured_settings_by_default(monkeypatch):
    babas_listen.configure(Settings(language="en"))
    backend = FakeBackend()
    transcriber = VoiceTranscriber(stt=backend)
    try:
        transcriber.transcribe(WAV)
        assert backend.calls[0][2] == "en"
    finally:
        transcriber.close()


def test_the_backend_is_built_once_from_the_settings(monkeypatch):
    built: List[Settings] = []
    backend = FakeBackend()
    module = types.ModuleType("babas.stt")
    module.make_stt = lambda settings: built.append(settings) or backend  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "babas.stt", module)
    settings = Settings(language="es", stt_provider="chat", stt_model="m")
    transcriber = VoiceTranscriber(settings=settings)
    try:
        assert transcriber.transcribe(WAV) == "hola"
        assert transcriber.transcribe(WAV) == "hola"
        assert built == [settings]
        assert len(backend.calls) == 2
    finally:
        transcriber.close()


def test_the_api_key_argument_still_works_as_the_speech_key(monkeypatch):
    built: List[Settings] = []
    module = types.ModuleType("babas.stt")
    module.make_stt = lambda settings: built.append(settings) or FakeBackend()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "babas.stt", module)
    transcriber = VoiceTranscriber(api_key="sk-explicit", settings=Settings())
    try:
        transcriber.transcribe(WAV)
        assert built[0].stt_api_key == "sk-explicit"
    finally:
        transcriber.close()


def test_a_backend_error_shows_its_message_and_not_a_traceback(capsys):
    message = "El dictado por voz no está configurado. Pida a quien instaló BABAS que revise BABAS_STT_PROVIDER."
    transcriber = make(FakeBackend(error=FakeSTTError("not_configured", message)))
    try:
        assert transcriber.transcribe(WAV) is None
        assert transcriber.last_error == message
    finally:
        transcriber.close()
    captured = capsys.readouterr()
    assert "Dictado por voz" in captured.out and "BABAS_STT_PROVIDER" in captured.out
    assert "✗" in captured.out
    assert "Traceback" not in captured.out + captured.err


@pytest.mark.parametrize("code", ["empty", "bad_audio"])
def test_soft_errors_are_a_warning_card(capsys, code):
    transcriber = make(FakeBackend(error=FakeSTTError(code, "No escuché ninguna voz.")))
    try:
        assert transcriber.transcribe(WAV) is None
    finally:
        transcriber.close()
    out = capsys.readouterr().out
    assert "▲ Dictado por voz" in out and "No escuché ninguna voz." in out


def test_an_empty_transcript_is_none_without_an_error():
    transcriber = make(FakeBackend("   "))
    try:
        assert transcriber.transcribe(WAV) is None
        assert transcriber.last_error is None
    finally:
        transcriber.close()


def test_unexpected_errors_are_reported_not_raised(capsys):
    transcriber = make(FakeBackend(error=RuntimeError("boom")))
    try:
        assert transcriber.transcribe(WAV) is None
        assert transcriber.last_error == "Transcription error: boom"
    finally:
        transcriber.close()
    assert "Transcription error: boom" in capsys.readouterr().out


def test_a_missing_speech_module_is_explained(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "babas.stt", None)
    transcriber = VoiceTranscriber(settings=Settings())
    try:
        assert transcriber.transcribe(WAV) is None
        assert "babas.stt" in (transcriber.last_error or "")
        assert transcriber.is_available() is False
    finally:
        transcriber.close()
    assert "not available" in capsys.readouterr().out


def test_an_invalid_configuration_is_explained(monkeypatch, capsys):
    monkeypatch.setenv("BABAS_BRIDGE_PORT", "abc")
    transcriber = VoiceTranscriber(stt=FakeBackend())
    try:
        assert transcriber.transcribe(WAV) is None
        assert "Invalid configuration" in (transcriber.last_error or "") and "BABAS_BRIDGE_PORT" in (transcriber.last_error or "")
    finally:
        transcriber.close()


def test_a_slow_backend_times_out_and_is_cancelled(capsys):
    backend = FakeBackend(delay=30)
    transcriber = make(backend)
    transcriber.timeout = 0.2
    try:
        assert transcriber.transcribe(WAV) is None
        assert "too long" in (transcriber.last_error or "")
        deadline = threading.Event()
        for _ in range(50):
            if backend.cancelled:
                break
            deadline.wait(0.02)
        assert backend.cancelled
    finally:
        transcriber.close()
    assert "too long" in capsys.readouterr().out


def test_close_closes_the_backend_and_leaves_no_thread():
    backend = FakeBackend()
    transcriber = make(backend)
    transcriber.transcribe(WAV)
    assert len(stt_threads()) == 1
    transcriber.close()
    assert backend.closed == 1
    assert stt_threads() == []
    transcriber.close()


def test_every_call_runs_on_the_same_loop_so_the_client_can_be_reused():
    backend = FakeBackend()
    transcriber = make(backend)
    try:
        transcriber.transcribe(WAV)
        transcriber.transcribe(WAV)
        assert backend.loops[0] is backend.loops[1]
    finally:
        transcriber.close()


async def test_it_works_from_a_worker_thread_while_an_event_loop_runs():
    backend = FakeBackend("desde otro hilo")
    transcriber = make(backend)
    try:
        assert await asyncio.to_thread(transcriber.transcribe, WAV) == "desde otro hilo"
    finally:
        transcriber.close()


def test_is_available_needs_the_microphone_libraries(monkeypatch):
    transcriber = make(FakeBackend())
    monkeypatch.setattr(babas_listen, "SOUNDDEVICE_AVAILABLE", False)
    assert transcriber.is_available() is False
    monkeypatch.setattr(babas_listen, "SOUNDDEVICE_AVAILABLE", True)
    assert transcriber.is_available() is True
    assert VoiceTranscriber(settings=Settings()).is_available() is True


def test_numpy_audio_is_converted_to_wav():
    np = pytest.importorskip("numpy")
    backend = FakeBackend()
    transcriber = make(backend)
    try:
        transcriber.transcribe(np.zeros(1600, dtype="float32"))
        assert backend.calls[0][0][:4] == b"RIFF"
    finally:
        transcriber.close()


class TestListener:
    def listener(self, backend: FakeBackend) -> VoiceListener:
        listener = VoiceListener(Settings(language="es"))
        listener.transcriber.close()
        listener.transcriber = make(backend)
        return listener

    def test_it_prints_the_transcript(self, capsys):
        listener = self.listener(FakeBackend("abre youtube"))
        try:
            assert listener._transcribe(WAV) == "abre youtube"
        finally:
            listener.transcriber.close()
        out = capsys.readouterr().out
        assert "Transcribing..." in out and '"abre youtube"' in out

    def test_a_backend_error_is_not_followed_by_a_generic_failure(self, capsys):
        listener = self.listener(FakeBackend(error=FakeSTTError("provider", "El servicio de voz falló.")))
        try:
            assert listener._transcribe(WAV) is None
        finally:
            listener.transcriber.close()
        out = capsys.readouterr().out
        assert "El servicio de voz falló." in out and "Could not transcribe" not in out

    def test_an_empty_result_says_it_could_not_transcribe(self, capsys):
        listener = self.listener(FakeBackend(""))
        try:
            assert listener._transcribe(WAV) is None
        finally:
            listener.transcriber.close()
        assert "Could not transcribe" in capsys.readouterr().out

    def test_the_input_mode_passes_the_settings_down(self):
        mode = VoiceInputMode(Settings(language="en"))
        assert mode.listener.transcriber._settings.language == "en"
        mode.listener.transcriber.close()
