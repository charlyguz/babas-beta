"""
🎤 BABAS Listen Module - Entrada por voz
Usa OpenAI Whisper para reconocimiento de voz
Push-to-Talk: Mantén ESPACIO para grabar
"""

import os
import io
import wave
import tempfile
import threading
import sys
from typing import Optional, Callable
from dotenv import load_dotenv

load_dotenv()

# Intentar importar dependencias de audio
try:
    import sounddevice as sd
    import numpy as np
    SOUNDDEVICE_AVAILABLE = True
except ImportError:
    SOUNDDEVICE_AVAILABLE = False
    print("⚠️ sounddevice no instalado. Ejecuta: pip install sounddevice numpy")

try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

# Para detectar teclas (push-to-talk)
try:
    import keyboard
    KEYBOARD_AVAILABLE = True
except ImportError:
    KEYBOARD_AVAILABLE = False
    # print("💡 Para push-to-talk: pip install keyboard")


# =============================================================================
# CONFIGURACIÓN
# =============================================================================

SAMPLE_RATE = 16000  # Hz - óptimo para transcripción
CHANNELS = 1  # Mono
SILENCE_THRESHOLD = 0.01  # Umbral de silencio
SILENCE_DURATION = 1.5  # Segundos de silencio para parar
MAX_DURATION = 30  # Máximo segundos de grabación
MIN_DURATION = 0.5  # Mínimo segundos para procesar


# =============================================================================
# GRABADOR DE AUDIO
# =============================================================================

class AudioRecorder:
    """Graba audio del micrófono"""
    
    def __init__(self):
        self.recording = False
        self.audio_data = []
        self.sample_rate = SAMPLE_RATE
        self._stop_event = threading.Event()
    
    def _is_silence(self, audio_chunk: np.ndarray) -> bool:
        """Detecta si el chunk es silencio"""
        rms = np.sqrt(np.mean(audio_chunk**2))
        return rms < SILENCE_THRESHOLD
    
    def record_until_silence(self, 
                            max_duration: float = MAX_DURATION,
                            silence_duration: float = SILENCE_DURATION,
                            on_start: Optional[Callable] = None,
                            on_sound: Optional[Callable] = None) -> Optional[np.ndarray]:
        """
        Graba hasta detectar silencio prolongado.
        Retorna el audio como numpy array.
        """
        if not SOUNDDEVICE_AVAILABLE:
            print("❌ sounddevice no disponible")
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
                
                # Si ya empezó y hay suficiente silencio, parar
                if has_started and silence_chunks >= silence_chunks_needed:
                    raise sd.CallbackAbort()
                
                # Si empezó, guardar todo (incluyendo silencios cortos)
                if has_started:
                    self.audio_data.append(chunk)
            
            with sd.InputStream(samplerate=self.sample_rate,
                              channels=CHANNELS,
                              dtype='float32',
                              blocksize=1024,
                              callback=callback):
                # Esperar máximo max_duration segundos
                self._stop_event.wait(timeout=max_duration)
        
        except sd.CallbackAbort:
            pass
        except Exception as e:
            print(f"❌ Error grabando: {e}")
            return None
        finally:
            self.recording = False
        
        if not self.audio_data:
            return None
        
        # Concatenar chunks
        audio = np.concatenate(self.audio_data)
        
        # Verificar duración mínima
        duration = len(audio) / self.sample_rate
        if duration < MIN_DURATION:
            return None
        
        return audio
    
    def record_for_duration(self, duration: float) -> Optional[np.ndarray]:
        """Graba por un tiempo específico"""
        if not SOUNDDEVICE_AVAILABLE:
            return None
        
        try:
            print(f"   🎤 Grabando {duration}s...")
            audio = sd.rec(int(duration * self.sample_rate),
                          samplerate=self.sample_rate,
                          channels=CHANNELS,
                          dtype='float32')
            sd.wait()
            return audio.flatten()
        except Exception as e:
            print(f"❌ Error grabando: {e}")
            return None
    
    def record_push_to_talk(self, key: str = "space") -> Optional[np.ndarray]:
        """
        Push-to-Talk: Mantén presionada la tecla para grabar.
        Suelta para terminar.
        """
        if not SOUNDDEVICE_AVAILABLE:
            print("❌ sounddevice no disponible")
            return None
        
        if not KEYBOARD_AVAILABLE:
            print("❌ keyboard no disponible. Ejecuta: pip install keyboard")
            return None
        
        self.audio_data = []
        self.recording = False
        self._stop_event.clear()
        
        print(f"   🎤 Mantén presionado [{key.upper()}] para hablar...")
        
        # Esperar a que presione la tecla
        keyboard.wait(key)
        
        print(f"   🔴 GRABANDO... (suelta [{key.upper()}] para terminar)")
        self.recording = True
        
        try:
            def callback(indata, frames, time, status):
                if self._stop_event.is_set() or not self.recording:
                    raise sd.CallbackAbort()
                self.audio_data.append(indata.copy().flatten())
            
            with sd.InputStream(samplerate=self.sample_rate,
                              channels=CHANNELS,
                              dtype='float32',
                              blocksize=1024,
                              callback=callback):
                # Esperar a que suelte la tecla
                keyboard.wait(key, suppress=True, trigger_on_release=True)
                self.recording = False
        
        except sd.CallbackAbort:
            pass
        except Exception as e:
            print(f"❌ Error: {e}")
            return None
        finally:
            self.recording = False
        
        if not self.audio_data:
            return None
        
        audio = np.concatenate(self.audio_data)
        duration = len(audio) / self.sample_rate
        
        if duration < MIN_DURATION:
            print(f"   ⚠️ Muy corto ({duration:.1f}s)")
            return None
        
        print(f"   ✅ Grabado ({duration:.1f}s)")
        return audio
    
    def record_with_enter(self) -> Optional[np.ndarray]:
        """
        Graba mientras ENTER no se presione de nuevo.
        Más simple que push-to-talk.
        """
        if not SOUNDDEVICE_AVAILABLE:
            return None
        
        self.audio_data = []
        self.recording = True
        self._stop_event.clear()
        
        print("   🔴 GRABANDO... (presiona ENTER para terminar)")
        
        # Thread para esperar ENTER
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
            
            with sd.InputStream(samplerate=self.sample_rate,
                              channels=CHANNELS,
                              dtype='float32',
                              blocksize=1024,
                              callback=callback):
                self._stop_event.wait(timeout=MAX_DURATION)
        
        except sd.CallbackAbort:
            pass
        except Exception as e:
            print(f"❌ Error: {e}")
            return None
        finally:
            self.recording = False
        
        if not self.audio_data:
            return None
        
        audio = np.concatenate(self.audio_data)
        duration = len(audio) / self.sample_rate
        
        if duration < MIN_DURATION:
            print(f"   ⚠️ Muy corto ({duration:.1f}s)")
            return None
        
        print(f"   ✅ Grabado ({duration:.1f}s)")
        return audio
    
    def stop(self):
        """Detiene la grabación"""
        self._stop_event.set()
        self.recording = False
    
    def audio_to_wav_bytes(self, audio: np.ndarray) -> bytes:
        """Convierte numpy array a bytes WAV"""
        # Normalizar a int16
        audio_int16 = (audio * 32767).astype(np.int16)
        
        # Crear WAV en memoria
        buffer = io.BytesIO()
        with wave.open(buffer, 'wb') as wav:
            wav.setnchannels(CHANNELS)
            wav.setsampwidth(2)  # 16-bit
            wav.setframerate(self.sample_rate)
            wav.writeframes(audio_int16.tobytes())
        
        buffer.seek(0)
        return buffer.read()


# =============================================================================
# TRANSCRIPTOR
# =============================================================================

class VoiceTranscriber:
    """Transcribe audio usando OpenAI"""
    
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.client = None
        self.model = "whisper-1"  # o "gpt-4o-transcribe" cuando esté disponible
        
        if self.api_key and OPENAI_AVAILABLE:
            self.client = OpenAI(api_key=self.api_key)
    
    def is_available(self) -> bool:
        return self.client is not None and SOUNDDEVICE_AVAILABLE
    
    def transcribe(self, audio: np.ndarray, language: str = "es") -> Optional[str]:
        """Transcribe audio a texto"""
        if not self.client:
            return None
        
        try:
            # Convertir a WAV
            recorder = AudioRecorder()
            wav_bytes = recorder.audio_to_wav_bytes(audio)
            
            # Crear archivo temporal
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(wav_bytes)
                temp_path = f.name
            
            # Transcribir con OpenAI
            with open(temp_path, "rb") as audio_file:
                response = self.client.audio.transcriptions.create(
                    model=self.model,
                    file=audio_file,
                    language=language,
                    response_format="text"
                )
            
            # Limpiar archivo temporal
            os.unlink(temp_path)
            
            return response.strip() if response else None
        
        except Exception as e:
            print(f"❌ Error transcribiendo: {e}")
            return None


# =============================================================================
# LISTENER PRINCIPAL
# =============================================================================

class VoiceListener:
    """Escucha comandos por voz"""
    
    # Modos de grabación
    MODE_AUTO = "auto"          # Detección automática de silencio
    MODE_PUSH_TO_TALK = "ptt"   # Mantener tecla presionada
    MODE_ENTER = "enter"        # ENTER para empezar/parar
    
    def __init__(self):
        self.recorder = AudioRecorder()
        self.transcriber = VoiceTranscriber()
        self.enabled = self.transcriber.is_available()
        self.listening = False
        self.mode = self.MODE_ENTER  # Default más confiable
        
        # Verificar si push-to-talk está disponible
        self.ptt_available = KEYBOARD_AVAILABLE
    
    def is_available(self) -> bool:
        return self.enabled
    
    def set_mode(self, mode: str):
        """Cambia el modo de grabación"""
        if mode == self.MODE_PUSH_TO_TALK and not self.ptt_available:
            print("   ⚠️ Push-to-talk no disponible (pip install keyboard)")
            self.mode = self.MODE_ENTER
        else:
            self.mode = mode
    
    def listen_once(self, prompt: str = "🎤 Escuchando...") -> Optional[str]:
        """
        Escucha un comando de voz según el modo configurado.
        """
        if not self.enabled:
            print("❌ Reconocimiento de voz no disponible")
            return None
        
        # Grabar según el modo
        if self.mode == self.MODE_PUSH_TO_TALK and self.ptt_available:
            audio = self.recorder.record_push_to_talk()
        elif self.mode == self.MODE_ENTER:
            audio = self.recorder.record_with_enter()
        else:
            print(prompt)
            audio = self.recorder.record_until_silence(
                on_sound=lambda: print("   🔊 Detectado...")
            )
        
        if audio is None:
            return None
        
        # Transcribir
        print("   🔄 Transcribiendo...")
        text = self.transcriber.transcribe(audio)
        
        if text:
            print(f"   📝 \"{text}\"")
            return text
        else:
            print("   ❌ No se pudo transcribir")
            return None
    
    def listen_push_to_talk(self, key: str = "space") -> Optional[str]:
        """Escucha con push-to-talk"""
        if not self.enabled:
            return None
        
        audio = self.recorder.record_push_to_talk(key)
        if audio is None:
            return None
        
        print("   🔄 Transcribiendo...")
        text = self.transcriber.transcribe(audio)
        
        if text:
            print(f"   📝 \"{text}\"")
        return text
    
    def listen_with_enter(self) -> Optional[str]:
        """Escucha hasta que presiones ENTER"""
        if not self.enabled:
            return None
        
        audio = self.recorder.record_with_enter()
        if audio is None:
            return None
        
        print("   🔄 Transcribiendo...")
        text = self.transcriber.transcribe(audio)
        
        if text:
            print(f"   📝 \"{text}\"")
        return text


# =============================================================================
# MODO VOZ PARA BABAS
# =============================================================================

class VoiceInputMode:
    """Modo de entrada por voz para BABAS"""
    
    def __init__(self):
        self.listener = VoiceListener()
        self.enabled = self.listener.is_available()
        self.active = False
        self.ptt_available = KEYBOARD_AVAILABLE
    
    def is_available(self) -> bool:
        return self.enabled
    
    def get_voice_command(self) -> Optional[str]:
        """
        Obtiene un comando por voz.
        Usa el mejor método disponible.
        """
        if not self.enabled:
            return None
        
        if self.ptt_available:
            return self.listener.listen_push_to_talk()
        else:
            return self.listener.listen_with_enter()
    
    def ask_mode_preference(self, butler=None) -> str:
        """
        Pregunta al usuario qué modo prefiere (voz o texto).
        Opcionalmente usa el mayordomo para hablar.
        Retorna: "voice" o "text"
        """
        print("\n" + "="*50)
        print("🎤 CONFIGURACIÓN DE ENTRADA")
        print("="*50)
        
        # Si el mayordomo está disponible, que pregunte con voz
        if butler and hasattr(butler, 'say'):
            butler.say("Buenos días, señor. ¿Desea usar comandos por voz o por texto?")
        
        print("\n¿Cómo quieres dar comandos?\n")
        print("  [1] ⌨️  Texto (escribir comandos)")
        print("  [2] 🎤 Voz (hablar comandos)")
        
        if self.ptt_available:
            print("\n  💡 Modo voz: Mantén ESPACIO para hablar")
        else:
            print("\n  💡 Modo voz: Presiona ENTER, habla, ENTER para enviar")
        
        choice = input("\nOpción (1/2) [1]: ").strip() or "1"
        
        if choice == "2":
            if not self.enabled:
                print("\n⚠️ Voz no disponible. Usando texto.")
                if butler:
                    butler.say("Disculpe señor, el modo de voz no está disponible. Usaremos texto.")
                return "text"
            
            self.active = True
            print("\n✅ Modo VOZ activado")
            if butler:
                butler.say("Perfecto, señor. Modo de voz activado. Estoy escuchando.")
            return "voice"
        
        print("\n✅ Modo TEXTO activado")
        if butler:
            butler.say("Entendido, señor. Modo texto. Escriba sus comandos.")
        return "text"
    
    def show_voice_instructions(self):
        """Muestra instrucciones del modo voz"""
        print("\n📖 MODO VOZ:")
        if self.ptt_available:
            print("   • Mantén [ESPACIO] para hablar")
            print("   • Suelta para enviar")
        else:
            print("   • Presiona [ENTER] para empezar a grabar")
            print("   • Presiona [ENTER] de nuevo para enviar")
        print("   • Escribe 'texto' para cambiar a modo texto")
    
    def get_input_with_mode(self) -> tuple[str, bool]:
        """
        Obtiene input según el modo activo.
        Retorna: (texto, fue_voz)
        """
        if self.active:
            # Modo voz
            if self.ptt_available:
                print("\n🎤 Mantén [ESPACIO] para hablar (o escribe comando)")
            else:
                print("\n🎤 Presiona [ENTER] para hablar (o escribe comando)")
            
            text_input = input(">>> ").strip()
            
            # Si escribió algo, usar eso
            if text_input:
                if text_input.lower() == "texto":
                    self.active = False
                    print("   ⌨️ Cambiado a modo texto")
                    return "", False
                return text_input, False
            
            # Si solo ENTER, grabar voz
            voice_text = self.get_voice_command()
            if voice_text:
                return voice_text, True
            return "", False
        
        else:
            # Modo texto normal
            return input(">>> ").strip(), False


# =============================================================================
# SINGLETON
# =============================================================================

_voice_input: Optional[VoiceInputMode] = None

def get_voice_input() -> VoiceInputMode:
    """Obtiene la instancia de entrada por voz"""
    global _voice_input
    if _voice_input is None:
        _voice_input = VoiceInputMode()
    return _voice_input


# =============================================================================
# TEST
# =============================================================================

if __name__ == "__main__":
    print("🎤 Test de entrada por voz BABAS")
    print("="*50)
    
    voice = get_voice_input()
    
    if not voice.is_available():
        print("\n❌ Entrada por voz no disponible.")
        print("   Asegúrate de tener:")
        print("   1. pip install sounddevice numpy keyboard")
        print("   2. OPENAI_API_KEY en tu .env")
        print("   3. Un micrófono conectado")
    else:
        print("\n✅ Sistema de voz listo!")
        
        # Preguntar modo
        mode = voice.ask_mode_preference()
        
        if mode == "voice":
            voice.show_voice_instructions()
            voice.active = True
        
        print("\nEscribe 'salir' para terminar.\n")
        
        while True:
            text, was_voice = voice.get_input_with_mode()
            
            if text.lower() in ["salir", "exit", "q"]:
                print("\n👋 ¡Hasta luego!")
                break
            
            if text:
                source = "🎤 Voz" if was_voice else "⌨️ Texto"
                print(f"\n{source}: \"{text}\"")
            else:
                print("\n   (sin entrada)")

