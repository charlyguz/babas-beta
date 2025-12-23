"""
🖥️ BABAS GUI - Interfaz Gráfica Moderna
Una interfaz cyberpunk para el asistente de automatización web
"""

import customtkinter as ctk
import threading
import asyncio
import queue
import sys
import os
from datetime import datetime
from typing import Optional, Callable
import time

# Configurar tema antes de importar
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

# =============================================================================
# 🎨 TEMA CYBERPUNK
# =============================================================================

class CyberpunkTheme:
    """Colores y estilos para el tema cyberpunk"""
    
    # Colores principales
    BG_DARK = "#0a0a0f"
    BG_MEDIUM = "#12121a"
    BG_LIGHT = "#1a1a2e"
    BG_CARD = "#16213e"
    
    # Acentos
    NEON_GREEN = "#00ff9f"
    NEON_BLUE = "#00d4ff"
    NEON_PURPLE = "#bf00ff"
    NEON_PINK = "#ff006e"
    NEON_ORANGE = "#ff9500"
    
    # Texto
    TEXT_PRIMARY = "#ffffff"
    TEXT_SECONDARY = "#a0a0a0"
    TEXT_MUTED = "#606060"
    
    # Estados
    SUCCESS = "#00ff9f"
    ERROR = "#ff4444"
    WARNING = "#ffaa00"
    INFO = "#00d4ff"
    
    # Fuentes
    FONT_MONO = ("JetBrains Mono", "Consolas", "Courier New")
    FONT_MAIN = ("Segoe UI", "SF Pro Display", "Helvetica")


# =============================================================================
# 📝 LOGGER CON COLA
# =============================================================================

class LogQueue:
    """Cola thread-safe para logs"""
    
    def __init__(self):
        self.queue = queue.Queue()
        self.callbacks = []
    
    def log(self, message: str, level: str = "info"):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.queue.put((timestamp, level, message))
        for cb in self.callbacks:
            try:
                cb()
            except:
                pass
    
    def on_log(self, callback: Callable):
        self.callbacks.append(callback)
    
    def get_all(self):
        items = []
        while not self.queue.empty():
            items.append(self.queue.get_nowait())
        return items


# Global log queue
log_queue = LogQueue()


def log(message: str, level: str = "info"):
    """Función global de log"""
    log_queue.log(message, level)


# =============================================================================
# 🎤 BOTÓN DE MICRÓFONO ANIMADO
# =============================================================================

class MicrophoneButton(ctk.CTkButton):
    """Botón de micrófono con animación de grabación"""
    
    def __init__(self, master, on_record_start=None, on_record_stop=None, **kwargs):
        self.is_recording = False
        self.on_record_start = on_record_start
        self.on_record_stop = on_record_stop
        self.pulse_job = None
        self.pulse_state = 0
        
        super().__init__(
            master,
            text="🎤",
            width=80,
            height=80,
            corner_radius=40,
            font=("Segoe UI Emoji", 32),
            fg_color=CyberpunkTheme.BG_CARD,
            hover_color=CyberpunkTheme.NEON_GREEN,
            border_width=3,
            border_color=CyberpunkTheme.NEON_GREEN,
            **kwargs
        )
        
        # Eventos de mouse
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
    
    def _on_press(self, event):
        if not self.is_recording:
            self.is_recording = True
            self.configure(
                fg_color=CyberpunkTheme.NEON_PINK,
                border_color=CyberpunkTheme.NEON_PINK,
                text="🔴"
            )
            self._start_pulse()
            if self.on_record_start:
                self.on_record_start()
    
    def _on_release(self, event):
        if self.is_recording:
            self.is_recording = False
            self._stop_pulse()
            self.configure(
                fg_color=CyberpunkTheme.BG_CARD,
                border_color=CyberpunkTheme.NEON_GREEN,
                text="🎤"
            )
            if self.on_record_stop:
                self.on_record_stop()
    
    def _start_pulse(self):
        self._pulse()
    
    def _pulse(self):
        if not self.is_recording:
            return
        
        # Alternar entre colores
        colors = [CyberpunkTheme.NEON_PINK, CyberpunkTheme.NEON_ORANGE]
        self.pulse_state = (self.pulse_state + 1) % len(colors)
        self.configure(border_color=colors[self.pulse_state])
        
        self.pulse_job = self.after(300, self._pulse)
    
    def _stop_pulse(self):
        if self.pulse_job:
            self.after_cancel(self.pulse_job)
            self.pulse_job = None


# =============================================================================
# 📊 PANEL DE LOGS
# =============================================================================

class LogPanel(ctk.CTkFrame):
    """Panel de logs con colores por nivel"""
    
    def __init__(self, master, **kwargs):
        super().__init__(master, fg_color=CyberpunkTheme.BG_DARK, **kwargs)
        
        # Header
        header = ctk.CTkLabel(
            self,
            text="📋 LOGS",
            font=(CyberpunkTheme.FONT_MAIN[0], 14, "bold"),
            text_color=CyberpunkTheme.TEXT_SECONDARY
        )
        header.pack(anchor="w", padx=10, pady=(10, 5))
        
        # Área de texto
        self.text = ctk.CTkTextbox(
            self,
            font=(CyberpunkTheme.FONT_MONO[0], 11),
            fg_color=CyberpunkTheme.BG_MEDIUM,
            text_color=CyberpunkTheme.TEXT_PRIMARY,
            wrap="word",
            state="disabled"
        )
        self.text.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        
        # Configurar tags de colores
        self.text._textbox.tag_config("info", foreground=CyberpunkTheme.NEON_BLUE)
        self.text._textbox.tag_config("success", foreground=CyberpunkTheme.SUCCESS)
        self.text._textbox.tag_config("error", foreground=CyberpunkTheme.ERROR)
        self.text._textbox.tag_config("warning", foreground=CyberpunkTheme.WARNING)
        self.text._textbox.tag_config("timestamp", foreground=CyberpunkTheme.TEXT_MUTED)
        
        # Escuchar logs
        log_queue.on_log(self._check_logs)
    
    def _check_logs(self):
        self.after(10, self._process_logs)
    
    def _process_logs(self):
        items = log_queue.get_all()
        for timestamp, level, message in items:
            self.add_log(timestamp, level, message)
    
    def add_log(self, timestamp: str, level: str, message: str):
        self.text.configure(state="normal")
        
        # Insertar timestamp
        self.text._textbox.insert("end", f"[{timestamp}] ", "timestamp")
        
        # Insertar mensaje con color
        self.text._textbox.insert("end", f"{message}\n", level)
        
        # Auto-scroll
        self.text._textbox.see("end")
        self.text.configure(state="disabled")
    
    def clear(self):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")


# =============================================================================
# 📑 PANEL DE PESTAÑAS
# =============================================================================

class TabsPanel(ctk.CTkFrame):
    """Panel que muestra las pestañas del navegador"""
    
    def __init__(self, master, **kwargs):
        super().__init__(master, fg_color=CyberpunkTheme.BG_DARK, **kwargs)
        
        # Header
        header_frame = ctk.CTkFrame(self, fg_color="transparent")
        header_frame.pack(fill="x", padx=10, pady=(10, 5))
        
        ctk.CTkLabel(
            header_frame,
            text="📑 PESTAÑAS",
            font=(CyberpunkTheme.FONT_MAIN[0], 14, "bold"),
            text_color=CyberpunkTheme.TEXT_SECONDARY
        ).pack(side="left")
        
        # Botón nueva pestaña
        ctk.CTkButton(
            header_frame,
            text="+",
            width=30,
            height=30,
            fg_color=CyberpunkTheme.BG_CARD,
            hover_color=CyberpunkTheme.NEON_GREEN,
            font=(CyberpunkTheme.FONT_MAIN[0], 16, "bold"),
            command=self._on_new_tab
        ).pack(side="right")
        
        # Lista de pestañas
        self.tabs_frame = ctk.CTkScrollableFrame(
            self,
            fg_color=CyberpunkTheme.BG_MEDIUM,
            corner_radius=10
        )
        self.tabs_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        
        self.tabs = []
        self.on_new_tab = None
        self.on_select_tab = None
    
    def _on_new_tab(self):
        if self.on_new_tab:
            self.on_new_tab()
    
    def update_tabs(self, tabs_data: list):
        """Actualiza la lista de pestañas"""
        # Limpiar pestañas anteriores
        for widget in self.tabs_frame.winfo_children():
            widget.destroy()
        
        self.tabs = []
        
        for i, (name, title, is_active) in enumerate(tabs_data):
            tab_frame = ctk.CTkFrame(
                self.tabs_frame,
                fg_color=CyberpunkTheme.NEON_GREEN if is_active else CyberpunkTheme.BG_CARD,
                corner_radius=8,
                height=40
            )
            tab_frame.pack(fill="x", pady=2)
            tab_frame.pack_propagate(False)
            
            # Icono de estado
            icon = "→" if is_active else " "
            ctk.CTkLabel(
                tab_frame,
                text=icon,
                font=(CyberpunkTheme.FONT_MONO[0], 12),
                text_color=CyberpunkTheme.BG_DARK if is_active else CyberpunkTheme.NEON_GREEN,
                width=20
            ).pack(side="left", padx=(10, 0))
            
            # Nombre de la pestaña
            ctk.CTkLabel(
                tab_frame,
                text=f"[{name}] {title[:25]}...",
                font=(CyberpunkTheme.FONT_MAIN[0], 12),
                text_color=CyberpunkTheme.BG_DARK if is_active else CyberpunkTheme.TEXT_PRIMARY,
                anchor="w"
            ).pack(side="left", fill="x", expand=True, padx=5)
            
            self.tabs.append(tab_frame)


# =============================================================================
# 🎯 PANEL DE ENTRADA
# =============================================================================

class InputPanel(ctk.CTkFrame):
    """Panel principal de entrada de comandos"""
    
    def __init__(self, master, on_submit=None, on_voice_start=None, on_voice_stop=None, **kwargs):
        super().__init__(master, fg_color=CyberpunkTheme.BG_LIGHT, corner_radius=15, **kwargs)
        
        self.on_submit = on_submit
        
        # Título
        title_frame = ctk.CTkFrame(self, fg_color="transparent")
        title_frame.pack(fill="x", padx=20, pady=(20, 10))
        
        ctk.CTkLabel(
            title_frame,
            text="¿QUÉ QUIERES HACER?",
            font=(CyberpunkTheme.FONT_MAIN[0], 18, "bold"),
            text_color=CyberpunkTheme.TEXT_PRIMARY
        ).pack(side="left")
        
        # Estado de conexión
        self.status_label = ctk.CTkLabel(
            title_frame,
            text="● Desconectado",
            font=(CyberpunkTheme.FONT_MAIN[0], 12),
            text_color=CyberpunkTheme.TEXT_MUTED
        )
        self.status_label.pack(side="right")
        
        # Campo de entrada
        input_frame = ctk.CTkFrame(self, fg_color="transparent")
        input_frame.pack(fill="x", padx=20, pady=10)
        
        self.entry = ctk.CTkEntry(
            input_frame,
            placeholder_text="Escribe un comando o mantén presionado el micrófono...",
            font=(CyberpunkTheme.FONT_MAIN[0], 14),
            height=50,
            fg_color=CyberpunkTheme.BG_MEDIUM,
            border_color=CyberpunkTheme.NEON_BLUE,
            border_width=2,
            text_color=CyberpunkTheme.TEXT_PRIMARY
        )
        self.entry.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.entry.bind("<Return>", self._on_enter)
        
        # Botón enviar
        self.send_btn = ctk.CTkButton(
            input_frame,
            text="▶",
            width=50,
            height=50,
            font=(CyberpunkTheme.FONT_MAIN[0], 20),
            fg_color=CyberpunkTheme.NEON_BLUE,
            hover_color=CyberpunkTheme.NEON_GREEN,
            corner_radius=10,
            command=self._submit
        )
        self.send_btn.pack(side="left")
        
        # Frame para micrófono
        mic_frame = ctk.CTkFrame(self, fg_color="transparent")
        mic_frame.pack(pady=(10, 20))
        
        # Botón de micrófono
        self.mic_btn = MicrophoneButton(
            mic_frame,
            on_record_start=on_voice_start,
            on_record_stop=on_voice_stop
        )
        self.mic_btn.pack()
        
        # Instrucción
        ctk.CTkLabel(
            mic_frame,
            text="Mantén presionado para hablar",
            font=(CyberpunkTheme.FONT_MAIN[0], 11),
            text_color=CyberpunkTheme.TEXT_MUTED
        ).pack(pady=(5, 0))
        
        # Comandos rápidos
        quick_frame = ctk.CTkFrame(self, fg_color="transparent")
        quick_frame.pack(fill="x", padx=20, pady=(0, 20))
        
        commands = [
            ("📸 Screenshot", "screenshot"),
            ("📑 Nueva Tab", "nueva"),
            ("🔓 Hackear", "hackear"),
            ("❓ Ayuda", "ayuda"),
        ]
        
        for text, cmd in commands:
            btn = ctk.CTkButton(
                quick_frame,
                text=text,
                font=(CyberpunkTheme.FONT_MAIN[0], 11),
                fg_color=CyberpunkTheme.BG_CARD,
                hover_color=CyberpunkTheme.BG_LIGHT,
                height=30,
                corner_radius=15,
                command=lambda c=cmd: self._quick_command(c)
            )
            btn.pack(side="left", padx=5)
    
    def _on_enter(self, event):
        self._submit()
    
    def _submit(self):
        text = self.entry.get().strip()
        if text and self.on_submit:
            self.on_submit(text)
            self.entry.delete(0, "end")
    
    def _quick_command(self, cmd: str):
        if self.on_submit:
            self.on_submit(cmd)
    
    def set_status(self, status: str, connected: bool = False):
        color = CyberpunkTheme.SUCCESS if connected else CyberpunkTheme.TEXT_MUTED
        self.status_label.configure(text=f"● {status}", text_color=color)
    
    def set_transcription(self, text: str):
        """Pone el texto transcrito en el campo de entrada"""
        self.entry.delete(0, "end")
        self.entry.insert(0, text)


# =============================================================================
# 🚀 VENTANA PRINCIPAL
# =============================================================================

class BabasGUI(ctk.CTk):
    """Ventana principal de BABAS"""
    
    def __init__(self):
        super().__init__()
        
        # Configuración de ventana
        self.title("🤖 BABAS - Asistente de Automatización")
        self.geometry("1200x800")
        self.configure(fg_color=CyberpunkTheme.BG_DARK)
        
        # Centrar ventana
        self._center_window()
        
        # Callbacks
        self.on_command = None
        self.on_voice_start = None
        self.on_voice_stop = None
        
        # Crear UI
        self._create_header()
        self._create_main_content()
        
        # Log inicial
        log("BABAS GUI iniciada", "success")
        log("Esperando conexión al navegador...", "info")
    
    def _center_window(self):
        self.update_idletasks()
        width = 1200
        height = 800
        x = (self.winfo_screenwidth() // 2) - (width // 2)
        y = (self.winfo_screenheight() // 2) - (height // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")
    
    def _create_header(self):
        """Crea el header con logo y título"""
        header = ctk.CTkFrame(self, fg_color=CyberpunkTheme.BG_MEDIUM, height=70)
        header.pack(fill="x")
        header.pack_propagate(False)
        
        # Logo
        logo_frame = ctk.CTkFrame(header, fg_color="transparent")
        logo_frame.pack(side="left", padx=20)
        
        ctk.CTkLabel(
            logo_frame,
            text="🤖",
            font=("Segoe UI Emoji", 36)
        ).pack(side="left")
        
        title_frame = ctk.CTkFrame(logo_frame, fg_color="transparent")
        title_frame.pack(side="left", padx=10)
        
        ctk.CTkLabel(
            title_frame,
            text="BABAS",
            font=(CyberpunkTheme.FONT_MAIN[0], 24, "bold"),
            text_color=CyberpunkTheme.NEON_GREEN
        ).pack(anchor="w")
        
        ctk.CTkLabel(
            title_frame,
            text="Browser Automation Bot Assistant System",
            font=(CyberpunkTheme.FONT_MAIN[0], 11),
            text_color=CyberpunkTheme.TEXT_SECONDARY
        ).pack(anchor="w")
        
        # Controles de navegador
        browser_frame = ctk.CTkFrame(header, fg_color="transparent")
        browser_frame.pack(side="right", padx=20)
        
        self.browser_btn = ctk.CTkButton(
            browser_frame,
            text="🌐 Conectar Navegador",
            font=(CyberpunkTheme.FONT_MAIN[0], 12),
            fg_color=CyberpunkTheme.NEON_BLUE,
            hover_color=CyberpunkTheme.NEON_GREEN,
            height=35,
            command=self._on_connect_browser
        )
        self.browser_btn.pack(side="left", padx=5)
        
        # Botón de voz del mayordomo
        self.butler_btn = ctk.CTkButton(
            browser_frame,
            text="🔊",
            width=35,
            height=35,
            font=("Segoe UI Emoji", 16),
            fg_color=CyberpunkTheme.BG_CARD,
            hover_color=CyberpunkTheme.NEON_PURPLE,
            command=self._on_toggle_butler
        )
        self.butler_btn.pack(side="left", padx=5)
    
    def _create_main_content(self):
        """Crea el contenido principal"""
        # Frame principal
        main = ctk.CTkFrame(self, fg_color="transparent")
        main.pack(fill="both", expand=True, padx=20, pady=20)
        
        # Columna izquierda (pestañas)
        left_col = ctk.CTkFrame(main, fg_color="transparent", width=250)
        left_col.pack(side="left", fill="y", padx=(0, 10))
        left_col.pack_propagate(False)
        
        self.tabs_panel = TabsPanel(left_col)
        self.tabs_panel.pack(fill="both", expand=True)
        self.tabs_panel.on_new_tab = lambda: self._send_command("nueva")
        
        # Columna central (entrada)
        center_col = ctk.CTkFrame(main, fg_color="transparent")
        center_col.pack(side="left", fill="both", expand=True, padx=10)
        
        self.input_panel = InputPanel(
            center_col,
            on_submit=self._on_submit,
            on_voice_start=self._on_voice_start,
            on_voice_stop=self._on_voice_stop
        )
        self.input_panel.pack(fill="x")
        
        # Historial de comandos
        history_frame = ctk.CTkFrame(center_col, fg_color=CyberpunkTheme.BG_LIGHT, corner_radius=15)
        history_frame.pack(fill="both", expand=True, pady=(20, 0))
        
        ctk.CTkLabel(
            history_frame,
            text="📜 HISTORIAL",
            font=(CyberpunkTheme.FONT_MAIN[0], 14, "bold"),
            text_color=CyberpunkTheme.TEXT_SECONDARY
        ).pack(anchor="w", padx=15, pady=(15, 5))
        
        self.history_text = ctk.CTkTextbox(
            history_frame,
            font=(CyberpunkTheme.FONT_MONO[0], 12),
            fg_color=CyberpunkTheme.BG_MEDIUM,
            text_color=CyberpunkTheme.TEXT_PRIMARY,
            wrap="word",
            state="disabled"
        )
        self.history_text.pack(fill="both", expand=True, padx=15, pady=(0, 15))
        
        # Columna derecha (logs)
        right_col = ctk.CTkFrame(main, fg_color="transparent", width=350)
        right_col.pack(side="right", fill="y", padx=(10, 0))
        right_col.pack_propagate(False)
        
        self.log_panel = LogPanel(right_col)
        self.log_panel.pack(fill="both", expand=True)
    
    def _on_connect_browser(self):
        log("Intentando conectar al navegador...", "info")
        if self.on_command:
            self.on_command("__connect_browser__")
    
    def _on_toggle_butler(self):
        log("Toggle mayordomo", "info")
        if self.on_command:
            self.on_command("voz")
    
    def _on_submit(self, text: str):
        self._send_command(text)
    
    def _send_command(self, text: str):
        # Agregar al historial
        self._add_to_history(f">>> {text}")
        log(f"Comando: {text}", "info")
        
        if self.on_command:
            self.on_command(text)
    
    def _on_voice_start(self):
        log("🎤 Grabando...", "info")
        if self.on_voice_start:
            self.on_voice_start()
    
    def _on_voice_stop(self):
        log("🔄 Procesando audio...", "info")
        if self.on_voice_stop:
            self.on_voice_stop()
    
    def _add_to_history(self, text: str):
        self.history_text.configure(state="normal")
        self.history_text.insert("end", f"{text}\n")
        self.history_text.see("end")
        self.history_text.configure(state="disabled")
    
    # === Métodos públicos para actualizar UI ===
    
    def set_status(self, status: str, connected: bool = False):
        """Actualiza el estado de conexión"""
        self.input_panel.set_status(status, connected)
        if connected:
            self.browser_btn.configure(
                text="✓ Conectado",
                fg_color=CyberpunkTheme.SUCCESS
            )
        else:
            self.browser_btn.configure(
                text="🌐 Conectar Navegador",
                fg_color=CyberpunkTheme.NEON_BLUE
            )
    
    def update_tabs(self, tabs_data: list):
        """Actualiza la lista de pestañas"""
        self.tabs_panel.update_tabs(tabs_data)
    
    def add_result(self, text: str, success: bool = True):
        """Agrega un resultado al historial"""
        prefix = "✅" if success else "❌"
        self._add_to_history(f"{prefix} {text}")
    
    def set_transcription(self, text: str):
        """Pone texto transcrito en el campo de entrada"""
        self.input_panel.set_transcription(text)


# =============================================================================
# 🔗 INTEGRACIÓN CON BABAS
# =============================================================================

class BabasController:
    """Controlador que conecta la GUI con el sistema BABAS"""
    
    def __init__(self, gui: BabasGUI):
        self.gui = gui
        self.browser = None
        self.context = None
        self.page = None
        self.executor = None
        self.tab_manager = None
        self.butler = None
        self.voice_input = None
        self.is_recording = False
        
        # Conectar callbacks
        gui.on_command = self.handle_command
        gui.on_voice_start = self.start_recording
        gui.on_voice_stop = self.stop_recording
        
        # Thread para operaciones async
        self.loop = None
        self.thread = None
    
    def start_async_loop(self):
        """Inicia el loop asyncio en un thread separado"""
        self.loop = asyncio.new_event_loop()
        
        def run():
            asyncio.set_event_loop(self.loop)
            self.loop.run_forever()
        
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
    
    def run_async(self, coro):
        """Ejecuta una corutina en el loop async"""
        if self.loop:
            future = asyncio.run_coroutine_threadsafe(coro, self.loop)
            return future
    
    def handle_command(self, command: str):
        """Maneja comandos desde la GUI"""
        log(f"Procesando: {command}", "info")
        
        # Comandos especiales
        if command == "__connect_browser__":
            self._connect_browser()
            return
        
        if command.lower() in ["salir", "exit", "q"]:
            log("¡Hasta luego!", "info")
            self.gui.quit()
            return
        
        if command.lower() in ["voz", "voice"]:
            self._toggle_butler()
            return
        
        # Ejecutar comando normal
        self._execute_command(command)
    
    def _connect_browser(self):
        """Conecta al navegador"""
        log("Buscando navegadores disponibles...", "info")
        
        # TODO: Integrar con select_session_mode de babas_text
        # Por ahora, simular
        self.gui.after(1000, lambda: self._on_connected())
    
    def _on_connected(self):
        self.gui.set_status("Brave (Puerto 9222)", connected=True)
        log("Conectado a Brave", "success")
        
        # Actualizar pestañas (demo)
        self.gui.update_tabs([
            ("main", "Nueva pestaña", True),
        ])
    
    def _toggle_butler(self):
        if self.butler:
            # self.butler.toggle_mute()
            log("Mayordomo silenciado/activado", "info")
        else:
            log("Mayordomo no disponible", "warning")
    
    def _execute_command(self, command: str):
        """Ejecuta un comando de automatización"""
        log(f"Ejecutando: {command}...", "info")
        
        # TODO: Integrar con create_plan y run_plan_with_recovery
        # Por ahora, simular
        def simulate():
            time.sleep(1)
            self.gui.after(0, lambda: log("Plan completado", "success"))
            self.gui.after(0, lambda: self.gui.add_result(command, True))
        
        threading.Thread(target=simulate, daemon=True).start()
    
    def start_recording(self):
        """Inicia grabación de voz"""
        self.is_recording = True
        log("🔴 Grabando...", "info")
        
        # TODO: Integrar con VoiceInputMode
    
    def stop_recording(self):
        """Detiene grabación y transcribe"""
        self.is_recording = False
        log("🔄 Transcribiendo...", "info")
        
        # TODO: Integrar con VoiceTranscriber
        # Por ahora, simular
        def simulate():
            time.sleep(1)
            text = "Comando de voz simulado"
            self.gui.after(0, lambda: self.gui.set_transcription(text))
            self.gui.after(0, lambda: log(f"Transcrito: {text}", "success"))
        
        threading.Thread(target=simulate, daemon=True).start()


# =============================================================================
# 🏁 MAIN
# =============================================================================

def main():
    """Punto de entrada de la GUI"""
    print("[*] Iniciando BABAS GUI...")
    
    # Crear ventana
    app = BabasGUI()
    
    # Crear controlador
    controller = BabasController(app)
    controller.start_async_loop()
    
    # Ejecutar
    app.mainloop()


if __name__ == "__main__":
    main()

