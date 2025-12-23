import os
import io
import random
import threading
import tempfile
from typing import Optional
from queue import Queue, Empty
from dotenv import load_dotenv

load_dotenv()

try:
    from elevenlabs import ElevenLabs
    ELEVENLABS_AVAILABLE = True
except ImportError:
    ELEVENLABS_AVAILABLE = False
    print("elevenlabs not installed. Run: pip install elevenlabs")

try:
    import pygame
    pygame.mixer.init()
    PYGAME_AVAILABLE = True
except ImportError:
    PYGAME_AVAILABLE = False
    print("pygame not installed. Run: pip install pygame")

class ButlerPhrases:
    GREETINGS = [
        "At your service sir. How may I assist you today?",
        "Good day sir. I am ready for your instructions.",
        "Welcome sir. The system is operational.",
        "At your command sir.",
    ]
    PLANNING = [
        "Understood sir. Allow me to prepare a plan.",
        "Very well sir. Analyzing the best strategy.",
        "Understood. Preparing the necessary steps.",
    ]
    EXECUTING = [
        "Proceeding with the task sir.",
        "On it sir.",
        "Initiating operation.",
    ]
    STEP_COMPLETE = [
        "Making adequate progress sir.",
        "Advancing as planned.",
    ]
    SUCCESS = [
        "Task completed sir. Is there anything else I can help with?",
        "Mission accomplished sir.",
        "Done sir. Everything in order.",
        "Operation successful sir.",
    ]
    ERROR = [
        "One moment sir. I encountered an obstacle.",
        "My apologies sir. Allow me to find an alternative.",
    ]
    RECOVERY = [
        "I have an alternative plan sir.",
        "Not to worry, I found another way.",
    ]
    GOODBYE = [
        "It has been a pleasure serving you sir. Until next time.",
        "Have an excellent day sir.",
        "I will be here when you need me.",
    ]
    MUTED = [
        "Understood. I will remain silent.",
    ]
    UNMUTED = [
        "At your service sir.",
    ]

    @classmethod
    def get(cls, category: str) -> str:
        phrases = getattr(cls, category.upper(), [""])
        return random.choice(phrases)

class VoiceEngine:
    RECOMMENDED_VOICES = {
        "jarvis": "onwK4e9ZLuTAKqWW03F9",
        "alfred": "pNInz6obpgDQGcFmaJgB",
        "butler": "ErXwobaYiN019PkySvjV",
        "spanish": "ThT5KcBeYPX3keUQqHPh",
    }

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("ELEVENLABS_API_KEY")
        self.client = None
        self.voice_id = self.RECOMMENDED_VOICES.get("jarvis")
        self.muted = False
        self.speaking = False
        self.speech_queue: Queue = Queue()
        self.stop_event = threading.Event()
        self.speech_thread: Optional[threading.Thread] = None
        if self.api_key and ELEVENLABS_AVAILABLE:
            try:
                self.client = ElevenLabs(api_key=self.api_key)
                print("Voice engine initialized")
            except Exception as e:
                print(f"Error initializing voice: {e}")
                self.client = None
        else:
            if not self.api_key:
                print("To enable voice, add ELEVENLABS_API_KEY to your .env")

    def is_available(self) -> bool:
        return self.client is not None and PYGAME_AVAILABLE

    def set_voice(self, voice_name: str):
        if voice_name in self.RECOMMENDED_VOICES:
            self.voice_id = self.RECOMMENDED_VOICES[voice_name]

    def mute(self):
        self.muted = True
        print("Voice muted")

    def unmute(self):
        self.muted = False
        print("Voice enabled")

    def toggle_mute(self) -> bool:
        self.muted = not self.muted
        return self.muted

    def stop_speaking(self):
        if PYGAME_AVAILABLE:
            try:
                pygame.mixer.music.stop()
            except:
                pass
        self.speaking = False

    def _synthesize_and_play(self, text: str):
        if self.muted or not self.is_available():
            return
        try:
            self.speaking = True
            audio_generator = self.client.text_to_speech.convert(
                voice_id=self.voice_id,
                text=text,
                model_id="eleven_multilingual_v2",
                output_format="mp3_44100_128"
            )
            audio_bytes = b"".join(audio_generator)
            audio_stream = io.BytesIO(audio_bytes)
            pygame.mixer.music.load(audio_stream, "mp3")
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy() and not self.stop_event.is_set():
                pygame.time.wait(100)
        except Exception as e:
            print(f"Voice error: {e}")
        finally:
            self.speaking = False

    def _speech_worker(self):
        while not self.stop_event.is_set():
            try:
                text = self.speech_queue.get(timeout=0.5)
                if text:
                    self._synthesize_and_play(text)
                self.speech_queue.task_done()
            except Empty:
                continue
            except Exception as e:
                print(f"Voice worker error: {e}")

    def start(self):
        if not self.is_available():
            return
        self.stop_event.clear()
        self.speech_thread = threading.Thread(target=self._speech_worker, daemon=True)
        self.speech_thread.start()

    def stop(self):
        self.stop_event.set()
        self.stop_speaking()
        if self.speech_thread:
            self.speech_thread.join(timeout=2)

    def speak(self, text: str, priority: bool = False):
        if self.muted or not self.is_available():
            return
        if priority:
            while not self.speech_queue.empty():
                try:
                    self.speech_queue.get_nowait()
                except:
                    break
            self.stop_speaking()
        self.speech_queue.put(text)

    def speak_sync(self, text: str):
        if self.muted or not self.is_available():
            return
        self._synthesize_and_play(text)

class Butler:
    def __init__(self):
        self.voice = VoiceEngine()
        self.enabled = self.voice.is_available()

    def start(self):
        if self.enabled:
            self.voice.start()

    def stop(self):
        if self.enabled:
            self.voice.stop()

    def greet(self):
        self.voice.speak(ButlerPhrases.get("greetings"))

    def planning(self):
        self.voice.speak(ButlerPhrases.get("planning"))

    def executing(self):
        self.voice.speak(ButlerPhrases.get("executing"))

    def step_complete(self):
        if random.random() < 0.3:
            self.voice.speak(ButlerPhrases.get("step_complete"))

    def success(self):
        self.voice.speak(ButlerPhrases.get("success"))

    def error(self):
        self.voice.speak(ButlerPhrases.get("error"))

    def recovery(self):
        self.voice.speak(ButlerPhrases.get("recovery"))

    def goodbye(self):
        self.voice.speak_sync(ButlerPhrases.get("goodbye"))

    def mute(self):
        self.voice.speak_sync(ButlerPhrases.get("muted"))
        self.voice.mute()

    def unmute(self):
        self.voice.unmute()
        self.voice.speak(ButlerPhrases.get("unmuted"))

    def toggle_mute(self) -> bool:
        is_muted = self.voice.toggle_mute()
        if is_muted:
            print("Butler muted")
        else:
            print("Butler enabled")
            self.voice.speak(ButlerPhrases.get("unmuted"))
        return is_muted

    def say(self, text: str):
        self.voice.speak(text)

    def shut_up(self):
        self.voice.stop_speaking()

_butler_instance: Optional[Butler] = None

def get_butler() -> Butler:
    global _butler_instance
    if _butler_instance is None:
        _butler_instance = Butler()
    return _butler_instance

def init_butler() -> Butler:
    butler = get_butler()
    butler.start()
    return butler

def shutdown_butler():
    global _butler_instance
    if _butler_instance:
        _butler_instance.stop()
        _butler_instance = None

if __name__ == "__main__":
    print("BABAS Butler Test")
    print("="*50)
    butler = init_butler()
    if not butler.enabled:
        print("\nButler not available.")
        print("   Make sure you have:")
        print("   1. ELEVENLABS_API_KEY in your .env")
        print("   2. pip install elevenlabs pygame")
    else:
        print("\nButler ready!")
        print("\nTest commands:")
        print("  1 - Greeting")
        print("  2 - Executing")
        print("  3 - Success")
        print("  4 - Error")
        print("  m - Mute/Unmute")
        print("  c - Stop now")
        print("  q - Quit")
        while True:
            cmd = input("\n>>> ").strip().lower()
            if cmd == "q":
                butler.goodbye()
                break
            elif cmd == "1":
                butler.greet()
            elif cmd == "2":
                butler.executing()
            elif cmd == "3":
                butler.success()
            elif cmd == "4":
                butler.error()
            elif cmd == "m":
                butler.toggle_mute()
            elif cmd == "c":
                butler.shut_up()
            else:
                butler.say(f"You said: {cmd}")
    shutdown_butler()
    print("\nTest complete")
