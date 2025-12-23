import os
import json
import re
from typing import List, Literal, Optional, Dict, Callable, Any
from functools import wraps
from datetime import datetime
from dotenv import load_dotenv
from pydantic import BaseModel
from openai import OpenAI
from playwright.sync_api import sync_playwright, Page, BrowserContext

# ---------- ENV ----------
print("🔧 Cargando variables de entorno...")
load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
print("✅ Cliente OpenAI inicializado")

# ---------- MAYORDOMO (VOZ) ----------
try:
    from babas_voice import init_butler, shutdown_butler, get_butler
    VOICE_AVAILABLE = True
except ImportError:
    VOICE_AVAILABLE = False
    def init_butler(): return None
    def shutdown_butler(): pass
    def get_butler(): return None

MODEL = "gpt-4o-mini"
MAX_RETRIES = 3  # Intentos de auto-recuperación

# =============================================================================
# CAPTURADOR DE CONTEXTO MEJORADO
# =============================================================================

class PageAnalyzer:
    """Analiza la página y extrae información útil para la IA"""
    
    @staticmethod
    def get_interactive_elements(page: Page, max_per_type: int = 15) -> Dict:
        """Extrae elementos interactivos de la página"""
        elements = {
            "inputs": [],
            "buttons": [],
            "links": [],
            "search_boxes": []
        }
        
        try:
            # Inputs y textareas
            for inp in page.locator("input:visible, textarea:visible").all()[:max_per_type]:
                try:
                    info = {
                        "type": inp.get_attribute("type") or "text",
                        "name": inp.get_attribute("name"),
                        "id": inp.get_attribute("id"),
                        "placeholder": inp.get_attribute("placeholder"),
                        "aria-label": inp.get_attribute("aria-label"),
                        "class": inp.get_attribute("class"),
                    }
                    # Crear selector sugerido
                    if info["id"]:
                        info["selector"] = f"#{info['id']}"
                    elif info["name"]:
                        info["selector"] = f"input[name='{info['name']}']"
                    elif info["placeholder"]:
                        info["selector"] = f"input[placeholder='{info['placeholder']}']"
                    elif info["aria-label"]:
                        info["selector"] = f"input[aria-label='{info['aria-label']}']"
                    else:
                        info["selector"] = None
                    
                    elements["inputs"].append({k: v for k, v in info.items() if v})
                except:
                    continue
            
            # Buscar cajas de búsqueda específicamente
            search_selectors = [
                "input[type='search']",
                "input[name*='search']",
                "input[name*='query']",
                "input[placeholder*='Search']",
                "input[placeholder*='Buscar']",
                "input[aria-label*='Search']",
                "input[aria-label*='Buscar']",
                "[role='searchbox']",
                "[role='combobox']"
            ]
            for sel in search_selectors:
                try:
                    elem = page.locator(sel).first
                    if elem.is_visible(timeout=500):
                        elements["search_boxes"].append({
                            "selector": sel,
                            "placeholder": elem.get_attribute("placeholder"),
                            "aria-label": elem.get_attribute("aria-label")
                        })
                except:
                    continue
            
            # Botones
            for btn in page.locator("button:visible, [role='button']:visible").all()[:max_per_type]:
                try:
                    text = btn.inner_text().strip()[:50]
                    if text:
                        elements["buttons"].append({
                            "text": text,
                            "selector": f"button:has-text('{text[:20]}')" if text else None
                        })
                except:
                    continue
            
            # Links importantes
            for link in page.locator("a:visible").all()[:max_per_type]:
                try:
                    text = link.inner_text().strip()[:50]
                    href = link.get_attribute("href")
                    if text and href and not href.startswith("#"):
                        elements["links"].append({
                            "text": text,
                            "href": href[:100]
                        })
                except:
                    continue
                    
        except Exception as e:
            elements["error"] = str(e)
        
        return elements
    
    @staticmethod
    def get_page_summary(page: Page) -> Dict:
        """Obtiene un resumen completo de la página"""
        try:
            summary = {
                "url": page.url,
                "title": page.title(),
                "elements": PageAnalyzer.get_interactive_elements(page)
            }
            return summary
        except Exception as e:
            return {"error": str(e), "url": "unknown"}

# =============================================================================
# SISTEMA DE REGISTRO DE ACCIONES
# =============================================================================

class ActionRegistry:
    """Registro centralizado de acciones"""
    _actions: Dict[str, Callable] = {}
    _descriptions: Dict[str, str] = {}
    
    @classmethod
    def register(cls, action_name: str, description: str = ""):
        def decorator(func: Callable):
            @wraps(func)
            def wrapper(*args, **kwargs):
                return func(*args, **kwargs)
            cls._actions[action_name] = wrapper
            cls._descriptions[action_name] = description
            return wrapper
        return decorator
    
    @classmethod
    def get(cls, action_name: str) -> Optional[Callable]:
        return cls._actions.get(action_name)
    
    @classmethod
    def get_all_descriptions(cls) -> str:
        """Retorna todas las acciones disponibles formateadas"""
        lines = []
        for name, desc in cls._descriptions.items():
            lines.append(f"- {name}: {desc}")
        return "\n".join(lines)

# =============================================================================
# MANEJADOR DE PESTAÑAS
# =============================================================================

class TabManager:
    """Gestiona múltiples pestañas del navegador"""
    
    def __init__(self, context: BrowserContext):
        self.context = context
        self.tabs: Dict[str, Page] = {}
        self._tab_counter = 0
    
    def create_tab(self, name: str = None) -> tuple[str, Page]:
        self._tab_counter += 1
        tab_name = name or f"tab_{self._tab_counter}"
        page = self.context.new_page()
        self.tabs[tab_name] = page
        print(f"   📑 Nueva pestaña: '{tab_name}'")
        return tab_name, page
    
    def get_tab(self, name: str) -> Optional[Page]:
        return self.tabs.get(name)
    
    def close_tab(self, name: str) -> bool:
        if name in self.tabs and name != "main":
            self.tabs[name].close()
            del self.tabs[name]
            print(f"   🗑️ Pestaña cerrada: '{name}'")
            return True
        return False
    
    def list_tabs(self) -> List[str]:
        return list(self.tabs.keys())

# =============================================================================
# SCHEMAS
# =============================================================================

ActionType = Literal[
    "goto", "wait", "click", "type_text", "press_key", "screenshot", "scroll",
    "smart_search", "smart_click",  # Acciones inteligentes
    "new_tab", "switch_tab", "close_tab",
    "search_youtube", "click_first_video",
    "open_gmail", "compose_email", "send_email",
    "open_whatsapp", "search_contact", "send_whatsapp_message",
    "analyze_page"  # Nueva acción para obtener contexto
]

class Step(BaseModel):
    action: ActionType
    value: Optional[str] = None
    selector: Optional[str] = None
    ms: Optional[int] = None
    to: Optional[str] = None
    subject: Optional[str] = None
    body: Optional[str] = None
    tab_name: Optional[str] = None

class Plan(BaseModel):
    steps: List[Step]
    reasoning: Optional[str] = None

# =============================================================================
# EJECUTOR DE ACCIONES
# =============================================================================

class ActionExecutor:
    """Ejecuta acciones con soporte para auto-recuperación"""
    
    def __init__(self, tab_manager: TabManager, initial_page: Page):
        self.tab_manager = tab_manager
        self.current_page = initial_page
        self.current_tab_name = "main"
        self.tab_manager.tabs["main"] = initial_page
        self.last_error = None
        self.last_context = None
        self._register_all_actions()
    
    def _register_all_actions(self):
        """Registra todas las acciones"""
        
        # ========== BÁSICAS ==========
        @ActionRegistry.register("goto", "Navegar a una URL")
        def goto(executor, step):
            print(f"   🌐 Navegando a {step.value}...")
            executor.current_page.goto(step.value, wait_until="domcontentloaded", timeout=30000)
            executor.current_page.wait_for_timeout(2000)
            print(f"   ✅ Página cargada")
        
        @ActionRegistry.register("wait", "Esperar X milisegundos")
        def wait(executor, step):
            ms = step.ms or 2000
            print(f"   ⏳ Esperando {ms}ms...")
            executor.current_page.wait_for_timeout(ms)
        
        @ActionRegistry.register("click", "Click en elemento por selector CSS")
        def click(executor, step):
            print(f"   👆 Click en: {step.selector}")
            executor.current_page.locator(step.selector).first.click(timeout=10000)
            print(f"   ✅ Click realizado")
        
        @ActionRegistry.register("type_text", "Escribir texto en un campo (requiere selector)")
        def type_text(executor, step):
            print(f"   ⌨️ Escribiendo: '{step.value}' en {step.selector}")
            executor.current_page.locator(step.selector).first.fill(step.value, timeout=10000)
            print(f"   ✅ Texto ingresado")
        
        @ActionRegistry.register("press_key", "Presionar una tecla (Enter, Tab, etc)")
        def press_key(executor, step):
            print(f"   ⌨️ Tecla: {step.value}")
            executor.current_page.keyboard.press(step.value)
        
        @ActionRegistry.register("screenshot", "Capturar pantalla")
        def screenshot(executor, step):
            filename = f"screenshot_{datetime.now().strftime('%H%M%S')}.png"
            executor.current_page.screenshot(path=filename)
            print(f"   📸 Guardado: {filename}")
        
        @ActionRegistry.register("scroll", "Hacer scroll (value: up/down)")
        def scroll(executor, step):
            amount = step.ms or 500
            direction = 1 if step.value == "down" else -1
            executor.current_page.mouse.wheel(0, amount * direction)
            print(f"   📜 Scroll {step.value}")
        
        # ========== ACCIONES INTELIGENTES ==========
        @ActionRegistry.register("smart_search", "Buscar automáticamente la caja de búsqueda y escribir")
        def smart_search(executor, step):
            print(f"   🔍 Buscando campo de búsqueda para: '{step.value}'")
            search_selectors = [
                "input[type='search']",
                "input[name*='search']", "input[name*='query']", "input[name*='q']",
                "input[placeholder*='Search']", "input[placeholder*='Buscar']",
                "input[aria-label*='Search']", "input[aria-label*='Buscar']",
                "[role='searchbox']", "[role='combobox']",
                "input[id*='search']", "input[class*='search']",
                "textarea[name*='search']"
            ]
            
            for selector in search_selectors:
                try:
                    elem = executor.current_page.locator(selector).first
                    if elem.is_visible(timeout=1000):
                        elem.click()
                        elem.fill(step.value)
                        elem.press("Enter")
                        print(f"   ✅ Búsqueda realizada con: {selector}")
                        return
                except:
                    continue
            
            raise RuntimeError("No se encontró campo de búsqueda")
        
        @ActionRegistry.register("smart_click", "Click inteligente por texto visible")
        def smart_click(executor, step):
            print(f"   👆 Buscando elemento con texto: '{step.value}'")
            # Intentar diferentes estrategias
            strategies = [
                f"text='{step.value}'",
                f"button:has-text('{step.value}')",
                f"a:has-text('{step.value}')",
                f"[role='button']:has-text('{step.value}')",
                f"*:has-text('{step.value}')"
            ]
            
            for strategy in strategies:
                try:
                    elem = executor.current_page.locator(strategy).first
                    if elem.is_visible(timeout=1000):
                        elem.click()
                        print(f"   ✅ Click realizado con: {strategy}")
                        return
                except:
                    continue
            
            raise RuntimeError(f"No se encontró elemento con texto: {step.value}")
        
        @ActionRegistry.register("analyze_page", "Analizar la página actual y obtener elementos")
        def analyze_page(executor, step):
            print(f"   🔬 Analizando página...")
            summary = PageAnalyzer.get_page_summary(executor.current_page)
            executor.last_context = summary
            print(f"   📋 URL: {summary.get('url', 'unknown')}")
            print(f"   📋 Título: {summary.get('title', 'unknown')}")
            if summary.get("elements", {}).get("search_boxes"):
                print(f"   🔍 Cajas de búsqueda encontradas: {len(summary['elements']['search_boxes'])}")
            return summary
        
        # ========== PESTAÑAS ==========
        @ActionRegistry.register("new_tab", "Abrir nueva pestaña")
        def new_tab(executor, step):
            name, page = executor.tab_manager.create_tab(step.tab_name or step.value)
            executor.current_tab_name = name
            executor.current_page = page
        
        @ActionRegistry.register("switch_tab", "Cambiar a otra pestaña")
        def switch_tab(executor, step):
            tab_name = step.tab_name or step.value
            page = executor.tab_manager.get_tab(tab_name)
            if page:
                executor.current_tab_name = tab_name
                executor.current_page = page
                print(f"   🔄 Cambiado a: '{tab_name}'")
            else:
                raise ValueError(f"Pestaña '{tab_name}' no existe")
        
        @ActionRegistry.register("close_tab", "Cerrar pestaña")
        def close_tab(executor, step):
            tab_name = step.tab_name or step.value or executor.current_tab_name
            if executor.tab_manager.close_tab(tab_name):
                if tab_name == executor.current_tab_name:
                    executor.current_tab_name = "main"
                    executor.current_page = executor.tab_manager.get_tab("main")
        
        # ========== YOUTUBE ==========
        @ActionRegistry.register("search_youtube", "Buscar en YouTube")
        def search_youtube(executor, step):
            print(f"   🔍 YouTube: '{step.value}'")
            executor._close_popups()
            search = executor._find_element([
                'input[name="search_query"]', 
                'input#search',
                'input[placeholder*="Search"]'
            ])
            search.fill(step.value)
            search.press("Enter")
            executor.current_page.wait_for_timeout(2000)
            print(f"   ✅ Búsqueda realizada")
        
        @ActionRegistry.register("click_first_video", "Click en primer video de YouTube")
        def click_first_video(executor, step):
            print(f"   🎥 Seleccionando video...")
            executor.current_page.wait_for_selector("ytd-video-renderer", timeout=10000)
            executor.current_page.locator("ytd-video-renderer a#thumbnail").first.click()
            print(f"   ✅ Video seleccionado")
        
        # ========== GMAIL ==========
        @ActionRegistry.register("open_gmail", "Abrir Gmail")
        def open_gmail(executor, step):
            executor.current_page.goto("https://mail.google.com", wait_until="domcontentloaded")
            executor.current_page.wait_for_timeout(3000)
            print(f"   📧 Gmail abierto")
        
        @ActionRegistry.register("compose_email", "Componer email")
        def compose_email(executor, step):
            print(f"   ✍️ Componiendo email...")
            executor.current_page.locator('div[role="button"]:has-text("Compose"), div[role="button"]:has-text("Redactar")').first.click()
            executor.current_page.wait_for_timeout(2000)
            executor.current_page.locator('input[name="to"]').fill(step.to)
            if step.subject:
                executor.current_page.locator('input[name="subjectbox"]').fill(step.subject)
            if step.body:
                executor.current_page.locator('div[role="textbox"]').last.fill(step.body)
            print(f"   ✅ Email listo")
        
        @ActionRegistry.register("send_email", "Enviar email")
        def send_email(executor, step):
            executor.current_page.locator('div[role="button"]:has-text("Send"), div[role="button"]:has-text("Enviar")').first.click()
            executor.current_page.wait_for_timeout(2000)
            print(f"   📤 Email enviado")
        
        # ========== WHATSAPP ==========
        @ActionRegistry.register("open_whatsapp", "Abrir WhatsApp Web")
        def open_whatsapp(executor, step):
            executor.current_page.goto("https://web.whatsapp.com", wait_until="domcontentloaded")
            print(f"   💬 WhatsApp - Escanea QR si es necesario")
            executor.current_page.wait_for_timeout(5000)
        
        @ActionRegistry.register("search_contact", "Buscar contacto en WhatsApp")
        def search_contact(executor, step):
            search = executor._find_element([
                'div[contenteditable="true"][data-tab="3"]',
                'div[title*="Search"]'
            ])
            search.fill(step.value)
            executor.current_page.wait_for_timeout(2000)
            executor.current_page.locator('div[role="listitem"]').first.click()
        
        @ActionRegistry.register("send_whatsapp_message", "Enviar mensaje WhatsApp")
        def send_whatsapp_message(executor, step):
            msg_box = executor._find_element([
                'div[contenteditable="true"][data-tab="10"]',
                'footer div[contenteditable="true"]'
            ])
            msg_box.fill(step.body)
            msg_box.press("Enter")
            print(f"   💬 Mensaje enviado")
    
    def _find_element(self, selectors: List[str]):
        for selector in selectors:
            try:
                elem = self.current_page.locator(selector).first
                if elem.is_visible(timeout=2000):
                    return elem
            except:
                continue
        raise RuntimeError(f"Elemento no encontrado")
    
    def _close_popups(self):
        popups = ['button[aria-label*="Accept"]', 'button:has-text("Accept")']
        for sel in popups:
            try:
                if self.current_page.locator(sel).is_visible(timeout=1000):
                    self.current_page.locator(sel).first.click()
                    break
            except:
                continue
    
    def execute(self, step: Step) -> Any:
        action_func = ActionRegistry.get(step.action)
        if action_func:
            return action_func(self, step)
        else:
            print(f"   ⚠️ Acción no registrada: {step.action}")
            return None
    
    def get_current_context(self) -> Dict:
        """Obtiene el contexto actual para la IA"""
        return PageAnalyzer.get_page_summary(self.current_page)

# =============================================================================
# AI PLANNER CON AUTO-RECUPERACIÓN
# =============================================================================

SYSTEM_PROMPT = """Eres un automatizador web inteligente. Creas planes paso a paso.

## ACCIONES DISPONIBLES:

### BÁSICAS:
- {"action": "goto", "value": "URL"} - Navegar a URL
- {"action": "wait", "ms": 2000} - Esperar milisegundos
- {"action": "click", "selector": "CSS"} - Click por selector
- {"action": "type_text", "selector": "CSS", "value": "texto"} - Escribir en campo
- {"action": "press_key", "value": "Enter"} - Presionar tecla
- {"action": "scroll", "value": "down|up"} - Scroll

### INTELIGENTES (¡ÚSALAS!):
- {"action": "smart_search", "value": "texto"} - Busca automáticamente el campo de búsqueda y escribe
- {"action": "smart_click", "value": "texto visible"} - Click por texto visible en botón/link
- {"action": "analyze_page"} - Analiza la página para obtener elementos disponibles

### PESTAÑAS:
- {"action": "new_tab", "tab_name": "nombre"} - Nueva pestaña
- {"action": "switch_tab", "tab_name": "nombre"} - Cambiar pestaña
- {"action": "close_tab"} - Cerrar pestaña actual

### YOUTUBE:
- {"action": "search_youtube", "value": "query"} - Buscar en YouTube
- {"action": "click_first_video"} - Click primer video

### GMAIL:
- {"action": "open_gmail"} - Abrir Gmail
- {"action": "compose_email", "to": "email", "subject": "asunto", "body": "mensaje"}
- {"action": "send_email"}

### WHATSAPP:
- {"action": "open_whatsapp"} - Abrir WhatsApp
- {"action": "search_contact", "value": "nombre"}
- {"action": "send_whatsapp_message", "body": "mensaje"}

## REGLAS CRÍTICAS:

1. **USA smart_search** en lugar de type_text cuando busques algo en un sitio
2. **USA smart_click** para clicks por texto visible
3. **Agrega waits** después de navegaciones (2000-3000ms)
4. Para sitios desconocidos, usa analyze_page primero si fallas

## FORMATO:
{"steps": [...], "reasoning": "explicación"}

## EJEMPLO:
Usuario: "busca two sum en leetcode"
{
  "steps": [
    {"action": "goto", "value": "https://leetcode.com"},
    {"action": "wait", "ms": 3000},
    {"action": "smart_search", "value": "two sum"}
  ],
  "reasoning": "Navego a LeetCode y uso smart_search que encuentra automáticamente el campo de búsqueda"
}
"""

RECOVERY_PROMPT = """Hubo un ERROR ejecutando el plan. Necesitas crear un NUEVO PLAN para completar la tarea.

## ERROR:
{error}

## PASO QUE FALLÓ:
{failed_step}

## CONTEXTO ACTUAL DE LA PÁGINA:
{context}

## TAREA ORIGINAL:
{original_task}

## PASOS YA COMPLETADOS:
{completed_steps}

## INSTRUCCIONES:
1. Analiza el error y el contexto actual
2. Crea un nuevo plan que complete la tarea desde donde quedó
3. Usa los selectores reales que aparecen en el contexto
4. Si hay search_boxes en el contexto, usa esos selectores
5. Puedes usar analyze_page para obtener más información

Retorna SOLO JSON válido con el nuevo plan.
"""

def create_plan(user_instruction: str, context: Dict = None) -> Plan:
    """Crea un plan usando IA"""
    print(f"🤖 Analizando: '{user_instruction}'")
    
    # Mayordomo anuncia
    butler = get_butler()
    if butler:
        butler.planning()
    
    # OpenAI requiere que el mensaje contenga "json" cuando usamos response_format
    user_message = f"{user_instruction}\n\nResponde SOLO con JSON válido."
    
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_message}
    ]
    
    if context:
        messages.append({"role": "system", "content": f"Contexto actual (json): {json.dumps(context)}"})
    
    response = client.chat.completions.create(
        model=MODEL,
        messages=messages,
        response_format={"type": "json_object"}
    )
    
    data = json.loads(response.choices[0].message.content)
    plan = Plan(**data)
    
    print(f"✅ Plan: {len(plan.steps)} pasos")
    if plan.reasoning:
        print(f"💡 {plan.reasoning}\n")
    
    return plan

def create_recovery_plan(
    original_task: str,
    error: str,
    failed_step: Step,
    completed_steps: List[Step],
    context: Dict
) -> Plan:
    """Crea un plan de recuperación cuando algo falla"""
    print(f"\n🔄 IA analizando el error...")
    
    prompt = RECOVERY_PROMPT.format(
        error=error,
        failed_step=failed_step.model_dump_json(),
        context=json.dumps(context, indent=2),
        original_task=original_task,
        completed_steps=[s.model_dump() for s in completed_steps]
    )
    
    # Agregar indicación de JSON para OpenAI
    prompt += "\n\nResponde SOLO con JSON válido."
    
    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt}
        ],
        response_format={"type": "json_object"}
    )
    
    data = json.loads(response.choices[0].message.content)
    plan = Plan(**data)
    
    print(f"✅ Plan de recuperación: {len(plan.steps)} pasos")
    if plan.reasoning:
        print(f"💡 {plan.reasoning}")
    
    return plan

# =============================================================================
# EJECUTOR CON AUTO-RECUPERACIÓN
# =============================================================================

def run_plan_with_recovery(
    executor: ActionExecutor, 
    plan: Plan, 
    original_task: str,
    max_retries: int = MAX_RETRIES
) -> bool:
    """Ejecuta el plan con auto-recuperación"""
    completed_steps = []
    current_plan = plan
    retry_count = 0
    butler = get_butler()
    
    while retry_count <= max_retries:
        print(f"\n🎬 Ejecutando {len(current_plan.steps)} pasos...\n")
        
        # Mayordomo anuncia inicio
        if butler and retry_count == 0:
            butler.executing()
        
        for i, step in enumerate(current_plan.steps):
            print(f"{'━'*50}")
            tab_info = f" [{executor.current_tab_name}]" if len(executor.tab_manager.tabs) > 1 else ""
            print(f"📍 [{i+1}/{len(current_plan.steps)}]{tab_info} {step.action}")
            if step.value:
                print(f"   → {step.value}")
            if step.selector:
                print(f"   → selector: {step.selector}")
            
            try:
                executor.execute(step)
                completed_steps.append(step)
                
                # Mayordomo comenta progreso solo en checkpoints importantes
                # Solo habla si hay más de 3 pasos y está a mitad del camino
                total_steps = len(current_plan.steps)
                current_step = i + 1
                if butler and total_steps >= 4:
                    # Checkpoint a la mitad
                    if current_step == total_steps // 2:
                        butler.say("Vamos por buen camino, señor.")
                
            except Exception as e:
                error_msg = f"{type(e).__name__}: {str(e)}"
                print(f"   ❌ ERROR: {error_msg}")
                
                # Guardar screenshot del error en carpeta temporal
                screenshot_path = save_debug_screenshot(executor.current_page, f"error_{retry_count}_{i}")
                if screenshot_path:
                    print(f"   📸 Debug screenshot: {os.path.basename(screenshot_path)}")
                
                if retry_count < max_retries:
                    print(f"\n🔄 Intentando auto-recuperación ({retry_count + 1}/{max_retries})...")
                    
                    # Mayordomo reporta error (solo una frase, no dos)
                    if butler:
                        butler.say("Un momento, señor. Encontré un obstáculo, pero tengo un plan alternativo.")
                    
                    # Obtener contexto actual
                    try:
                        context = executor.get_current_context()
                    except:
                        context = {"error": "No se pudo obtener contexto"}
                    
                    # Crear plan de recuperación
                    try:
                        recovery_plan = create_recovery_plan(
                            original_task=original_task,
                            error=error_msg,
                            failed_step=step,
                            completed_steps=completed_steps,
                            context=context
                        )
                        
                        # Mostrar nuevo plan
                        print("\n📋 Nuevo plan:")
                        for j, s in enumerate(recovery_plan.steps, 1):
                            print(f"   {j}. {s.action}" + (f" → {s.value}" if s.value else ""))
                        
                        current_plan = recovery_plan
                        retry_count += 1
                        break  # Salir del for para reintentar con nuevo plan
                        
                    except Exception as recovery_error:
                        print(f"   ❌ Error creando plan de recuperación: {recovery_error}")
                        retry_count += 1
                        continue
                else:
                    print(f"\n❌ Máximo de reintentos alcanzado ({max_retries})")
                    return False
        else:
            # El for terminó sin break = éxito
            print(f"\n{'━'*50}")
            print(f"✅ ¡Plan completado exitosamente!")
            
            # Mayordomo celebra
            if butler:
                butler.success()
            
            return True
    
    return False

# =============================================================================
# CONFIGURACIÓN DE SESIÓN Y NAVEGADORES
# =============================================================================

import socket
import subprocess
import platform
import shutil
import tempfile
import time

DEBUG_PORTS = [9222, 9223, 9224, 9225]  # Puertos a escanear
TEMP_DEBUG_DIR = None  # Se crea al inicio de sesión

def get_temp_debug_dir() -> str:
    """Obtiene o crea directorio temporal para archivos de debug"""
    global TEMP_DEBUG_DIR
    if TEMP_DEBUG_DIR is None or not os.path.exists(TEMP_DEBUG_DIR):
        TEMP_DEBUG_DIR = tempfile.mkdtemp(prefix="babas_debug_")
    return TEMP_DEBUG_DIR

def cleanup_temp_dir():
    """Limpia el directorio temporal"""
    global TEMP_DEBUG_DIR
    if TEMP_DEBUG_DIR and os.path.exists(TEMP_DEBUG_DIR):
        try:
            shutil.rmtree(TEMP_DEBUG_DIR, ignore_errors=True)
            print(f"   🧹 Archivos temporales eliminados")
        except:
            pass
        TEMP_DEBUG_DIR = None

def save_debug_screenshot(page, name: str = "error") -> str:
    """Guarda screenshot en carpeta temporal"""
    debug_dir = get_temp_debug_dir()
    filename = os.path.join(debug_dir, f"{name}_{datetime.now().strftime('%H%M%S')}.png")
    try:
        page.screenshot(path=filename)
        return filename
    except:
        return ""

def check_port(port: int) -> bool:
    """Verifica si un puerto está abierto"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(('localhost', port)) == 0

def scan_debug_ports() -> Optional[int]:
    """Escanea puertos de debug y retorna el primero disponible"""
    for port in DEBUG_PORTS:
        if check_port(port):
            return port
    return None

def get_browser_info() -> Dict[str, Dict]:
    """Retorna información de navegadores según el SO"""
    system = platform.system().lower()
    
    if system == "windows":
        # Múltiples rutas posibles para Windows
        chrome_paths = [
            os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]
        brave_paths = [
            os.path.expandvars(r"%PROGRAMFILES%\BraveSoftware\Brave-Browser\Application\brave.exe"),
            os.path.expandvars(r"%PROGRAMFILES(X86)%\BraveSoftware\Brave-Browser\Application\brave.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"),
        ]
        edge_paths = [
            os.path.expandvars(r"%PROGRAMFILES%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe"),
        ]
        
        # Encontrar la primera ruta válida
        chrome_path = next((p for p in chrome_paths if os.path.exists(p)), None)
        brave_path = next((p for p in brave_paths if os.path.exists(p)), None)
        edge_path = next((p for p in edge_paths if os.path.exists(p)), None)
        
        # Perfiles de usuario reales
        chrome_profile = os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data")
        brave_profile = os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data")
        edge_profile = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\User Data")
        
        return {
            "brave": {"path": brave_path, "profile": brave_profile, "port": 9222},
            "chrome": {"path": chrome_path, "profile": chrome_profile, "port": 9223},
            "edge": {"path": edge_path, "profile": edge_profile, "port": 9224},
        }
    else:
        # macOS / Linux
        return {
            "brave": {
                "path": "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser" if system == "darwin" else "/usr/bin/brave-browser",
                "profile": os.path.expanduser("~/.config/BraveSoftware/Brave-Browser"),
                "port": 9222
            },
            "chrome": {
                "path": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" if system == "darwin" else "/usr/bin/google-chrome",
                "profile": os.path.expanduser("~/.config/google-chrome"),
                "port": 9223
            },
        }

def get_available_browsers() -> List[str]:
    """Retorna lista de navegadores instalados, Brave primero"""
    browsers = get_browser_info()
    available = []
    # Orden de preferencia: Brave -> Chrome -> Edge
    for name in ["brave", "chrome", "edge"]:
        info = browsers.get(name, {})
        if info.get("path") and os.path.exists(info["path"]):
            available.append(name)
    return available

def get_preferred_browser() -> Optional[str]:
    """Retorna el navegador preferido (Brave > Chrome > Edge)"""
    available = get_available_browsers()
    return available[0] if available else None

def try_connect_to_browser(playwright) -> tuple[Optional[Any], Optional[int], Optional[str]]:
    """
    Intenta conectar a cualquier navegador en modo debug.
    Retorna: (browser, port, browser_name) o (None, None, None)
    """
    port = scan_debug_ports()
    if port:
        try:
            browser = playwright.chromium.connect_over_cdp(f"http://localhost:{port}")
            return browser, port, "detectado"
        except:
            pass
    return None, None, None

def is_profile_locked(profile_path: str) -> bool:
    """Verifica si el perfil está siendo usado por otra instancia"""
    lock_files = [
        os.path.join(profile_path, "lockfile"),
        os.path.join(profile_path, "SingletonLock"),
        os.path.join(profile_path, "SingletonSocket"),
    ]
    for lock in lock_files:
        if os.path.exists(lock):
            return True
    return False

def get_babas_profile_path(browser_name: str) -> str:
    """Retorna la ruta del perfil de BABAS para un navegador"""
    system = platform.system().lower()
    if system == "windows":
        return os.path.expandvars(rf"%LOCALAPPDATA%\babas-{browser_name}-profile")
    else:
        return os.path.expanduser(f"~/.babas-{browser_name}-profile")

def launch_browser_with_debug(browser_name: str) -> tuple[bool, int, str]:
    """
    Lanza navegador con debug habilitado usando perfil de BABAS.
    Retorna: (success, port, message)
    """
    browsers = get_browser_info()
    info = browsers.get(browser_name)
    
    if not info or not info.get("path") or not os.path.exists(info["path"]):
        return False, 0, "Navegador no encontrado"
    
    port = info.get("port", 9222)
    system = platform.system().lower()
    
    # Siempre usar perfil de BABAS (guarda sesiones entre usos)
    profile = get_babas_profile_path(browser_name)
    print(f"   📁 Perfil BABAS: {os.path.basename(profile)}")
    
    args = [
        info["path"],
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
    ]
    
    try:
        if system == "windows":
            subprocess.Popen(
                args, 
                creationflags=subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return True, port, "ok"
    except Exception as e:
        return False, 0, str(e)

def try_launch_and_connect(playwright, browser_name: str) -> tuple[bool, Any, int]:
    """
    Intenta lanzar un navegador y conectarse.
    Retorna: (success, browser, port)
    """
    print(f"\n🚀 Lanzando {browser_name.capitalize()}...")
    
    success, port, msg = launch_browser_with_debug(browser_name)
    
    if not success:
        print(f"   ❌ Error: {msg}")
        return False, None, 0
    
    print(f"   ⏳ Conectando...")
    
    for i in range(30):  # 15 segundos de espera
        time.sleep(0.5)
        if check_port(port):
            time.sleep(1)  # Pausa adicional
            try:
                browser = playwright.chromium.connect_over_cdp(f"http://localhost:{port}")
                contexts = browser.contexts
                pages_count = len(contexts[0].pages) if contexts else 0
                print(f"   ✅ ¡Conectado! ({pages_count} pestañas)")
                return True, browser, port
            except:
                continue
    
    print("   ❌ No se pudo conectar")
    return False, None, 0

def select_session_mode(playwright) -> tuple[str, dict, Any]:
    """
    Selección simplificada de navegador.
    Preferencia: Brave > Chrome > Edge
    """
    print("\n🌐 CONFIGURACIÓN DE NAVEGADOR")
    print("━"*50)
    
    available = get_available_browsers()
    preferred = get_preferred_browser()
    
    print(f"\n📋 Disponibles: {', '.join(b.capitalize() for b in available) if available else 'ninguno'}")
    if preferred:
        print(f"   Predeterminado: {preferred.capitalize()}")
    
    # 1. Verificar si ya hay navegador en modo debug
    port = scan_debug_ports()
    if port:
        print(f"\n✅ Navegador detectado en puerto {port}")
        try:
            browser = playwright.chromium.connect_over_cdp(f"http://localhost:{port}")
            contexts = browser.contexts
            pages = contexts[0].pages if contexts else []
            print(f"   ✅ Conectado! {len(pages)} pestañas")
            return "connect", {"port": port}, browser
        except Exception as e:
            print(f"   ⚠️ Error conectando: {e}")
    
    # 2. Menú simplificado
    print("\n¿Qué navegador usar?\n")
    if preferred:
        print(f"  [ENTER] 🦁 {preferred.capitalize()} (recomendado)")
    for i, name in enumerate(available, 1):
        if name != preferred:
            print(f"  [{i}] {name.capitalize()}")
    print(f"  [n] 🆕 Sesión temporal (sin guardar)")
    
    choice = input(f"\nOpción [ENTER = {preferred.capitalize() if preferred else 'Chrome'}]: ").strip().lower()
    
    # Sesión temporal
    if choice == "n":
        return "new", {}, None
    
    # Determinar navegador a usar
    if choice == "" and preferred:
        browser_name = preferred
    else:
        try:
            idx = int(choice) - 1
            browser_name = available[idx] if 0 <= idx < len(available) else preferred or "chrome"
        except:
            browser_name = preferred or "chrome"
    
    # 3. Intentar lanzar el navegador preferido
    success, browser, port = try_launch_and_connect(playwright, browser_name)
    
    if success:
        return "connect", {"port": port, "browser": browser_name}, browser
    
    # 4. Fallback: probar otros navegadores
    for fallback in available:
        if fallback != browser_name:
            print(f"\n🔄 Intentando con {fallback.capitalize()}...")
            success, browser, port = try_launch_and_connect(playwright, fallback)
            if success:
                return "connect", {"port": port, "browser": fallback}, browser
    
    # 5. Último recurso: usar Playwright directamente
    print("\n⚠️ No se pudo conectar a ningún navegador.")
    print("   Usando navegador integrado de Playwright...")
    return "saved", {}, None

def show_help():
    """Muestra ayuda de comandos"""
    print("\n📖 COMANDOS ESPECIALES:")
    print("   salir, exit, q     → Cerrar programa")
    print("   ayuda, help        → Mostrar esta ayuda")
    print("   pestañas, tabs     → Ver pestañas abiertas")
    print("   nueva, new         → Abrir nueva pestaña")
    print("   captura, screenshot → Tomar screenshot")
    print("   voz, voice         → Activar/silenciar mayordomo")
    print("   calla, shut up     → Callar al mayordomo ahora")
    print("\n💡 EJEMPLOS DE TAREAS:")
    print('   • "abre youtube y busca lofi music"')
    print('   • "ve a gmail"')
    print('   • "en nueva pestaña abre twitter"')
    print('   • "busca two sum en leetcode"')

# =============================================================================
# MAIN INTERACTIVO
# =============================================================================

def main():
    print("="*60)
    print("🤖 BABAS - Asistente de Automatización Web")
    print("="*60)
    print("\n📋 Capacidades:")
    print("   • Navegación web inteligente")
    print("   • Auto-recuperación con IA cuando falla")
    print("   • Múltiples pestañas")
    print("   • Conectar a TU navegador actual")
    print("   • 🎙️ Mayordomo con voz (escribe 'voz' para controlar)")
    print("\n💡 Escribe 'ayuda' para ver comandos")
    print("="*60)
    
    # Inicializar mayordomo con voz
    butler = None
    if VOICE_AVAILABLE:
        butler = init_butler()
        if butler and butler.enabled:
            print("\n🎙️ Mayordomo con voz activado")
    
    profile_path = None
    
    try:
        with sync_playwright() as p:
            # Seleccionar modo de sesión (ahora recibe playwright)
            mode, config, browser = select_session_mode(p)
            
            context = None
            is_connected = False
            
            if mode == "connect" and browser:
                # Ya conectado a navegador existente
                is_connected = True
                contexts = browser.contexts
                if contexts:
                    context = contexts[0]
                else:
                    context = browser.new_context()
            
            if context is None:
                # Lanzar navegador con Playwright
                if mode == "saved":
                    profile_path = os.path.join(os.path.dirname(__file__), "babas-profile")
                    print(f"\n💾 Usando sesión BABAS guardada...")
                else:
                    profile_path = os.path.join(get_temp_debug_dir(), "temp-profile")
                    print(f"\n🆕 Sesión temporal")
                
                context = p.chromium.launch_persistent_context(
                    user_data_dir=profile_path,
                    headless=False,
                    args=[
                        '--disable-blink-features=AutomationControlled',
                        '--start-maximized'
                    ],
                    viewport=None,
                    no_viewport=True
                )
            
            # Crear nueva pestaña para BABAS (no tocar las existentes)
            if is_connected:
                print("\n📑 Creando nueva pestaña para BABAS...")
                initial_page = context.new_page()
                print(f"   ✅ Pestaña creada (tus otras pestañas no serán tocadas)")
            else:
                if context.pages:
                    initial_page = context.pages[0]
                else:
                    initial_page = context.new_page()
            
            tab_manager = TabManager(context)
            executor = ActionExecutor(tab_manager, initial_page)
            
            print("\n" + "="*60)
            print("✅ ¡Listo! Escribe tus comandos.")
            print("="*60)
            
            # Mayordomo saluda
            if butler and butler.enabled:
                butler.greet()
            
            # === LOOP INTERACTIVO ===
            while True:
                try:
                    print("\n💬 ¿Qué quieres hacer? (o 'salir')")
                    user_input = input(">>> ").strip()
                    
                    if not user_input:
                        continue
                    
                    # Comandos especiales
                    cmd = user_input.lower()
                    
                    if cmd in ["salir", "exit", "q", "quit"]:
                        # Mayordomo se despide
                        if butler and butler.enabled:
                            butler.goodbye()
                        print("\n👋 ¡Hasta luego!")
                        break
                    
                    if cmd in ["ayuda", "help", "?"]:
                        show_help()
                        continue
                    
                    if cmd in ["pestañas", "tabs"]:
                        print("\n📑 Pestañas controladas por BABAS:")
                        for name in tab_manager.list_tabs():
                            page = tab_manager.get_tab(name)
                            marker = "→" if name == executor.current_tab_name else " "
                            try:
                                title = page.title()[:40] or page.url[:40]
                                print(f"   {marker} [{name}] {title}")
                            except:
                                print(f"   {marker} [{name}] (error)")
                        continue
                    
                    if cmd in ["nueva", "new"]:
                        name, page = tab_manager.create_tab()
                        executor.current_tab_name = name
                        executor.current_page = page
                        print(f"   ✅ Nueva pestaña: {name}")
                        continue
                    
                    if cmd in ["captura", "screenshot", "ss"]:
                        filename = f"screenshot_{datetime.now().strftime('%H%M%S')}.png"
                        executor.current_page.screenshot(path=filename)
                        print(f"   📸 Guardado: {filename}")
                        continue
                    
                    # Comandos de voz del mayordomo
                    if cmd in ["voz", "voice", "v"]:
                        if butler and butler.enabled:
                            butler.toggle_mute()
                        else:
                            print("   ⚠️ Mayordomo no disponible")
                        continue
                    
                    if cmd in ["calla", "callate", "shut up", "shh", "silence"]:
                        if butler and butler.enabled:
                            butler.shut_up()
                            print("   🤫 Mayordomo silenciado")
                        continue
                    
                    # Crear y ejecutar plan
                    print()
                    try:
                        plan = create_plan(user_input)
                        
                        # Mostrar plan
                        print("\n📋 Plan:")
                        for i, step in enumerate(plan.steps, 1):
                            details = step.value or step.selector or step.tab_name or ""
                            if details:
                                details = f" → {details[:40]}"
                            print(f"   {i}. {step.action}{details}")
                        
                        confirm = input("\n¿Ejecutar? (s/n) [s]: ").strip().lower()
                        if confirm == 'n':
                            print("❌ Cancelado")
                            continue
                        
                        # Ejecutar
                        success = run_plan_with_recovery(executor, plan, user_input)
                        
                        if success:
                            print("\n✅ ¡Listo!")
                        else:
                            print("\n⚠️ Completado parcialmente")
                            
                    except Exception as e:
                        print(f"\n❌ Error: {e}")
                        continue
                
                except KeyboardInterrupt:
                    print("\n\n(Ctrl+C detectado - escribe 'salir' para cerrar)")
                    continue
            
            # Limpiar
            print("\n🧹 Cerrando sesión...")
            
            # Cerrar todas las pestañas controladas por BABAS
            tabs_closed = 0
            for name, page in list(tab_manager.tabs.items()):
                try:
                    if not page.is_closed():
                        page.close()
                        tabs_closed += 1
                except:
                    pass
            
            if tabs_closed > 0:
                print(f"   📑 {tabs_closed} pestaña(s) cerrada(s)")
            
            if is_connected:
                # Desconectar del navegador (no lo cierra)
                print("   💡 Tu navegador sigue abierto (sin las pestañas de BABAS)")
                try:
                    browser.close()  # Solo desconecta
                except:
                    pass
            else:
                # Cerrar el navegador de Playwright
                try:
                    context.close()
                    print("   🌐 Navegador cerrado")
                except:
                    pass
            
            # Limpiar archivos temporales
            cleanup_temp_dir()
            
            # Limpiar perfil temporal si era sesión nueva
            if mode == "new" and profile_path:
                try:
                    shutil.rmtree(profile_path, ignore_errors=True)
                    print("   🗑️ Perfil temporal eliminado")
                except:
                    pass
            
            print("   ✅ Sesión finalizada")
            
            # Apagar mayordomo
            shutdown_butler()
    
    except KeyboardInterrupt:
        print("\n👋 Cancelado")
        cleanup_temp_dir()
        shutdown_butler()
    except Exception as e:
        print(f"\n💥 Error fatal: {e}")
        cleanup_temp_dir()
        shutdown_butler()
        raise

if __name__ == "__main__":
    main()
