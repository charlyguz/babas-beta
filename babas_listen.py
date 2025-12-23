"""
🎤 BABAS Listen Module - Entrada por voz
Usa OpenAI Whisper/gpt-4o-transcribe para reconocimiento de voz
"""

import os
import io
import wave
import tempfile
import threading
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
    print("⚠️ sounddevice no instalado. Ejecuta: pip install sounddevice")

try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False


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
    
    def __init__(self):
        self.recorder = AudioRecorder()
        self.transcriber = VoiceTranscriber()
        self.enabled = self.transcriber.is_available()
        self.listening = False
        self._callback: Optional[Callable[[str], None]] = None
    
    def is_available(self) -> bool:
        return self.enabled
    
    def listen_once(self, prompt: str = "🎤 Escuchando...") -> Optional[str]:
        """
        Escucha un comando de voz.
        Retorna el texto transcrito o None.
        """
        if not self.enabled:
            print("❌ Reconocimiento de voz no disponible")
            return None
        
        print(prompt)
        
        # Grabar audio
        audio = self.recorder.record_until_silence(
            on_sound=lambda: print("   🔊 Detectado...")
        )
        
        if audio is None:
            print("   ❌ No se detectó audio")
            return None
        
        duration = len(audio) / self.recorder.sample_rate
        print(f"   ✅ Grabado ({duration:.1f}s)")
        
        # Transcribir
        print("   🔄 Transcribiendo...")
        text = self.transcriber.transcribe(audio)
        
        if text:
            print(f"   📝 \"{text}\"")
            return text
        else:
            print("   ❌ No se pudo transcribir")
            return None
    
    def listen_with_hotkey(self, 
                          on_result: Callable[[str], None],
                          hotkey: str = "space") -> None:
        """
        Modo de escucha continua con tecla de activación.
        Presiona la tecla para empezar a hablar.
        """
        # Esto requeriría keyboard o pynput
        # Por ahora usamos un enfoque más simple
        pass


# =============================================================================
# MODO VOZ PARA BABAS
# =============================================================================

class VoiceInputMode:
    """Modo de entrada por voz para BABAS"""
    
    def __init__(self):
        self.listener = VoiceListener()
        self.enabled = self.listener.is_available()
        self.active = False
    
    def is_available(self) -> bool:
        return self.enabled
    
    def get_voice_input(self) -> Optional[str]:
        """
        Obtiene input por voz.
        Retorna el comando o None si no se detectó.
        """
        if not self.enabled:
            return None
        
        print("\n🎤 MODO VOZ - Presiona ENTER y habla (o escribe 'texto' para volver)")
        input("   [Presiona ENTER para hablar]")
        
        return self.listener.listen_once("   🎤 Habla ahora...")
    
    def interactive_prompt(self) -> tuple[str, bool]:
        """
        Prompt interactivo que permite voz o texto.
        Retorna: (input_text, was_voice)
        """
        print("\n💬 ¿Qué quieres hacer?")
        print("   [Escribe comando o presiona ENTER para hablar]")
        
        user_input = input(">>> ").strip()
        
        # Si escribió algo, usar texto
        if user_input:
            return user_input, False
        
        # Si solo presionó ENTER, usar voz
        if self.enabled:
            voice_text = self.listener.listen_once("🎤 Habla ahora...")
            if voice_text:
                return voice_text, True
            else:
                print("   ⚠️ No se detectó voz. Intenta de nuevo.")
                return "", False
        else:
            print("   ⚠️ Voz no disponible. Escribe tu comando.")
            return "", False


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
        print("   1. pip install sounddevice numpy")
        print("   2. OPENAI_API_KEY en tu .env")
        print("   3. Un micrófono conectado")
    else:
        print("\n✅ Sistema de voz listo!")
        print("\nPresiona ENTER para hablar, o escribe 'salir' para terminar.\n")
        
        while True:
            text, was_voice = voice.interactive_prompt()
            
            if text.lower() in ["salir", "exit", "q"]:
                print("\n👋 ¡Hasta luego!")
                break
            
            if text:
                source = "🎤 Voz" if was_voice else "⌨️ Texto"
                print(f"\n{source}: {text}")
            else:
                print("\n   (sin entrada)")

