"""
🎤 BABAS Voice GUI - Herramienta de grabación de voz
Solo sirve para grabar audio fácilmente con un botón
Se usa desde babas_text.py cuando se activa modo voz
"""

import customtkinter as ctk
import threading
import queue
from typing import Optional, Callable
from datetime import datetime

# Configurar tema
ctk.set_appearance_mode("dark")

# =============================================================================
# 🎨 COLORES
# =============================================================================

class Colors:
    BG = "#0f0f1a"
    CARD = "#1a1a2e"
    ACCENT = "#00d4ff"
    GREEN = "#00ff88"
    RED = "#ff4757"
    ORANGE = "#ff9f43"
    TEXT = "#ffffff"
    TEXT_DIM = "#666666"


# =============================================================================
# 🎤 VENTANA DE GRABACIÓN
# =============================================================================

class VoiceRecorderWindow(ctk.CTkToplevel):
    """
    Ventana flotante para grabar voz fácilmente.
    Clic para grabar, clic para parar.
    """
    
    def __init__(self, 
                 on_recording_complete: Callable[[str], None] = None,
                 on_close: Callable[[], None] = None):
        super().__init__()
        
        self.on_recording_complete = on_recording_complete
        self.on_close = on_close
        self.is_recording = False
        self.recorder = None
        self.transcriber = None
        self.audio_data = None
        self._pulse_job = None
        
        # Configurar ventana
        self.title("🎤 BABAS - Grabación de Voz")
        self.geometry("350x300")
        self.configure(fg_color=Colors.BG)
        self.resizable(False, False)
        self.attributes("-topmost", True)  # Siempre visible
        
        # Centrar en pantalla
        self.update_idletasks()
        x = (self.winfo_screenwidth() // 2) - 175
        y = (self.winfo_screenheight() // 2) - 150
        self.geometry(f"350x300+{x}+{y}")
        
        # Al cerrar
        self.protocol("WM_DELETE_WINDOW", self._on_window_close)
        
        # Inicializar audio
        self._init_audio()
        
        # Construir UI
        self._build_ui()
    
    def _init_audio(self):
        """Inicializa los sistemas de audio"""
        try:
            from babas_listen import VoiceListener
            listener = VoiceListener()
            if listener.is_available():
                self.recorder = listener.recorder
                self.transcriber = listener.transcriber
        except Exception as e:
            print(f"[!] Error inicializando audio: {e}")
    
    def _build_ui(self):
        """Construye la interfaz"""
        
        # Título
        ctk.CTkLabel(
            self,
            text="🎤 Grabación de Voz",
            font=("Segoe UI", 18, "bold"),
            text_color=Colors.TEXT
        ).pack(pady=(20, 5))
        
        ctk.CTkLabel(
            self,
            text="Clic para grabar, clic para enviar",
            font=("Segoe UI", 11),
            text_color=Colors.TEXT_DIM
        ).pack(pady=(0, 20))
        
        # Botón grande de micrófono
        self.mic_btn = ctk.CTkButton(
            self,
            text="🎤\nGrabar",
            width=120,
            height=120,
            corner_radius=60,
            font=("Segoe UI", 16),
            fg_color=Colors.CARD,
            hover_color=Colors.ACCENT,
            border_width=4,
            border_color=Colors.ACCENT,
            command=self._toggle_recording
        )
        self.mic_btn.pack(pady=10)
        
        # Estado
        self.status_label = ctk.CTkLabel(
            self,
            text="Listo para grabar",
            font=("Segoe UI", 12),
            text_color=Colors.TEXT_DIM
        )
        self.status_label.pack(pady=15)
        
        # Botón cerrar
        ctk.CTkButton(
            self,
            text="Volver a modo texto",
            width=150,
            height=30,
            font=("Segoe UI", 11),
            fg_color=Colors.CARD,
            hover_color=Colors.BG,
            command=self._on_window_close
        ).pack(pady=(10, 20))
    
    def _toggle_recording(self):
        """Alterna entre grabar y parar"""
        if not self.recorder:
            self.status_label.configure(text="❌ Micrófono no disponible", text_color=Colors.RED)
            return
        
        if not self.is_recording:
            self._start_recording()
        else:
            self._stop_recording()
    
    def _start_recording(self):
        """Inicia la grabación"""
        self.is_recording = True
        self.audio_data = None
        
        # Actualizar UI
        self.mic_btn.configure(
            text="🔴\nGrabando...",
            fg_color=Colors.RED,
            border_color=Colors.RED
        )
        self.status_label.configure(text="🔴 Grabando... (clic para enviar)", text_color=Colors.ORANGE)
        self._start_pulse()
        
        # Grabar en thread separado
        def record():
            try:
                # Usar el nuevo método que graba TODO hasta que se pare
                self.audio_data = self.recorder.record_until_stopped(max_duration=60)
            except Exception as e:
                self.after(0, lambda: self.status_label.configure(
                    text=f"❌ Error: {e}", text_color=Colors.RED
                ))
        
        self.record_thread = threading.Thread(target=record, daemon=True)
        self.record_thread.start()
    
    def _stop_recording(self):
        """Para la grabación y transcribe"""
        self.is_recording = False
        self._stop_pulse()
        
        # Parar el recorder
        if self.recorder:
            self.recorder.stop()
        
        # Actualizar UI
        self.mic_btn.configure(
            text="⏳",
            fg_color=Colors.CARD,
            border_color=Colors.ACCENT
        )
        self.status_label.configure(text="🔄 Transcribiendo...", text_color=Colors.ACCENT)
        
        # Transcribir en thread
        def transcribe():
            try:
                # Esperar a que termine el thread de grabación
                if hasattr(self, 'record_thread'):
                    self.record_thread.join(timeout=2)
                
                if self.audio_data is None or len(self.audio_data) == 0:
                    self.after(0, lambda: self._on_transcription_done(None, "No se grabó audio"))
                    return
                
                text = self.transcriber.transcribe(self.audio_data)
                self.after(0, lambda: self._on_transcription_done(text, None))
                
            except Exception as e:
                self.after(0, lambda: self._on_transcription_done(None, str(e)))
        
        threading.Thread(target=transcribe, daemon=True).start()
    
    def _on_transcription_done(self, text: Optional[str], error: Optional[str]):
        """Callback cuando termina la transcripción"""
        # Restaurar botón
        self.mic_btn.configure(
            text="🎤\nGrabar",
            fg_color=Colors.CARD,
            border_color=Colors.ACCENT
        )
        
        if error:
            self.status_label.configure(text=f"❌ {error}", text_color=Colors.RED)
            return
        
        if not text:
            self.status_label.configure(text="❌ No se pudo transcribir", text_color=Colors.RED)
            return
        
        # Éxito
        self.status_label.configure(text=f"✅ \"{text[:30]}...\"", text_color=Colors.GREEN)
        
        # Enviar al callback
        if self.on_recording_complete:
            self.on_recording_complete(text)
    
    def _start_pulse(self):
        """Inicia animación de pulso"""
        self._pulse_state = 0
        self._pulse()
    
    def _pulse(self):
        if not self.is_recording:
            return
        colors = [Colors.RED, Colors.ORANGE]
        self._pulse_state = (self._pulse_state + 1) % 2
        self.mic_btn.configure(border_color=colors[self._pulse_state])
        self._pulse_job = self.after(400, self._pulse)
    
    def _stop_pulse(self):
        if self._pulse_job:
            self.after_cancel(self._pulse_job)
            self._pulse_job = None
    
    def _on_window_close(self):
        """Al cerrar la ventana"""
        if self.is_recording and self.recorder:
            self.recorder.stop()
        if self.on_close:
            self.on_close()
        self.destroy()


# =============================================================================
# 🔗 INTERFAZ PARA BABAS_TEXT
# =============================================================================

class VoiceInputGUI:
    """
    Clase que maneja la GUI de voz desde babas_text.py
    """
    
    def __init__(self):
        self.root = None
        self.window = None
        self.result_queue = queue.Queue()
        self.is_open = False
        self._gui_thread = None
    
    def open(self, butler=None):
        """Abre la ventana de grabación"""
        if self.is_open:
            return
        
        self.is_open = True
        
        # Crear root oculto si no existe
        def run_gui():
            self.root = ctk.CTk()
            self.root.withdraw()  # Ocultar root
            
            self.window = VoiceRecorderWindow(
                on_recording_complete=self._on_recording_complete,
                on_close=self._on_close
            )
            
            if butler and hasattr(butler, 'say'):
                butler.say("Ventana de grabación lista, señor.")
            
            self.root.mainloop()
        
        self._gui_thread = threading.Thread(target=run_gui, daemon=True)
        self._gui_thread.start()
    
    def _on_recording_complete(self, text: str):
        """Cuando se completa una grabación"""
        self.result_queue.put(("text", text))
    
    def _on_close(self):
        """Cuando se cierra la ventana"""
        self.is_open = False
        self.result_queue.put(("close", None))
        if self.root:
            self.root.quit()
    
    def get_voice_command(self, timeout: float = None) -> Optional[str]:
        """
        Espera y retorna el próximo comando de voz.
        Retorna None si se cierra la ventana.
        """
        try:
            event_type, data = self.result_queue.get(timeout=timeout)
            if event_type == "text":
                return data
            elif event_type == "close":
                return None
        except queue.Empty:
            return None
    
    def close(self):
        """Cierra la GUI"""
        if self.window:
            try:
                self.window.after(0, self.window._on_window_close)
            except:
                pass
        self.is_open = False


# Singleton
_voice_gui: Optional[VoiceInputGUI] = None

def get_voice_gui() -> VoiceInputGUI:
    """Obtiene la instancia de la GUI de voz"""
    global _voice_gui
    if _voice_gui is None:
        _voice_gui = VoiceInputGUI()
    return _voice_gui


# =============================================================================
# 🏁 TEST
# =============================================================================

if __name__ == "__main__":
    print("[*] Test de GUI de grabación de voz")
    
    # Crear ventana de prueba
    root = ctk.CTk()
    root.withdraw()
    
    def on_complete(text):
        print(f"[+] Transcrito: {text}")
    
    def on_close():
        print("[*] Ventana cerrada")
        root.quit()
    
    window = VoiceRecorderWindow(
        on_recording_complete=on_complete,
        on_close=on_close
    )
    
    root.mainloop()
