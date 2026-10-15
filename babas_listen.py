# preview beta V0.1 tested done formating done
from __future__ import annotations

import asyncio
import io
import sys
import threading
import wave
from dataclasses import replace
from typing import Any, Awaitable, Callable, Optional, Union

from dotenv import load_dotenv

from babas import ui

load_dotenv()

try:
    import sounddevice as sd
    import numpy as np
    SOUNDDEVICE_AVAILABLE = True
except ImportError:
    SOUNDDEVICE_AVAILABLE = False
    print("sounddevice not installed. Run: pip install sounddevice numpy")

try:
    import keyboard
    KEYBOARD_AVAILABLE = True
except ImportError:
    KEYBOARD_AVAILABLE = False

SAMPLE_RATE = 16000
CHANNELS = 1
SILENCE_THRESHOLD = 0.01
SILENCE_DURATION = 1.5
MAX_DURATION = 30
MIN_DURATION = 0.5
TRANSCRIBE_TIMEOUT_S = 120
WAV_MIME = "audio/wav"
SOFT_ERROR_CODES = ("empty", "bad_audio")

_default_settings: Any = None


def configure(settings: Any) -> None:
    global _default_settings
    _default_settings = settings


class AudioRecorder:
    def __init__(self):
        self.recording = False
        self.audio_data = []
        self.sample_rate = SAMPLE_RATE
        self._stop_event = threading.Event()

    def _is_silence(self, audio_chunk: np.ndarray) -> bool:
        rms = np.sqrt(np.mean(audio_chunk**2))
        return rms < SILENCE_THRESHOLD

    def record_until_silence(self, max_duration: float = MAX_DURATION, silence_duration: float = SILENCE_DURATION, on_start: Optional[Callable] = None, on_sound: Optional[Callable] = None) -> Optional[np.ndarray]:
        if not SOUNDDEVICE_AVAILABLE:
            print("sounddevice not available")
            return None
        self.audio_data = []
        self.recording = True
        self._stop_event.clear()
        silence_chunks = 0
        chunks_per_second = int(self.sample_rate / 1024)
        silence_chunks_needed = int(silence_duration * chunks_per_second)
        has_started = False
        if on_start:
            on_start()
        try:
            def callback(indata, frames, time, status):
                nonlocal silence_chunks, has_started
                if self._stop_event.is_set():
                    raise sd.CallbackAbort()
                chunk = indata.copy().flatten()
                if self._is_silence(chunk):
                    if has_started:
                        silence_chunks += 1
                else:
                    if not has_started:
                        has_started = True
                        if on_sound:
                            on_sound()
                    silence_chunks = 0
                    self.audio_data.append(chunk)
                if has_started and silence_chunks >= silence_chunks_needed:
                    raise sd.CallbackAbort()
                if has_started:
                    self.audio_data.append(chunk)
            with sd.InputStream(samplerate=self.sample_rate, channels=CHANNELS, dtype='float32', blocksize=1024, callback=callback):
                self._stop_event.wait(timeout=max_duration)
        except sd.CallbackAbort:
            pass
        except Exception as e:
            print(f"Recording error: {e}")
            return None
        finally:
            self.recording = False
        if not self.audio_data:
            return None
        audio = np.concatenate(self.audio_data)
        duration = len(audio) / self.sample_rate
        if duration < MIN_DURATION:
            return None
        return audio

    def record_until_stopped(self, max_duration: float = 60) -> Optional[np.ndarray]:
        if not SOUNDDEVICE_AVAILABLE:
            print("sounddevice not available")
            return None
        self.audio_data = []
        self.recording = True
        self._stop_event.clear()
        try:
            def callback(indata, frames, time, status):
                if self._stop_event.is_set():
                    raise sd.CallbackAbort()
                self.audio_data.append(indata.copy().flatten())
            with sd.InputStream(samplerate=self.sample_rate, channels=CHANNELS, dtype='float32', blocksize=1024, callback=callback):
                self._stop_event.wait(timeout=max_duration)
        except sd.CallbackAbort:
            pass
        except Exception as e:
            print(f"Recording error: {e}")
            return None
        finally:
            self.recording = False
        if not self.audio_data:
            return None
        audio = np.concatenate(self.audio_data)
        duration = len(audio) / self.sample_rate
        if duration < 0.3:
            return None
        return audio

    def record_for_duration(self, duration: float) -> Optional[np.ndarray]:
        if not SOUNDDEVICE_AVAILABLE:
            return None
        try:
            print(f"   Recording {duration}s...")
            audio = sd.rec(int(duration * self.sample_rate), samplerate=self.sample_rate, channels=CHANNELS, dtype='float32')
            sd.wait()
            return audio.flatten()
        except Exception as e:
            print(f"Recording error: {e}")
            return None

    def record_push_to_talk(self, key: str = "space", already_pressed: bool = False) -> Optional[np.ndarray]:
        if not SOUNDDEVICE_AVAILABLE:
            print("sounddevice not available")
            return None
        if not KEYBOARD_AVAILABLE:
            print("keyboard not available. Run: pip install keyboard")
            return None
        self.audio_data = []
        self._stop_event.clear()
        if not already_pressed:
            print(f"   Hold [{key.upper()}] to talk...")
            keyboard.wait(key)
            print(f"   RECORDING... (release [{key.upper()}] to stop)")
        self.recording = True
        try:
            def callback(indata, frames, time, status):
                if self._stop_event.is_set() or not self.recording:
                    raise sd.CallbackAbort()
                self.audio_data.append(indata.copy().flatten())
            with sd.InputStream(samplerate=self.sample_rate, channels=CHANNELS, dtype='float32', blocksize=1024, callback=callback):
                while keyboard.is_pressed(key):
                    import time as time_module
                    time_module.sleep(0.01)
                self.recording = False
        except sd.CallbackAbort:
            pass
        except Exception as e:
            print(f"Error: {e}")
            return None
        finally:
            self.recording = False
        if not self.audio_data:
            return None
        audio = np.concatenate(self.audio_data)
        duration = len(audio) / self.sample_rate
        if duration < MIN_DURATION:
            print(f"   Too short ({duration:.1f}s)")
            return None
        print(f"   Recorded ({duration:.1f}s)")
        return audio

    def record_with_enter(self) -> Optional[np.ndarray]:
        if not SOUNDDEVICE_AVAILABLE:
            return None
        self.audio_data = []
        self.recording = True
        self._stop_event.clear()
        print("   RECORDING... (press ENTER to stop)")
        def wait_for_enter():
            input()
            self._stop_event.set()
        enter_thread = threading.Thread(target=wait_for_enter, daemon=True)
        enter_thread.start()
        try:
            def callback(indata, frames, time, status):
                if self._stop_event.is_set():
                    raise sd.CallbackAbort()
                self.audio_data.append(indata.copy().flatten())
            with sd.InputStream(samplerate=self.sample_rate, channels=CHANNELS, dtype='float32', blocksize=1024, callback=callback):
                self._stop_event.wait(timeout=MAX_DURATION)
        except sd.CallbackAbort:
            pass
        except Exception as e:
            print(f"Error: {e}")
            return None
        finally:
            self.recording = False
        if not self.audio_data:
            return None
        audio = np.concatenate(self.audio_data)
        duration = len(audio) / self.sample_rate
        if duration < MIN_DURATION:
            print(f"   Too short ({duration:.1f}s)")
            return None
        print(f"   Recorded ({duration:.1f}s)")
        return audio

    def stop(self):
        self._stop_event.set()
        self.recording = False

    def audio_to_wav_bytes(self, audio: np.ndarray) -> bytes:
        audio_int16 = (audio * 32767).astype(np.int16)
        buffer = io.BytesIO()
        with wave.open(buffer, 'wb') as wav:
            wav.setnchannels(CHANNELS)
            wav.setsampwidth(2)
            wav.setframerate(self.sample_rate)
            wav.writeframes(audio_int16.tobytes())
        buffer.seek(0)
        return buffer.read()

class _AsyncRunner:
    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    @staticmethod
    def _serve(loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    def _ensure(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                thread = threading.Thread(target=self._serve, args=(loop,), name="babas-stt", daemon=True)
                thread.start()
                self._loop, self._thread = loop, thread
            return self._loop

    def run(self, coro: Awaitable[Any], timeout: float) -> Any:
        future = asyncio.run_coroutine_threadsafe(coro, self._ensure())  # type: ignore[arg-type]
        try:
            return future.result(timeout)
        except BaseException:
            future.cancel()
            raise

    def close(self) -> None:
        with self._lock:
            loop, thread = self._loop, self._thread
            self._loop = self._thread = None
        if loop is None or thread is None:
            return
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        if not thread.is_alive():
            loop.close()


def _failure_text(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    message = getattr(exc, "message", None)
    if isinstance(code, str) and isinstance(message, str) and message:
        return message
    if isinstance(exc, ImportError):
        return "The speech-to-text module (babas.stt) is not available."
    if isinstance(exc, ValueError):
        return f"Invalid configuration: {exc}"
    if isinstance(exc, TimeoutError) or type(exc).__name__ == "TimeoutError":
        return "The speech service took too long to answer. Try again."
    return f"Transcription error: {exc}"


class VoiceTranscriber:
    def __init__(self, api_key: Optional[str] = None, settings: Any = None, stt: Any = None):
        self._api_key = api_key
        self._settings = settings
        self._resolved: Any = None
        self._stt = stt
        self._runner = _AsyncRunner()
        self.timeout = TRANSCRIBE_TIMEOUT_S
        self.last_error: Optional[str] = None

    def _load_settings(self) -> Any:
        if self._resolved is None:
            base = self._settings
            if base is None:
                from babas.config import Settings

                base = _default_settings if _default_settings is not None else Settings.from_env()
            self._resolved = replace(base, stt_api_key=self._api_key) if self._api_key else base
        return self._resolved

    def _backend(self) -> Any:
        if self._stt is None:
            from babas.stt import make_stt

            self._stt = make_stt(self._load_settings())
        return self._stt

    def is_available(self) -> bool:
        if not SOUNDDEVICE_AVAILABLE:
            return False
        if self._stt is not None:
            return True
        try:
            import babas.stt  # noqa: F401
        except ImportError:
            return False
        return True

    def _wav(self, audio: Union[bytes, "np.ndarray"]) -> bytes:
        if isinstance(audio, (bytes, bytearray)):
            return bytes(audio)
        return AudioRecorder().audio_to_wav_bytes(audio)

    def _report(self, exc: BaseException) -> None:
        self.last_error = _failure_text(exc)
        term = ui.detect(sys.stdout)
        if getattr(exc, "code", None) in SOFT_ERROR_CODES:
            ui.emit(ui.card(term, "Dictado por voz", [self.last_error], kind="warn"))
        else:
            ui.emit(ui.error_panel(term, self.last_error, title="Dictado por voz"))

    def transcribe(self, audio: Union[bytes, "np.ndarray"], language: Optional[str] = None) -> Optional[str]:
        self.last_error = None
        try:
            wav = self._wav(audio)
            language = language or self._load_settings().language
            text = self._runner.run(self._backend().transcribe(wav, WAV_MIME, language), self.timeout)
        except Exception as exc:
            self._report(exc)
            return None
        text = (text or "").strip()
        return text or None

    def close(self) -> None:
        backend, self._stt = self._stt, None
        closer = getattr(backend, "close", None)
        if closer is not None:
            try:
                self._runner.run(closer(), 5)
            except Exception:
                pass
        self._runner.close()

class VoiceListener:
    MODE_AUTO = "auto"
    MODE_PUSH_TO_TALK = "ptt"
    MODE_ENTER = "enter"

    def __init__(self, settings: Any = None):
        self.recorder = AudioRecorder()
        self.transcriber = VoiceTranscriber(settings=settings)
        self.enabled = self.transcriber.is_available()
        self.listening = False
        self.mode = self.MODE_ENTER
        self.ptt_available = KEYBOARD_AVAILABLE

    def is_available(self) -> bool:
        return self.enabled

    def set_mode(self, mode: str):
        if mode == self.MODE_PUSH_TO_TALK and not self.ptt_available:
            print("   Push-to-talk not available (pip install keyboard)")
            self.mode = self.MODE_ENTER
        else:
            self.mode = mode

    def listen_once(self, prompt: str = "Listening...") -> Optional[str]:
        if not self.enabled:
            print("Voice recognition not available")
            return None
        if self.mode == self.MODE_PUSH_TO_TALK and self.ptt_available:
            audio = self.recorder.record_push_to_talk()
        elif self.mode == self.MODE_ENTER:
            audio = self.recorder.record_with_enter()
        else:
            print(prompt)
            audio = self.recorder.record_until_silence(on_sound=lambda: print("   Detected..."))
        if audio is None:
            return None
        return self._transcribe(audio)

    def _transcribe(self, audio: Any) -> Optional[str]:
        print("   Transcribing...")
        text = self.transcriber.transcribe(audio)
        if text:
            print(f"   \"{text}\"")
            return text
        if not self.transcriber.last_error:
            print("   Could not transcribe")
        return None

    def listen_push_to_talk(self, key: str = "space") -> Optional[str]:
        if not self.enabled:
            return None
        audio = self.recorder.record_push_to_talk(key)
        if audio is None:
            return None
        return self._transcribe(audio)

    def listen_with_enter(self) -> Optional[str]:
        if not self.enabled:
            return None
        audio = self.recorder.record_with_enter()
        if audio is None:
            return None
        return self._transcribe(audio)

class VoiceInputMode:
    def __init__(self, settings: Any = None):
        self.listener = VoiceListener(settings)
        self.enabled = self.listener.is_available()
        self.active = False
        self.ptt_available = KEYBOARD_AVAILABLE

    def is_available(self) -> bool:
        return self.enabled

    def get_voice_command(self) -> Optional[str]:
        if not self.enabled:
            return None
        if self.ptt_available:
            return self.listener.listen_push_to_talk()
        else:
            return self.listener.listen_with_enter()

    def ask_mode_preference(self, butler=None) -> str:
        print("\n" + "="*50)
        print("INPUT CONFIGURATION")
        print("="*50)
        if butler and hasattr(butler, 'say'):
            butler.say("Good day sir. Would you like to use voice or text commands?")
        print("\nHow would you like to give commands?\n")
        print("  [1] Text (type commands)")
        print("  [2] Voice (speak commands)")
        if self.ptt_available:
            print("\n  Voice mode: Hold SPACE to talk")
        else:
            print("\n  Voice mode: Press ENTER, speak, ENTER to send")
        choice = input("\nOption (1/2) [1]: ").strip() or "1"
        if choice == "2":
            if not self.enabled:
                print("\nVoice not available. Using text.")
                if butler:
                    butler.say("I'm sorry sir, voice mode is not available. We'll use text.")
                return "text"
            self.active = True
            print("\nVOICE mode activated")
            if butler:
                butler.say("Very good sir. Voice mode activated. I'm listening.")
            return "voice"
        print("\nTEXT mode activated")
        if butler:
            butler.say("Understood sir. Text mode. Type your commands.")
        return "text"

    def show_voice_instructions(self):
        print("\nVOICE MODE:")
        if self.ptt_available:
            print("   - Hold [SPACE] to talk")
            print("   - Release to send")
        else:
            print("   - Press [ENTER] to start recording")
            print("   - Press [ENTER] again to send")
        print("   - Type 'text' to switch to text mode")

    def get_input_with_mode(self, butler=None) -> tuple[str, bool]:
        if self.active and self.ptt_available:
            return self._get_voice_or_text_input(butler)
        elif self.active:
            print("\nPress [ENTER] to talk (or type command)")
            text_input = input(">>> ").strip()
            if text_input:
                if text_input.lower() == "text":
                    self.active = False
                    print("   Switched to text mode")
                    return "", False
                return text_input, False
            voice_text = self.get_voice_command()
            if voice_text:
                return voice_text, True
            return "", False
        else:
            return input(">>> ").strip(), False

    def _get_voice_or_text_input(self, butler=None) -> tuple[str, bool]:
        print("\n[SPACE] = talk | [T] = type | [Q] = quit")
        print("   Waiting...", end="", flush=True)
        event = keyboard.read_event(suppress=True)
        while event.event_type != keyboard.KEY_DOWN:
            event = keyboard.read_event(suppress=True)
        print("\r" + " " * 50 + "\r", end="")
        key = event.name
        if key == "space":
            print("RECORDING... (release SPACE to send)")
            audio = self.listener.recorder.record_push_to_talk("space", already_pressed=True)
            if audio is None:
                print("   No audio recorded")
                return "", False
            text = self.listener._transcribe(audio)
            return (text, True) if text else ("", False)
        elif key == "t":
            print("Type your command:")
            text = input(">>> ").strip()
            return text, False
        elif key == "q":
            return "exit", False
        elif key == "escape":
            self.active = False
            print("   Switched to text mode")
            return "", False
        else:
            print(f"   Key '{key}' not recognized. Use SPACE, T or Q.")
            return "", False

_voice_input: Optional[VoiceInputMode] = None

def get_voice_input() -> VoiceInputMode:
    global _voice_input
    if _voice_input is None:
        _voice_input = VoiceInputMode()
    return _voice_input

if __name__ == "__main__":
    print("BABAS Voice Input Test")
    print("="*50)
    voice = get_voice_input()
    if not voice.is_available():
        print("\nVoice input not available.")
        print("   Make sure you have:")
        print("   1. pip install sounddevice numpy keyboard")
        print("   2. a speech backend in your .env (BABAS_STT_* variables, or OPENAI_API_KEY; see docs/providers.md)")
        print("   3. A microphone connected")
    else:
        print("\nVoice system ready!")
        mode = voice.ask_mode_preference()
        if mode == "voice":
            voice.show_voice_instructions()
            voice.active = True
        print("\nType 'exit' to quit.\n")
        while True:
            text, was_voice = voice.get_input_with_mode()
            if text.lower() in ["exit", "quit", "q"]:
                print("\nGoodbye!")
                break
            if text:
                source = "Voice" if was_voice else "Text"
                print(f"\n{source}: \"{text}\"")
            else:
                print("\n   (no input)")
