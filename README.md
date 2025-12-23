# 🤖 BABAS - Bot de Automatización Basado en Asistente con IA

Asistente de automatización web inteligente que usa IA (GPT-4) para planificar y ejecutar tareas en el navegador.

![Python](https://img.shields.io/badge/Python-3.9+-blue.svg)
![Playwright](https://img.shields.io/badge/Playwright-latest-green.svg)
![OpenAI](https://img.shields.io/badge/OpenAI-GPT--4-orange.svg)

## ✨ Características

- 🧠 **IA Inteligente**: Interpreta instrucciones en lenguaje natural
- 🔄 **Auto-recuperación**: Si algo falla, la IA analiza y reintenta con otro enfoque
- 📑 **Múltiples pestañas**: Trabaja en varias pestañas simultáneamente
- 🔗 **Conecta a tu navegador**: Usa tus sesiones existentes de Gmail, WhatsApp, etc.
- 💾 **Sesiones persistentes**: Guarda tus logins entre usos
- 🎬 **Integraciones**: YouTube, Gmail, WhatsApp Web

## 📋 Requisitos

- Python 3.9+
- Cuenta de OpenAI con API key

## 🚀 Instalación

1. **Clonar el repositorio**
```bash
git clone https://github.com/tu-usuario/babas.git
cd babas
```

2. **Crear entorno virtual (recomendado)**
```bash
python -m venv venv
# Windows
venv\Scripts\activate
# Linux/Mac
source venv/bin/activate
```

3. **Instalar dependencias**
```bash
pip install -r requirements.txt
```

4. **Instalar navegadores de Playwright**
```bash
playwright install chromium
```

5. **Configurar API Key de OpenAI**
```bash
# Crear archivo .env
echo OPENAI_API_KEY=tu-api-key-aqui > .env
```

## 💻 Uso

```bash
python babas_text.py
```

### Menú de Configuración

Al iniciar, BABAS te preguntará cómo quieres trabajar:

```
¿Cómo quieres trabajar?

  [1] 🔗 Usar MI NAVEGADOR (abre nuevo con tus sesiones)
  [2] 💾 Sesión BABAS (perfil separado, guarda sesiones)
  [3] 🆕 Sesión TEMPORAL (se borra al cerrar)
```

### Ejemplos de Comandos

```
>>> abre youtube y busca lofi music
>>> ve a gmail y revisa mis correos
>>> en nueva pestaña abre twitter
>>> busca two sum en leetcode
>>> abre whatsapp y busca a María
```

### Comandos Especiales

| Comando | Descripción |
|---------|-------------|
| `salir` / `exit` / `q` | Cerrar BABAS |
| `ayuda` / `help` | Mostrar ayuda |
| `pestañas` / `tabs` | Ver pestañas abiertas |
| `nueva` / `new` | Abrir nueva pestaña |
| `captura` / `screenshot` | Tomar screenshot |

## 🔧 Opciones de Navegador

### Opción 1: Usar tu navegador (con tus sesiones)

BABAS puede conectarse a una instancia de Brave/Chrome con tus sesiones:

**Si tu navegador está cerrado**: BABAS lo abrirá con tu perfil real.

**Si tu navegador está abierto**: BABAS usará un perfil separado (pero guarda sesiones).

### Opción 2: Siempre listo para BABAS

Crea un acceso directo de tu navegador con este argumento:

**Brave:**
```
"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe" --remote-debugging-port=9222
```

**Chrome:**
```
"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222
```

Cuando abras tu navegador desde ese acceso directo, BABAS lo detectará automáticamente.

## 📁 Estructura del Proyecto

```
babas/
├── babas_text.py      # Código principal
├── requirements.txt   # Dependencias
├── .env              # API keys (no incluido en git)
├── .gitignore        # Archivos ignorados
├── README.md         # Este archivo
└── babas-profile/    # Perfil de sesiones guardadas (ignorado en git)
```

## 🛠️ Acciones Disponibles

### Básicas
- `goto` - Navegar a URL
- `click` - Click en elemento
- `type_text` - Escribir texto
- `press_key` - Presionar tecla
- `wait` - Esperar
- `scroll` - Hacer scroll
- `screenshot` - Capturar pantalla

### Inteligentes
- `smart_search` - Busca automáticamente el campo de búsqueda
- `smart_click` - Click por texto visible

### Pestañas
- `new_tab` - Nueva pestaña
- `switch_tab` - Cambiar pestaña
- `close_tab` - Cerrar pestaña

### YouTube
- `search_youtube` - Buscar en YouTube
- `click_first_video` - Reproducir primer video

### Gmail
- `open_gmail` - Abrir Gmail
- `compose_email` - Componer email
- `send_email` - Enviar email

### WhatsApp
- `open_whatsapp` - Abrir WhatsApp Web
- `search_contact` - Buscar contacto
- `send_whatsapp_message` - Enviar mensaje

## 🔄 Auto-recuperación

Si un paso falla, BABAS:
1. 📸 Toma screenshot del error
2. 🔍 Analiza el contexto actual de la página
3. 🤖 Pide a la IA un nuevo plan
4. 🔄 Reintenta (hasta 3 veces)

## ⚠️ Notas

- Los screenshots de debug se guardan en una carpeta temporal que se borra al cerrar
- El perfil `babas-profile/` guarda tus sesiones entre usos
- Nunca compartas tu archivo `.env` con tu API key

## 📝 Licencia

MIT License

## 🤝 Contribuir

¡Las contribuciones son bienvenidas! Por favor abre un issue o pull request.

