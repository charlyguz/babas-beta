"""
🎙️ BABAS Voice Module - Mayordomo estilo Jarvis
Integración con ElevenLabs para síntesis de voz
"""

import os
import io
import random
import threading
import tempfile
from typing import Optional
from queue import Queue, Empty
from dotenv import load_dotenv

load_dotenv()

# Intentar importar dependencias de audio
try:
    from elevenlabs import ElevenLabs
    ELEVENLABS_AVAILABLE = True
except ImportError:
    ELEVENLABS_AVAILABLE = False
    print("⚠️ elevenlabs no instalado. Ejecuta: pip install elevenlabs")

try:
    import pygame
    pygame.mixer.init()
    PYGAME_AVAILABLE = True
except ImportError:
    PYGAME_AVAILABLE = False
    print("⚠️ pygame no instalado. Ejecuta: pip install pygame")


# =============================================================================
# FRASES DEL MAYORDOMO
# =============================================================================

class ButlerPhrases:
    """Frases estilo Jarvis para diferentes situaciones"""
    
    GREETINGS = [
        "A sus órdenes, señor.",
        "Buenos días, señor. ¿En qué puedo servirle?",
        "Estoy a su disposición, señor.",
        "Sistema listo, señor. ¿Cuál es su deseo?",
        "BABAS a su servicio, señor.",
    ]
    
    PLANNING = [
        "Analizando su solicitud, señor.",
        "Entendido, señor. Preparando el plan.",
        "Procesando, señor. Un momento.",
        "Permítame elaborar una estrategia, señor.",
    ]
    
    EXECUTING = [
        "Ejecutando, señor.",
        "En proceso, señor.",
        "Trabajando en ello, señor.",
        "Iniciando la tarea, señor.",
    ]
    
    STEP_COMPLETE = [
        "Paso completado, señor.",
        "Hecho, señor.",
        "Siguiente paso, señor.",
        "Avanzando, señor.",
    ]
    
    SUCCESS = [
        "Tarea completada exitosamente, señor.",
        "Misión cumplida, señor.",
        "Listo, señor. ¿Algo más en lo que pueda ayudarle?",
        "Operación finalizada con éxito, señor.",
    ]
    
    ERROR = [
        "Ha ocurrido un inconveniente, señor. Permítame intentar de nuevo.",
        "Encontré un obstáculo, señor. Buscando alternativas.",
        "Mis disculpas, señor. Algo salió mal. Reintentando.",
        "Error detectado, señor. Analizando soluciones.",
    ]
    
    RECOVERY = [
        "No se preocupe, señor. Tengo un plan alternativo.",
        "Encontré otra forma de hacerlo, señor.",
        "Permítame intentar un enfoque diferente, señor.",
    ]
    
    GOODBYE = [
        "Ha sido un placer servirle, señor.",
        "Hasta pronto, señor.",
        "Estaré aquí cuando me necesite, señor.",
        "Que tenga un excelente día, señor.",
    ]
    
    MUTED = [
        "Entendido, señor. Guardaré silencio.",
    ]
    
    UNMUTED = [
        "Voz reactivada, señor. A sus órdenes.",
    ]
    
    @classmethod
    def get(cls, category: str) -> str:
        """Obtiene una frase aleatoria de una categoría"""
        phrases = getattr(cls, category.upper(), [""])
        return random.choice(phrases)


# =============================================================================
# MOTOR DE VOZ
# =============================================================================

class VoiceEngine:
    """Motor de síntesis de voz con ElevenLabs"""
    
    # Voces recomendadas de ElevenLabs para mayordomo
    RECOMMENDED_VOICES = {
        "jarvis": "onwK4e9ZLuTAKqWW03F9",      # Daniel - voz británica
        "alfred": "pNInz6obpgDQGcFmaJgB",      # Adam - voz profunda
        "butler": "ErXwobaYiN019PkySvjV",      # Antoni - elegante
        "spanish": "ThT5KcBeYPX3keUQqHPh",     # Laura - español
    }
    
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("ELEVENLABS_API_KEY")
        self.client = None
        self.voice_id = self.RECOMMENDED_VOICES.get("jarvis")
        self.muted = False
        self.speaking = False
        
        # Cola de mensajes para hablar
        self.speech_queue: Queue = Queue()
        self.stop_event = threading.Event()
        self.speech_thread: Optional[threading.Thread] = None
        
        # Inicializar cliente
        if self.api_key and ELEVENLABS_AVAILABLE:
            try:
                self.client = ElevenLabs(api_key=self.api_key)
                print("🎙️ Motor de voz inicializado")
            except Exception as e:
                print(f"⚠️ Error inicializando voz: {e}")
                self.client = None
        else:
            if not self.api_key:
                print("💡 Para habilitar voz, agrega ELEVENLABS_API_KEY a tu .env")
    
    def is_available(self) -> bool:
        """Verifica si el motor de voz está disponible"""
        return self.client is not None and PYGAME_AVAILABLE
    
    def set_voice(self, voice_name: str):
        """Cambia la voz del mayordomo"""
        if voice_name in self.RECOMMENDED_VOICES:
            self.voice_id = self.RECOMMENDED_VOICES[voice_name]
    
    def mute(self):
        """Silencia al mayordomo"""
        self.muted = True
        print("🔇 Voz silenciada")
    
    def unmute(self):
        """Activa la voz del mayordomo"""
        self.muted = False
        print("🔊 Voz activada")
    
    def toggle_mute(self) -> bool:
        """Alterna el estado de silencio"""
        self.muted = not self.muted
        return self.muted
    
    def stop_speaking(self):
        """Detiene la reproducción actual"""
        if PYGAME_AVAILABLE:
            try:
                pygame.mixer.music.stop()
            except:
                pass
        self.speaking = False
    
    def _synthesize_and_play(self, text: str):
        """Sintetiza y reproduce audio (interno)"""
        if self.muted or not self.is_available():
            return
        
        try:
            self.speaking = True
            
            # Generar audio con ElevenLabs
            audio_generator = self.client.text_to_speech.convert(
                voice_id=self.voice_id,
                text=text,
                model_id="eleven_multilingual_v2",  # Soporta español
                output_format="mp3_44100_128"
            )
            
            # Convertir generador a bytes
            audio_bytes = b"".join(audio_generator)
            
            # Reproducir con pygame
            audio_stream = io.BytesIO(audio_bytes)
            pygame.mixer.music.load(audio_stream, "mp3")
            pygame.mixer.music.play()
            
            # Esperar a que termine
            while pygame.mixer.music.get_busy() and not self.stop_event.is_set():
                pygame.time.wait(100)
            
        except Exception as e:
            print(f"⚠️ Error de voz: {e}")
        finally:
            self.speaking = False
    
    def _speech_worker(self):
        """Worker thread para procesar cola de mensajes"""
        while not self.stop_event.is_set():
            try:
                text = self.speech_queue.get(timeout=0.5)
                if text:
                    self._synthesize_and_play(text)
                self.speech_queue.task_done()
            except Empty:
                continue
            except Exception as e:
                print(f"⚠️ Error en worker de voz: {e}")
    
    def start(self):
        """Inicia el thread de voz"""
        if not self.is_available():
            return
        
        self.stop_event.clear()
        self.speech_thread = threading.Thread(target=self._speech_worker, daemon=True)
        self.speech_thread.start()
    
    def stop(self):
        """Detiene el thread de voz"""
        self.stop_event.set()
        self.stop_speaking()
        if self.speech_thread:
            self.speech_thread.join(timeout=2)
    
    def speak(self, text: str, priority: bool = False):
        """Agrega texto a la cola para hablar"""
        if self.muted or not self.is_available():
            return
        
        if priority:
            # Limpiar cola y hablar inmediatamente
            while not self.speech_queue.empty():
                try:
                    self.speech_queue.get_nowait()
                except:
                    break
            self.stop_speaking()
        
        self.speech_queue.put(text)
    
    def speak_sync(self, text: str):
        """Habla de forma síncrona (bloquea hasta terminar)"""
        if self.muted or not self.is_available():
            return
        self._synthesize_and_play(text)


# =============================================================================
# MAYORDOMO BABAS
# =============================================================================

class Butler:
    """El mayordomo virtual de BABAS"""
    
    def __init__(self):
        self.voice = VoiceEngine()
        self.enabled = self.voice.is_available()
    
    def start(self):
        """Inicia el mayordomo"""
        if self.enabled:
            self.voice.start()
    
    def stop(self):
        """Detiene el mayordomo"""
        if self.enabled:
            self.voice.stop()
    
    def greet(self):
        """Saludo inicial"""
        self.voice.speak(ButlerPhrases.get("greetings"))
    
    def planning(self):
        """Cuando está analizando"""
        self.voice.speak(ButlerPhrases.get("planning"))
    
    def executing(self):
        """Cuando empieza a ejecutar"""
        self.voice.speak(ButlerPhrases.get("executing"))
    
    def step_complete(self):
        """Cuando completa un paso"""
        # Solo habla cada 3 pasos para no ser molesto
        if random.random() < 0.3:
            self.voice.speak(ButlerPhrases.get("step_complete"))
    
    def success(self):
        """Cuando termina exitosamente"""
        self.voice.speak(ButlerPhrases.get("success"))
    
    def error(self):
        """Cuando hay un error"""
        self.voice.speak(ButlerPhrases.get("error"))
    
    def recovery(self):
        """Cuando está recuperándose"""
        self.voice.speak(ButlerPhrases.get("recovery"))
    
    def goodbye(self):
        """Despedida"""
        self.voice.speak_sync(ButlerPhrases.get("goodbye"))
    
    def mute(self):
        """Silencia al mayordomo"""
        self.voice.speak_sync(ButlerPhrases.get("muted"))
        self.voice.mute()
    
    def unmute(self):
        """Activa la voz"""
        self.voice.unmute()
        self.voice.speak(ButlerPhrases.get("unmuted"))
    
    def toggle_mute(self) -> bool:
        """Alterna silencio"""
        is_muted = self.voice.toggle_mute()
        if is_muted:
            print("🔇 Mayordomo silenciado")
        else:
            print("🔊 Mayordomo activado")
            self.voice.speak(ButlerPhrases.get("unmuted"))
        return is_muted
    
    def say(self, text: str):
        """Dice algo personalizado"""
        self.voice.speak(text)
    
    def shut_up(self):
        """Calla inmediatamente"""
        self.voice.stop_speaking()


# =============================================================================
# SINGLETON GLOBAL
# =============================================================================

_butler_instance: Optional[Butler] = None

def get_butler() -> Butler:
    """Obtiene la instancia del mayordomo"""
    global _butler_instance
    if _butler_instance is None:
        _butler_instance = Butler()
    return _butler_instance

def init_butler() -> Butler:
    """Inicializa y arranca el mayordomo"""
    butler = get_butler()
    butler.start()
    return butler

def shutdown_butler():
    """Detiene el mayordomo"""
    global _butler_instance
    if _butler_instance:
        _butler_instance.stop()
        _butler_instance = None


# =============================================================================
# TEST
# =============================================================================

if __name__ == "__main__":
    print("🎙️ Test del mayordomo BABAS")
    print("="*50)
    
    butler = init_butler()
    
    if not butler.enabled:
        print("\n⚠️ El mayordomo no está disponible.")
        print("   Asegúrate de tener:")
        print("   1. ELEVENLABS_API_KEY en tu .env")
        print("   2. pip install elevenlabs pygame")
    else:
        print("\n✅ Mayordomo listo!")
        print("\nComandos de prueba:")
        print("  1 - Saludo")
        print("  2 - Ejecutando")
        print("  3 - Éxito")
        print("  4 - Error")
        print("  m - Silenciar/Activar")
        print("  c - Callar ahora")
        print("  q - Salir")
        
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
                butler.say(f"Ha dicho: {cmd}")
    
    shutdown_butler()
    print("\n👋 Fin del test")

