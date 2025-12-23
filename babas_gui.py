"""
🎤 BABAS Control Panel - Panel de control con voz
Panel completo para controlar BABAS con voz
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
    BG_LIGHT = "#1a1a2e"
    CARD = "#16213e"
    ACCENT = "#00d4ff"
    GREEN = "#00ff88"
    RED = "#ff4757"
    ORANGE = "#ff9f43"
    PURPLE = "#a855f7"
    TEXT = "#ffffff"
    TEXT_DIM = "#666666"


# =============================================================================
# 🎛️ PANEL DE CONTROL PRINCIPAL
# =============================================================================

class BabasControlPanel(ctk.CTkToplevel):
    """
    Panel de control completo para BABAS:
    - Grabar comandos por voz
    - Ver y confirmar planes
    - Cerrar BABAS
    """
    
    def __init__(self,
                 on_command: Callable[[str], None] = None,
                 on_confirm: Callable[[bool], None] = None,
                 on_close: Callable[[], None] = None):
        super().__init__()
        
        self.on_command = on_command
        self.on_confirm = on_confirm  # True = aceptar, False = denegar
        self.on_close = on_close
        
        self.is_recording = False
        self.recorder = None
        self.transcriber = None
        self.audio_data = None
        self._pulse_job = None
        self.waiting_confirmation = False
        self.current_plan_text = ""
        
        # Configurar ventana
        self.title("🎛️ BABAS - Panel de Control")
        self.geometry("400x550")
        self.configure(fg_color=Colors.BG)
        self.resizable(False, False)
        self.attributes("-topmost", True)
        
        # Posicionar a la derecha de la pantalla
        self.update_idletasks()
        screen_width = self.winfo_screenwidth()
        x = screen_width - 420
        y = 50
        self.geometry(f"400x550+{x}+{y}")
        
        # Al cerrar
        self.protocol("WM_DELETE_WINDOW", self._on_close_click)
        
        # Inicializar audio
        self._init_audio()
        
        # Construir UI
        self._build_ui()
        
        # Resultado de grabación
        self.result_queue = queue.Queue()
        self.confirm_queue = queue.Queue()
        self.stop_queue = queue.Queue()  # Para señalizar detención
        self._stop_requested = False
    
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
        
        # === HEADER ===
        header = ctk.CTkFrame(self, fg_color=Colors.BG_LIGHT, height=50)
        header.pack(fill="x")
        header.pack_propagate(False)
        
        ctk.CTkLabel(
            header,
            text="🎛️ BABAS Control",
            font=("Segoe UI", 16, "bold"),
            text_color=Colors.ACCENT
        ).pack(side="left", padx=15, pady=10)
        
        self.status_indicator = ctk.CTkLabel(
            header,
            text="● Listo",
            font=("Segoe UI", 11),
            text_color=Colors.GREEN
        )
        self.status_indicator.pack(side="right", padx=15)
        
        # === SECCIÓN DE VOZ ===
        voice_frame = ctk.CTkFrame(self, fg_color=Colors.CARD, corner_radius=15)
        voice_frame.pack(fill="x", padx=15, pady=15)
        
        ctk.CTkLabel(
            voice_frame,
            text="🎤 Comando por Voz",
            font=("Segoe UI", 14, "bold"),
            text_color=Colors.TEXT
        ).pack(pady=(15, 5))
        
        # Botón de grabar
        self.mic_btn = ctk.CTkButton(
            voice_frame,
            text="🎤\nGrabar",
            width=100,
            height=100,
            corner_radius=50,
            font=("Segoe UI", 14),
            fg_color=Colors.BG,
            hover_color=Colors.ACCENT,
            border_width=3,
            border_color=Colors.ACCENT,
            command=self._toggle_recording
        )
        self.mic_btn.pack(pady=10)
        
        self.mic_status = ctk.CTkLabel(
            voice_frame,
            text="Clic para grabar",
            font=("Segoe UI", 11),
            text_color=Colors.TEXT_DIM
        )
        self.mic_status.pack(pady=(0, 15))
        
        # === SECCIÓN DE PLAN ===
        plan_frame = ctk.CTkFrame(self, fg_color=Colors.CARD, corner_radius=15)
        plan_frame.pack(fill="both", expand=True, padx=15, pady=(0, 15))
        
        ctk.CTkLabel(
            plan_frame,
            text="📋 Plan Actual",
            font=("Segoe UI", 14, "bold"),
            text_color=Colors.TEXT
        ).pack(anchor="w", padx=15, pady=(15, 5))
        
        # Área de texto para el plan
        self.plan_text = ctk.CTkTextbox(
            plan_frame,
            font=("Consolas", 11),
            fg_color=Colors.BG,
            text_color=Colors.TEXT,
            height=120
        )
        self.plan_text.pack(fill="both", expand=True, padx=15, pady=(0, 10))
        self.plan_text.insert("1.0", "Esperando comando...")
        self.plan_text.configure(state="disabled")
        
        # Botones de confirmación
        self.confirm_frame = ctk.CTkFrame(plan_frame, fg_color="transparent")
        self.confirm_frame.pack(fill="x", padx=15, pady=(0, 15))
        
        self.accept_btn = ctk.CTkButton(
            self.confirm_frame,
            text="✓ Ejecutar",
            width=120,
            height=40,
            font=("Segoe UI", 13, "bold"),
            fg_color=Colors.GREEN,
            hover_color="#00cc6a",
            text_color=Colors.BG,
            command=lambda: self._on_confirm(True)
        )
        self.accept_btn.pack(side="left", padx=(0, 10))
        
        self.deny_btn = ctk.CTkButton(
            self.confirm_frame,
            text="✗ Cancelar",
            width=120,
            height=40,
            font=("Segoe UI", 13),
            fg_color=Colors.RED,
            hover_color="#cc3344",
            command=lambda: self._on_confirm(False)
        )
        self.deny_btn.pack(side="left")
        
        # Ocultar botones hasta que haya plan
        self._hide_confirm_buttons()
        
        # === BOTÓN DETENER (grande y visible) ===
        self.stop_btn = ctk.CTkButton(
            self,
            text="🛑 DETENER",
            width=370,
            height=50,
            font=("Segoe UI", 16, "bold"),
            fg_color=Colors.RED,
            hover_color="#cc2233",
            text_color="white",
            command=self._on_stop_click
        )
        self.stop_btn.pack(padx=15, pady=(0, 10))
        self._hide_stop_button()  # Oculto hasta que se ejecute algo
        
        # === BOTÓN CERRAR ===
        close_frame = ctk.CTkFrame(self, fg_color="transparent")
        close_frame.pack(fill="x", padx=15, pady=(0, 15))
        
        ctk.CTkButton(
            close_frame,
            text="🚪 Cerrar BABAS",
            width=370,
            height=40,
            font=("Segoe UI", 13),
            fg_color=Colors.PURPLE,
            hover_color="#7c3aed",
            command=self._on_close_click
        ).pack()
    
    def _hide_confirm_buttons(self):
        """Oculta los botones de confirmación"""
        self.accept_btn.configure(state="disabled", fg_color=Colors.TEXT_DIM)
        self.deny_btn.configure(state="disabled", fg_color=Colors.TEXT_DIM)
        self.waiting_confirmation = False
    
    def _show_confirm_buttons(self):
        """Muestra los botones de confirmación"""
        self.accept_btn.configure(state="normal", fg_color=Colors.GREEN)
        self.deny_btn.configure(state="normal", fg_color=Colors.RED)
        self.waiting_confirmation = True
    
    def _hide_stop_button(self):
        """Oculta el botón de detener"""
        self.stop_btn.configure(state="disabled", fg_color=Colors.TEXT_DIM)
    
    def _show_stop_button(self):
        """Muestra el botón de detener"""
        self.stop_btn.configure(state="normal", fg_color=Colors.RED)
        self._stop_requested = False
    
    def _on_stop_click(self):
        """Cuando se presiona el botón de detener"""
        self._stop_requested = True
        self.stop_queue.put(True)
        self.status_indicator.configure(text="● DETENIDO", text_color=Colors.RED)
        self.set_plan_text("⛔ Ejecución detenida por el usuario.\n\nGraba otro comando.")
        self._hide_stop_button()
    
    def _toggle_recording(self):
        """Alterna entre grabar y parar"""
        if not self.recorder:
            self.mic_status.configure(text="❌ Micrófono no disponible", text_color=Colors.RED)
            return
        
        if not self.is_recording:
            self._start_recording()
        else:
            self._stop_recording()
    
    def _start_recording(self):
        """Inicia la grabación"""
        self.is_recording = True
        self.audio_data = None
        
        self.mic_btn.configure(
            text="🔴\nGrabando",
            fg_color=Colors.RED,
            border_color=Colors.RED
        )
        self.mic_status.configure(text="🔴 Grabando... (clic para enviar)", text_color=Colors.ORANGE)
        self.status_indicator.configure(text="● Grabando", text_color=Colors.RED)
        self._start_pulse()
        
        def record():
            try:
                self.audio_data = self.recorder.record_until_stopped(max_duration=60)
            except Exception as e:
                self.after(0, lambda: self.mic_status.configure(
                    text=f"❌ Error: {e}", text_color=Colors.RED
                ))
        
        self.record_thread = threading.Thread(target=record, daemon=True)
        self.record_thread.start()
    
    def _stop_recording(self):
        """Para la grabación y transcribe"""
        self.is_recording = False
        self._stop_pulse()
        
        if self.recorder:
            self.recorder.stop()
        
        self.mic_btn.configure(
            text="⏳",
            fg_color=Colors.CARD,
            border_color=Colors.ACCENT
        )
        self.mic_status.configure(text="🔄 Transcribiendo...", text_color=Colors.ACCENT)
        self.status_indicator.configure(text="● Procesando", text_color=Colors.ORANGE)
        
        def transcribe():
            try:
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
        """Cuando termina la transcripción"""
        self.mic_btn.configure(
            text="🎤\nGrabar",
            fg_color=Colors.BG,
            border_color=Colors.ACCENT
        )
        
        if error:
            self.mic_status.configure(text=f"❌ {error}", text_color=Colors.RED)
            self.status_indicator.configure(text="● Error", text_color=Colors.RED)
            return
        
        if not text:
            self.mic_status.configure(text="❌ No se pudo transcribir", text_color=Colors.RED)
            self.status_indicator.configure(text="● Error", text_color=Colors.RED)
            return
        
        self.mic_status.configure(text=f"✓ \"{text[:30]}...\"", text_color=Colors.GREEN)
        self.status_indicator.configure(text="● Listo", text_color=Colors.GREEN)
        
        # Enviar comando
        self.result_queue.put(text)
        if self.on_command:
            self.on_command(text)
    
    def _on_confirm(self, accepted: bool):
        """Cuando se acepta o rechaza el plan"""
        self._hide_confirm_buttons()
        self.confirm_queue.put(accepted)
        
        if accepted:
            self.status_indicator.configure(text="● Ejecutando", text_color=Colors.ORANGE)
        else:
            self.status_indicator.configure(text="● Cancelado", text_color=Colors.TEXT_DIM)
            self.set_plan_text("Cancelado. Graba otro comando.")
        
        if self.on_confirm:
            self.on_confirm(accepted)
    
    def _on_close_click(self):
        """Al hacer clic en cerrar"""
        if self.is_recording and self.recorder:
            self.recorder.stop()
        
        self.result_queue.put("__CLOSE__")
        
        if self.on_close:
            self.on_close()
        
        self.destroy()
    
    def _start_pulse(self):
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
    
    # === API PÚBLICA ===
    
    def set_plan_text(self, text: str):
        """Muestra el plan en el panel"""
        self.current_plan_text = text
        self.plan_text.configure(state="normal")
        self.plan_text.delete("1.0", "end")
        self.plan_text.insert("1.0", text)
        self.plan_text.configure(state="disabled")
    
    def ask_confirmation(self, plan_text: str):
        """Muestra el plan y pide confirmación"""
        self.set_plan_text(plan_text)
        self._show_confirm_buttons()
        self.status_indicator.configure(text="● Confirmar?", text_color=Colors.PURPLE)
    
    def set_status(self, text: str, color: str = None):
        """Actualiza el indicador de estado"""
        self.status_indicator.configure(
            text=f"● {text}",
            text_color=color or Colors.TEXT
        )
    
    def get_command(self, timeout: float = None) -> Optional[str]:
        """Obtiene el próximo comando (bloqueante)"""
        try:
            return self.result_queue.get(timeout=timeout)
        except queue.Empty:
            return None
    
    def get_confirmation(self, timeout: float = None) -> Optional[bool]:
        """Obtiene la confirmación (bloqueante)"""
        try:
            return self.confirm_queue.get(timeout=timeout)
        except queue.Empty:
            return None
    
    def start_execution(self):
        """Llamar cuando inicia la ejecución de un plan"""
        self._stop_requested = False
        # Limpiar cola de stop
        while not self.stop_queue.empty():
            try:
                self.stop_queue.get_nowait()
            except:
                break
        self._show_stop_button()
    
    def end_execution(self):
        """Llamar cuando termina la ejecución"""
        self._hide_stop_button()
    
    def is_stop_requested(self) -> bool:
        """Verifica si se pidió detener"""
        # Verificar cola sin bloquear
        try:
            self.stop_queue.get_nowait()
            self._stop_requested = True
        except queue.Empty:
            pass
        return self._stop_requested
    
    def clear_stop(self):
        """Limpia la señal de detención"""
        self._stop_requested = False
        while not self.stop_queue.empty():
            try:
                self.stop_queue.get_nowait()
            except:
                break


# =============================================================================
# 🔗 MANAGER PARA BABAS
# =============================================================================

class VoiceControlManager:
    """Maneja la GUI de control desde babas.py"""
    
    def __init__(self):
        self.root = None
        self.panel = None
        self.is_open = False
        self._close_requested = False
    
    def open(self, butler=None):
        """Abre el panel de control"""
        if self.is_open:
            return
        
        self.is_open = True
        self._close_requested = False
        
        def run_gui():
            self.root = ctk.CTk()
            self.root.withdraw()
            
            self.panel = BabasControlPanel(
                on_close=self._on_panel_close
            )
            
            if butler and hasattr(butler, 'say'):
                butler.say("Panel de control listo, señor.")
            
            self.root.mainloop()
        
        self._gui_thread = threading.Thread(target=run_gui, daemon=True)
        self._gui_thread.start()
        
        # Esperar a que se cree el panel
        import time
        for _ in range(50):  # 5 segundos máximo
            if self.panel:
                break
            time.sleep(0.1)
    
    def _on_panel_close(self):
        """Cuando se cierra el panel"""
        self.is_open = False
        self._close_requested = True
        if self.root:
            self.root.quit()
    
    def get_voice_command(self, timeout: float = 0.5) -> Optional[str]:
        """Obtiene un comando de voz"""
        if not self.panel or not self.is_open:
            return None
        return self.panel.get_command(timeout)
    
    def show_plan_and_confirm(self, plan_text: str, timeout: float = 60) -> Optional[bool]:
        """Muestra el plan y espera confirmación"""
        if not self.panel or not self.is_open:
            return None
        
        self.panel.after(0, lambda: self.panel.ask_confirmation(plan_text))
        return self.panel.get_confirmation(timeout)
    
    def set_status(self, text: str, color: str = None):
        """Actualiza el estado"""
        if self.panel and self.is_open:
            self.panel.after(0, lambda: self.panel.set_status(text, color))
    
    def show_message(self, text: str):
        """Muestra un mensaje en el área de plan"""
        if self.panel and self.is_open:
            self.panel.after(0, lambda: self.panel.set_plan_text(text))
    
    def close_requested(self) -> bool:
        """Verifica si se pidió cerrar"""
        return self._close_requested
    
    def start_execution(self):
        """Inicia modo ejecución (muestra botón detener)"""
        if self.panel and self.is_open:
            self.panel.after(0, self.panel.start_execution)
    
    def end_execution(self):
        """Termina modo ejecución (oculta botón detener)"""
        if self.panel and self.is_open:
            self.panel.after(0, self.panel.end_execution)
    
    def is_stop_requested(self) -> bool:
        """Verifica si se pidió detener la ejecución"""
        if self.panel and self.is_open:
            return self.panel.is_stop_requested()
        return False
    
    def clear_stop(self):
        """Limpia la señal de detención"""
        if self.panel and self.is_open:
            self.panel.clear_stop()
    
    def close(self):
        """Cierra el panel"""
        if self.panel:
            try:
                self.panel.after(0, self.panel._on_close_click)
            except:
                pass
        self.is_open = False


# Singleton
_voice_manager: Optional[VoiceControlManager] = None

def get_voice_gui() -> VoiceControlManager:
    """Obtiene el manager de GUI"""
    global _voice_manager
    if _voice_manager is None:
        _voice_manager = VoiceControlManager()
    return _voice_manager


# Mantener compatibilidad
VoiceInputGUI = VoiceControlManager


# =============================================================================
# 🏁 TEST
# =============================================================================

if __name__ == "__main__":
    print("[*] Test del Panel de Control BABAS")
    
    root = ctk.CTk()
    root.withdraw()
    
    def on_command(text):
        print(f"[+] Comando: {text}")
    
    def on_confirm(accepted):
        print(f"[+] Confirmado: {accepted}")
    
    def on_close():
        print("[*] Cerrando")
        root.quit()
    
    panel = BabasControlPanel(
        on_command=on_command,
        on_confirm=on_confirm,
        on_close=on_close
    )
    
    # Test: mostrar un plan después de 3 segundos
    def test_plan():
        panel.ask_confirmation(
            "Plan de ejemplo:\n"
            "1. goto → https://google.com\n"
            "2. smart_search → gatos\n"
            "3. wait → 2000ms"
        )
    
    root.after(3000, test_plan)
    root.mainloop()
