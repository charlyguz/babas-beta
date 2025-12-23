"""
🖥️ BABAS GUI - Interfaz Simple
Una interfaz minimalista para el asistente de automatización
"""

import customtkinter as ctk
import threading
import queue
import sys
import os
from datetime import datetime
from typing import Optional, Callable
import time

# Configurar tema
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")


# =============================================================================
# 🎨 COLORES
# =============================================================================

class Colors:
    BG = "#1a1a2e"
    BG_DARK = "#0f0f1a"
    CARD = "#16213e"
    ACCENT = "#00d4ff"
    ACCENT_GREEN = "#00ff88"
    ACCENT_RED = "#ff4757"
    TEXT = "#ffffff"
    TEXT_DIM = "#888888"


# =============================================================================
# 📝 LOGS
# =============================================================================

log_queue = queue.Queue()

def log(msg: str, level: str = "info"):
    """Log global"""
    timestamp = datetime.now().strftime("%H:%M:%S")
    log_queue.put((timestamp, level, msg))


# =============================================================================
# 🎤 BOTÓN MICRÓFONO
# =============================================================================

class MicButton(ctk.CTkButton):
    """Botón de micrófono simple"""
    
    def __init__(self, master, on_start=None, on_stop=None, **kwargs):
        self.recording = False
        self.on_start = on_start
        self.on_stop = on_stop
        
        super().__init__(
            master,
            text="🎤",
            width=70,
            height=70,
            corner_radius=35,
            font=("Segoe UI Emoji", 28),
            fg_color=Colors.CARD,
            hover_color=Colors.ACCENT,
            border_width=2,
            border_color=Colors.ACCENT,
            command=None,
            **kwargs
        )
        
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)
    
    def _press(self, e):
        if not self.recording:
            self.recording = True
            self.configure(fg_color=Colors.ACCENT_RED, text="🔴", border_color=Colors.ACCENT_RED)
            if self.on_start:
                self.on_start()
    
    def _release(self, e):
        if self.recording:
            self.recording = False
            self.configure(fg_color=Colors.CARD, text="🎤", border_color=Colors.ACCENT)
            if self.on_stop:
                self.on_stop()


# =============================================================================
# 🚀 APP PRINCIPAL
# =============================================================================

class BabasApp(ctk.CTk):
    """Interfaz simple de BABAS"""
    
    def __init__(self):
        super().__init__()
        
        self.title("BABAS")
        self.geometry("600x500")
        self.configure(fg_color=Colors.BG_DARK)
        self.resizable(True, True)
        
        # Callbacks
        self.on_command = None
        self.on_voice_start = None
        self.on_voice_stop = None
        
        self._build_ui()
        self._start_log_updater()
        
        log("BABAS listo", "success")
    
    def _build_ui(self):
        """Construye la interfaz"""
        
        # === HEADER ===
        header = ctk.CTkFrame(self, fg_color=Colors.BG, height=60)
        header.pack(fill="x")
        header.pack_propagate(False)
        
        ctk.CTkLabel(
            header,
            text="🤖 BABAS",
            font=("Segoe UI", 20, "bold"),
            text_color=Colors.ACCENT
        ).pack(side="left", padx=20, pady=15)
        
        self.status = ctk.CTkLabel(
            header,
            text="⚪ Sin conectar",
            font=("Segoe UI", 12),
            text_color=Colors.TEXT_DIM
        )
        self.status.pack(side="right", padx=20)
        
        # === CONTENIDO ===
        content = ctk.CTkFrame(self, fg_color="transparent")
        content.pack(fill="both", expand=True, padx=20, pady=20)
        
        # Input frame
        input_frame = ctk.CTkFrame(content, fg_color=Colors.BG, corner_radius=15)
        input_frame.pack(fill="x")
        
        ctk.CTkLabel(
            input_frame,
            text="¿Qué quieres hacer?",
            font=("Segoe UI", 14),
            text_color=Colors.TEXT_DIM
        ).pack(anchor="w", padx=15, pady=(15, 5))
        
        # Entry + Send
        entry_row = ctk.CTkFrame(input_frame, fg_color="transparent")
        entry_row.pack(fill="x", padx=15, pady=(0, 15))
        
        self.entry = ctk.CTkEntry(
            entry_row,
            placeholder_text="Escribe aquí o usa el micrófono...",
            font=("Segoe UI", 14),
            height=45,
            fg_color=Colors.BG_DARK,
            border_color=Colors.ACCENT,
            border_width=1
        )
        self.entry.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.entry.bind("<Return>", lambda e: self._send())
        
        ctk.CTkButton(
            entry_row,
            text="➤",
            width=45,
            height=45,
            font=("Segoe UI", 18),
            fg_color=Colors.ACCENT,
            hover_color=Colors.ACCENT_GREEN,
            command=self._send
        ).pack(side="right")
        
        # Micrófono centrado
        mic_frame = ctk.CTkFrame(content, fg_color="transparent")
        mic_frame.pack(pady=20)
        
        self.mic = MicButton(mic_frame, on_start=self._voice_start, on_stop=self._voice_stop)
        self.mic.pack()
        
        ctk.CTkLabel(
            mic_frame,
            text="Mantén presionado",
            font=("Segoe UI", 11),
            text_color=Colors.TEXT_DIM
        ).pack(pady=(5, 0))
        
        # Logs
        log_frame = ctk.CTkFrame(content, fg_color=Colors.BG, corner_radius=15)
        log_frame.pack(fill="both", expand=True, pady=(10, 0))
        
        ctk.CTkLabel(
            log_frame,
            text="Actividad",
            font=("Segoe UI", 12),
            text_color=Colors.TEXT_DIM
        ).pack(anchor="w", padx=15, pady=(10, 5))
        
        self.logs = ctk.CTkTextbox(
            log_frame,
            font=("Consolas", 11),
            fg_color=Colors.BG_DARK,
            text_color=Colors.TEXT,
            height=150
        )
        self.logs.pack(fill="both", expand=True, padx=15, pady=(0, 15))
        self.logs.configure(state="disabled")
        
        # Tags de colores
        self.logs._textbox.tag_config("time", foreground=Colors.TEXT_DIM)
        self.logs._textbox.tag_config("info", foreground=Colors.ACCENT)
        self.logs._textbox.tag_config("success", foreground=Colors.ACCENT_GREEN)
        self.logs._textbox.tag_config("error", foreground=Colors.ACCENT_RED)
    
    def _start_log_updater(self):
        """Actualiza logs cada 100ms"""
        def update():
            while not log_queue.empty():
                try:
                    ts, level, msg = log_queue.get_nowait()
                    self._add_log(ts, level, msg)
                except:
                    break
            self.after(100, update)
        self.after(100, update)
    
    def _add_log(self, ts: str, level: str, msg: str):
        self.logs.configure(state="normal")
        self.logs._textbox.insert("end", f"[{ts}] ", "time")
        self.logs._textbox.insert("end", f"{msg}\n", level)
        self.logs._textbox.see("end")
        self.logs.configure(state="disabled")
    
    def _send(self):
        text = self.entry.get().strip()
        if text:
            log(f">>> {text}", "info")
            self.entry.delete(0, "end")
            if self.on_command:
                self.on_command(text)
    
    def _voice_start(self):
        log("🎤 Grabando...", "info")
        if self.on_voice_start:
            self.on_voice_start()
    
    def _voice_stop(self):
        log("Procesando...", "info")
        if self.on_voice_stop:
            self.on_voice_stop()
    
    # === API Pública ===
    
    def set_status(self, text: str, connected: bool = False):
        icon = "🟢" if connected else "⚪"
        color = Colors.ACCENT_GREEN if connected else Colors.TEXT_DIM
        self.status.configure(text=f"{icon} {text}", text_color=color)
    
    def set_transcription(self, text: str):
        self.entry.delete(0, "end")
        self.entry.insert(0, text)
    
    def add_result(self, text: str, success: bool = True):
        level = "success" if success else "error"
        log(text, level)


# =============================================================================
# 🏁 MAIN
# =============================================================================

def main():
    print("[*] Iniciando BABAS GUI...")
    
    app = BabasApp()
    
    # Demo: comandos básicos
    def handle_cmd(cmd):
        if cmd.lower() in ["salir", "exit", "q"]:
            app.quit()
        elif cmd.lower() in ["conectar", "connect"]:
            log("Conectando al navegador...", "info")
            app.after(1000, lambda: app.set_status("Brave", True))
            app.after(1000, lambda: log("Conectado a Brave", "success"))
        else:
            log(f"Ejecutando: {cmd}", "info")
            app.after(500, lambda: log("Completado", "success"))
    
    app.on_command = handle_cmd
    app.mainloop()


if __name__ == "__main__":
    main()
