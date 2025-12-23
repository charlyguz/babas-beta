"""
🖥️ BABAS GUI - Interfaz para comandos de voz y texto
Misma funcionalidad que babas_text.py pero con botón fácil para grabar audio
"""

import customtkinter as ctk
import threading
import queue
import sys
import os
from datetime import datetime
from typing import Optional
import time

# Configurar tema
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

# Importar módulos de BABAS
try:
    from babas_voice import BabasVoice
    VOICE_AVAILABLE = True
except ImportError:
    VOICE_AVAILABLE = False
    print("[!] babas_voice no disponible")

try:
    from babas_listen import VoiceListener, VoiceInputMode
    LISTEN_AVAILABLE = True
except ImportError:
    LISTEN_AVAILABLE = False
    print("[!] babas_listen no disponible")


# =============================================================================
# 🎨 COLORES
# =============================================================================

class Colors:
    BG = "#1a1a2e"
    BG_DARK = "#0f0f1a"
    CARD = "#16213e"
    ACCENT = "#00d4ff"
    GREEN = "#00ff88"
    RED = "#ff4757"
    ORANGE = "#ff9f43"
    TEXT = "#ffffff"
    TEXT_DIM = "#888888"


# =============================================================================
# 📝 LOGGER THREAD-SAFE
# =============================================================================

class GUILogger:
    """Logger que actualiza la GUI desde cualquier thread"""
    
    def __init__(self):
        self.queue = queue.Queue()
        self.text_widget = None
    
    def set_widget(self, widget):
        self.text_widget = widget
    
    def log(self, msg: str, level: str = "info"):
        ts = datetime.now().strftime("%H:%M:%S")
        self.queue.put((ts, level, msg))
        # También imprimir en consola
        print(f"[{ts}] {msg}")
    
    def process_queue(self):
        """Llamar desde el main thread para actualizar la GUI"""
        if not self.text_widget:
            return
        
        while not self.queue.empty():
            try:
                ts, level, msg = self.queue.get_nowait()
                self._add_to_widget(ts, level, msg)
            except:
                break
    
    def _add_to_widget(self, ts: str, level: str, msg: str):
        self.text_widget.configure(state="normal")
        self.text_widget._textbox.insert("end", f"[{ts}] ", "time")
        self.text_widget._textbox.insert("end", f"{msg}\n", level)
        self.text_widget._textbox.see("end")
        self.text_widget.configure(state="disabled")


# Logger global
logger = GUILogger()

def log(msg: str, level: str = "info"):
    logger.log(msg, level)


# =============================================================================
# 🎤 BOTÓN DE GRABACIÓN (TOGGLE)
# =============================================================================

class RecordButton(ctk.CTkButton):
    """
    Botón de grabación toggle:
    - Clic 1: Empieza a grabar
    - Clic 2: Para y envía
    """
    
    def __init__(self, master, on_toggle=None, **kwargs):
        self.is_recording = False
        self.on_toggle = on_toggle  # callback(is_recording: bool)
        self._pulse_job = None
        
        super().__init__(
            master,
            text="🎤\nGrabar",
            width=100,
            height=100,
            corner_radius=50,
            font=("Segoe UI", 14),
            fg_color=Colors.CARD,
            hover_color=Colors.ACCENT,
            border_width=3,
            border_color=Colors.ACCENT,
            command=self._toggle,
            **kwargs
        )
    
    def _toggle(self):
        self.is_recording = not self.is_recording
        
        if self.is_recording:
            # Empezar grabación
            self.configure(
                text="🔴\nGrabando...",
                fg_color=Colors.RED,
                border_color=Colors.RED,
                hover_color=Colors.ORANGE
            )
            self._start_pulse()
        else:
            # Parar grabación
            self._stop_pulse()
            self.configure(
                text="🎤\nGrabar",
                fg_color=Colors.CARD,
                border_color=Colors.ACCENT,
                hover_color=Colors.ACCENT
            )
        
        if self.on_toggle:
            self.on_toggle(self.is_recording)
    
    def _start_pulse(self):
        self._pulse_state = 0
        self._pulse()
    
    def _pulse(self):
        if not self.is_recording:
            return
        colors = [Colors.RED, Colors.ORANGE]
        self._pulse_state = (self._pulse_state + 1) % 2
        self.configure(border_color=colors[self._pulse_state])
        self._pulse_job = self.after(400, self._pulse)
    
    def _stop_pulse(self):
        if self._pulse_job:
            self.after_cancel(self._pulse_job)
            self._pulse_job = None
    
    def reset(self):
        """Resetea el botón al estado inicial"""
        self.is_recording = False
        self._stop_pulse()
        self.configure(
            text="🎤\nGrabar",
            fg_color=Colors.CARD,
            border_color=Colors.ACCENT
        )


# =============================================================================
# 🚀 APLICACIÓN PRINCIPAL
# =============================================================================

class BabasGUI(ctk.CTk):
    """GUI principal de BABAS"""
    
    def __init__(self):
        super().__init__()
        
        self.title("BABAS - Asistente de Automatización")
        self.geometry("650x600")
        self.configure(fg_color=Colors.BG_DARK)
        self.resizable(True, True)
        
        # Estado
        self.is_connected = False
        self.is_recording = False
        self.butler = None
        self.listener = None
        self.recorder = None
        
        # Para ejecutar BABAS en thread separado
        self.babas_thread = None
        self.command_queue = queue.Queue()
        self.audio_data = None
        
        # Construir UI
        self._build_ui()
        
        # Iniciar actualizador de logs
        self._start_log_updater()
        
        # Inicializar sistemas
        self._init_systems()
        
        log("BABAS GUI iniciada", "success")
        log("Escribe 'conectar' o usa el botón", "info")
    
    def _build_ui(self):
        """Construye la interfaz"""
        
        # === HEADER ===
        header = ctk.CTkFrame(self, fg_color=Colors.BG, height=60)
        header.pack(fill="x")
        header.pack_propagate(False)
        
        ctk.CTkLabel(
            header,
            text="🤖 BABAS",
            font=("Segoe UI", 22, "bold"),
            text_color=Colors.ACCENT
        ).pack(side="left", padx=20, pady=15)
        
        # Status
        self.status_label = ctk.CTkLabel(
            header,
            text="⚪ Sin conectar",
            font=("Segoe UI", 12),
            text_color=Colors.TEXT_DIM
        )
        self.status_label.pack(side="right", padx=20)
        
        # Botón conectar
        self.connect_btn = ctk.CTkButton(
            header,
            text="🌐 Conectar",
            width=100,
            height=35,
            font=("Segoe UI", 12),
            fg_color=Colors.ACCENT,
            hover_color=Colors.GREEN,
            command=self._on_connect
        )
        self.connect_btn.pack(side="right", padx=5)
        
        # === CONTENIDO PRINCIPAL ===
        content = ctk.CTkFrame(self, fg_color="transparent")
        content.pack(fill="both", expand=True, padx=20, pady=20)
        
        # --- Sección de entrada ---
        input_section = ctk.CTkFrame(content, fg_color=Colors.BG, corner_radius=15)
        input_section.pack(fill="x")
        
        ctk.CTkLabel(
            input_section,
            text="¿Qué quieres que haga?",
            font=("Segoe UI", 16, "bold"),
            text_color=Colors.TEXT
        ).pack(anchor="w", padx=20, pady=(20, 10))
        
        # Campo de texto + botón enviar
        entry_frame = ctk.CTkFrame(input_section, fg_color="transparent")
        entry_frame.pack(fill="x", padx=20, pady=(0, 20))
        
        self.entry = ctk.CTkEntry(
            entry_frame,
            placeholder_text="Escribe un comando o graba tu voz...",
            font=("Segoe UI", 14),
            height=50,
            fg_color=Colors.BG_DARK,
            border_color=Colors.ACCENT,
            border_width=2
        )
        self.entry.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.entry.bind("<Return>", lambda e: self._send_command())
        
        self.send_btn = ctk.CTkButton(
            entry_frame,
            text="▶",
            width=50,
            height=50,
            font=("Segoe UI", 20),
            fg_color=Colors.ACCENT,
            hover_color=Colors.GREEN,
            command=self._send_command
        )
        self.send_btn.pack(side="right")
        
        # --- Sección de voz ---
        voice_section = ctk.CTkFrame(content, fg_color="transparent")
        voice_section.pack(fill="x", pady=20)
        
        # Botón de grabar (centrado)
        self.record_btn = RecordButton(voice_section, on_toggle=self._on_record_toggle)
        self.record_btn.pack()
        
        ctk.CTkLabel(
            voice_section,
            text="Clic para grabar, clic para enviar",
            font=("Segoe UI", 11),
            text_color=Colors.TEXT_DIM
        ).pack(pady=(10, 0))
        
        # --- Sección de logs ---
        log_section = ctk.CTkFrame(content, fg_color=Colors.BG, corner_radius=15)
        log_section.pack(fill="both", expand=True)
        
        log_header = ctk.CTkFrame(log_section, fg_color="transparent")
        log_header.pack(fill="x", padx=15, pady=(10, 0))
        
        ctk.CTkLabel(
            log_header,
            text="📋 Actividad",
            font=("Segoe UI", 12, "bold"),
            text_color=Colors.TEXT_DIM
        ).pack(side="left")
        
        ctk.CTkButton(
            log_header,
            text="Limpiar",
            width=60,
            height=25,
            font=("Segoe UI", 10),
            fg_color=Colors.CARD,
            hover_color=Colors.BG_DARK,
            command=self._clear_logs
        ).pack(side="right")
        
        self.logs = ctk.CTkTextbox(
            log_section,
            font=("Consolas", 11),
            fg_color=Colors.BG_DARK,
            text_color=Colors.TEXT,
            state="disabled"
        )
        self.logs.pack(fill="both", expand=True, padx=15, pady=(5, 15))
        
        # Configurar tags de colores
        self.logs._textbox.tag_config("time", foreground=Colors.TEXT_DIM)
        self.logs._textbox.tag_config("info", foreground=Colors.ACCENT)
        self.logs._textbox.tag_config("success", foreground=Colors.GREEN)
        self.logs._textbox.tag_config("error", foreground=Colors.RED)
        self.logs._textbox.tag_config("warning", foreground=Colors.ORANGE)
        
        # Conectar logger
        logger.set_widget(self.logs)
    
    def _start_log_updater(self):
        """Actualiza logs desde la cola cada 100ms"""
        def update():
            logger.process_queue()
            self.after(100, update)
        self.after(100, update)
    
    def _init_systems(self):
        """Inicializa el mayordomo y el listener de voz"""
        # Mayordomo
        if VOICE_AVAILABLE:
            try:
                self.butler = BabasVoice()
                if self.butler.enabled:
                    log("🎙️ Mayordomo activado", "success")
                    self.butler.greet()
            except Exception as e:
                log(f"Mayordomo no disponible: {e}", "warning")
        
        # Listener de voz
        if LISTEN_AVAILABLE:
            try:
                self.listener = VoiceListener()
                if self.listener.is_available():
                    self.recorder = self.listener.recorder
                    log("🎤 Grabación de voz lista", "success")
                else:
                    log("Micrófono no disponible", "warning")
            except Exception as e:
                log(f"Listener no disponible: {e}", "warning")
    
    def _clear_logs(self):
        """Limpia el área de logs"""
        self.logs.configure(state="normal")
        self.logs.delete("1.0", "end")
        self.logs.configure(state="disabled")
    
    def _on_connect(self):
        """Conecta al navegador"""
        log("Conectando al navegador...", "info")
        if self.butler and self.butler.enabled:
            self.butler.say("Conectando al navegador, señor.")
        
        # Ejecutar en thread separado
        def connect():
            try:
                # Importar aquí para evitar problemas
                from playwright.sync_api import sync_playwright
                import babas_text as bt
                
                with sync_playwright() as p:
                    # Intentar conectar a navegador existente
                    browser, port, name = bt.try_connect_to_browser(p)
                    
                    if browser:
                        self.after(0, lambda: self._on_connected(name, port))
                    else:
                        # Lanzar nuevo navegador
                        preferred = bt.get_preferred_browser()
                        if preferred:
                            success, port, msg = bt.launch_browser_with_debug(preferred)
                            if success:
                                time.sleep(2)
                                browser, _, _ = bt.try_connect_to_browser(p)
                                if browser:
                                    self.after(0, lambda: self._on_connected(preferred, port))
                                    return
                        
                        self.after(0, lambda: log("No se pudo conectar", "error"))
            except Exception as e:
                self.after(0, lambda: log(f"Error: {e}", "error"))
        
        threading.Thread(target=connect, daemon=True).start()
    
    def _on_connected(self, browser_name: str, port: int):
        """Callback cuando se conecta al navegador"""
        self.is_connected = True
        self.status_label.configure(
            text=f"🟢 {browser_name.title()} (:{port})",
            text_color=Colors.GREEN
        )
        self.connect_btn.configure(text="✓ Conectado", fg_color=Colors.GREEN)
        log(f"Conectado a {browser_name}", "success")
        
        if self.butler and self.butler.enabled:
            self.butler.say(f"Conectado a {browser_name}, señor. Listo para sus órdenes.")
    
    def _on_record_toggle(self, is_recording: bool):
        """Callback cuando se presiona el botón de grabar"""
        if is_recording:
            # Empezar a grabar
            self.is_recording = True
            log("🔴 Grabando... (clic de nuevo para enviar)", "warning")
            
            if self.butler and self.butler.enabled:
                self.butler.say("Escuchando, señor.")
            
            # Empezar grabación en thread
            if self.recorder:
                self.audio_data = None
                threading.Thread(target=self._record_audio, daemon=True).start()
        else:
            # Parar y procesar
            self.is_recording = False
            
            if self.recorder:
                self.recorder.stop()
            
            log("Procesando audio...", "info")
            
            # Procesar en thread
            threading.Thread(target=self._process_audio, daemon=True).start()
    
    def _record_audio(self):
        """Graba audio hasta que is_recording sea False"""
        if not self.recorder:
            return
        
        try:
            # Grabar hasta que se detenga
            self.audio_data = self.recorder.record_until_silence(
                max_duration=30,
                silence_duration=60  # No parar por silencio, solo por botón
            )
        except Exception as e:
            self.after(0, lambda: log(f"Error grabando: {e}", "error"))
    
    def _process_audio(self):
        """Procesa el audio grabado"""
        if not self.listener or self.audio_data is None:
            self.after(0, lambda: log("No hay audio para procesar", "warning"))
            self.after(0, lambda: self.record_btn.reset())
            return
        
        try:
            # Transcribir
            self.after(0, lambda: log("🔄 Transcribiendo...", "info"))
            
            text = self.listener.transcriber.transcribe(self.audio_data)
            
            if text:
                self.after(0, lambda: log(f"📝 \"{text}\"", "success"))
                self.after(0, lambda: self._set_entry_text(text))
                
                if self.butler and self.butler.enabled:
                    self.butler.say(f"Entendido: {text[:50]}")
                
                # Ejecutar el comando automáticamente
                self.after(100, lambda: self._execute_command(text))
            else:
                self.after(0, lambda: log("No se pudo transcribir", "error"))
                if self.butler and self.butler.enabled:
                    self.butler.say("No entendí, señor. ¿Puede repetir?")
        
        except Exception as e:
            self.after(0, lambda: log(f"Error: {e}", "error"))
        
        finally:
            self.audio_data = None
    
    def _set_entry_text(self, text: str):
        """Pone texto en el campo de entrada"""
        self.entry.delete(0, "end")
        self.entry.insert(0, text)
    
    def _send_command(self):
        """Envía el comando del campo de texto"""
        text = self.entry.get().strip()
        if text:
            self.entry.delete(0, "end")
            self._execute_command(text)
    
    def _execute_command(self, command: str):
        """Ejecuta un comando"""
        log(f">>> {command}", "info")
        
        cmd = command.lower().strip()
        
        # Comandos especiales de la GUI
        if cmd in ["salir", "exit", "q"]:
            if self.butler and self.butler.enabled:
                self.butler.goodbye()
            self.after(1000, self.quit)
            return
        
        if cmd in ["conectar", "connect"]:
            self._on_connect()
            return
        
        if cmd in ["ayuda", "help"]:
            log("Comandos: conectar, hackear, nueva, screenshot, salir", "info")
            return
        
        if cmd in ["calla", "callate", "silencio"]:
            if self.butler and self.butler.enabled:
                self.butler.shut_up()
                log("Mayordomo silenciado", "info")
            return
        
        # Ejecutar en thread separado
        threading.Thread(
            target=self._run_babas_command,
            args=(command,),
            daemon=True
        ).start()
    
    def _run_babas_command(self, command: str):
        """Ejecuta un comando de BABAS (en thread separado)"""
        try:
            from playwright.sync_api import sync_playwright
            import babas_text as bt
            
            with sync_playwright() as p:
                # Conectar al navegador
                browser, port, name = bt.try_connect_to_browser(p)
                
                if not browser:
                    self.after(0, lambda: log("Primero conecta al navegador", "error"))
                    return
                
                # Obtener página
                context = browser.contexts[0]
                pages = context.pages
                
                if not pages:
                    page = context.new_page()
                else:
                    page = pages[-1]
                
                # Easter egg: hackear
                if any(w in command.lower() for w in ["hackear", "hackea", "hack"]):
                    self.after(0, lambda: log("🔓 Iniciando protocolo de intrusión...", "warning"))
                    bt.run_hacker_mode(page, self.butler)
                    self.after(0, lambda: log("Hackeo completado", "success"))
                    return
                
                # Crear tab manager y executor
                tab_manager = bt.TabManager(context)
                tab_manager.tabs["main"] = page
                
                executor = bt.ActionExecutor(tab_manager)
                executor.current_tab_name = "main"
                executor.current_page = page
                
                # Mayordomo anuncia
                if self.butler and self.butler.enabled:
                    self.butler.planning()
                
                # Crear plan
                self.after(0, lambda: log("Creando plan...", "info"))
                plan = bt.create_plan(command)
                
                # Mostrar pasos
                for i, step in enumerate(plan.steps, 1):
                    details = step.value or step.selector or step.tab_name or ""
                    if details:
                        details = f" → {details[:30]}"
                    self.after(0, lambda s=step, d=details, n=i: log(f"  {n}. {s.action}{d}", "info"))
                
                # Ejecutar
                if self.butler and self.butler.enabled:
                    self.butler.executing()
                
                self.after(0, lambda: log("Ejecutando...", "info"))
                success = bt.run_plan_with_recovery(executor, plan, command)
                
                if success:
                    self.after(0, lambda: log("✅ ¡Completado!", "success"))
                    if self.butler and self.butler.enabled:
                        self.butler.success()
                else:
                    self.after(0, lambda: log("⚠️ Completado con errores", "warning"))
        
        except Exception as e:
            self.after(0, lambda: log(f"Error: {e}", "error"))
            if self.butler and self.butler.enabled:
                self.butler.error(str(e))


# =============================================================================
# 🏁 MAIN
# =============================================================================

def main():
    print("[*] Iniciando BABAS GUI...")
    print("[*] Misma funcionalidad que terminal, pero con botón para grabar voz")
    print()
    
    app = BabasGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
