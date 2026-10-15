# BABAS

**Un asistente que usa su navegador para hacer tareas de internet por usted, con explicaciones sencillas y sin hacer nada importante sin preguntarle.**

*Browser Automation Bot Assistant System.*

## ¿Qué es BABAS?

BABAS es un asistente para personas con poca experiencia en computadoras, sobre todo adultos mayores. Usted le escribe
(o le dice) qué necesita, por ejemplo «busque videos de recetas en YouTube», y BABAS lo hace en su propio Chrome mientras
le explica cada paso en español sencillo. Sirve para cualquier tarea de internet, no solo para trámites.

Por dentro, un modelo de IA arma un plan de uno a tres pasos, BABAS lo ejecuta en el navegador y, si algo falla, vuelve a
mirar la página y prueba otra forma.

## Qué puede hacer

- **YouTube:** buscar y abrir videos.
- **Búsquedas:** encontrar información en Google y resumirla, diciendo de qué sitio viene.
- **Google Classroom:** revisar tareas pendientes, fechas y avisos.
- **Gmail:** leer correos y redactar o enviar uno (siempre con su aprobación).
- **WhatsApp Web:** buscar un contacto y enviar un mensaje (siempre con su aprobación).
- **Trámites de gobierno:** consulta de CURP, número de seguro social del IMSS, SAT y citas, solo en sitios `gob.mx`.

## Su seguridad

BABAS le pregunta antes de hacer algo importante. Estas reglas no se pueden saltar ([`docs/SAFETY.md`](docs/SAFETY.md)):

- Pide su permiso antes de **enviar un formulario**, **pagar**, **mandar un mensaje** o hacer clic en algo con consecuencias
  (enviar, entregar, comprar, borrar, publicar, firmar, agendar, confirmar, transferir, solicitar, registrarse).
- Pide su permiso antes de **escribir datos personales** (CURP, RFC, NSS, correo, teléfono, tarjeta).
- **Nunca escribe contraseñas** ni resuelve CAPTCHAs: usted inicia sesión y los resuelve en la página, y BABAS espera.
- Solo trabaja en sitios permitidos (`gob.mx`, `google.com`, `youtube.com`, `whatsapp.com`, `wikipedia.org`). Para
  cualquier otro sitio le pregunta primero y usted decide con «Permitir».
- El botón rojo **Detener** siempre funciona. Si usted contesta «No» a cualquier pregunta, BABAS termina la tarea.
- Tiene un límite de 30 pasos por tarea. El texto de las páginas se trata como información, nunca como órdenes.

## Privacidad

Para entender una página, BABAS envía su texto al proveedor de IA que usted configure (OpenAI y otros servicios en la
nube lo reciben). Si no quiere que salga de su computadora, use un modelo local (Ollama o LM Studio, ver
[`docs/providers.md`](docs/providers.md)). Si usa la voz, la grabación (máximo 30 segundos y solo después de pulsar «Hablar») se envía al servicio de voz que usted
configure, o se queda en su computadora con el modo local; BABAS no la guarda ni la registra. Los datos personales se enmascaran en los registros y en el historial; solo
se muestran completos en la tarjeta donde usted los aprueba. Las claves se guardan únicamente en su archivo `.env`.

## Requisitos

- Python 3.10 o más nuevo.
- Node.js 20.19 o más nuevo, o 22 (solo para construir la extensión).
- Chrome o Chromium 116 o más nuevo.
- Un proveedor de IA: una clave de OpenAI (u otro servicio compatible) o un modelo local.

## Instalación paso a paso

Pensada para la persona de confianza que instala BABAS en la computadora.

1. Descargue el proyecto y prepare Python:
   ```bash
   git clone https://github.com/charlyguz/babas-beta.git && cd babas-beta
   python -m venv .venv
   source .venv/bin/activate        # en Windows: .venv\Scripts\activate
   pip install -e .
   ```
2. Construya la extensión:
   ```bash
   cd extension && npm ci && npm run build && cd ..
   ```
3. En Chrome abra `chrome://extensions`, active **Modo de desarrollador**, pulse **Cargar descomprimida** y elija la
   carpeta `extension/dist`.
4. Cree un archivo `.env` en la carpeta del proyecto con el proveedor de IA (vea la sección siguiente):
   ```
   OPENAI_API_KEY=su_clave_aqui
   ```
5. Inicie BABAS y déjelo abierto:
   ```bash
   python -m babas serve
   ```
6. En Chrome pulse el icono de BABAS para abrir el panel lateral. La primera vez, la extensión se empareja sola con el
   programa. Debe decir «Conectado».

## Proveedor de IA

BABAS usa OpenAI por defecto. Cualquier servicio compatible con OpenAI funciona con tres variables en `.env`:

| Variable | Para qué sirve | Ejemplo |
|---|---|---|
| `BABAS_LLM_BASE_URL` | Dirección del servicio (vacía = OpenAI) | `http://localhost:11434/v1` |
| `BABAS_LLM_API_KEY` | Clave del servicio (también sirve `OPENAI_API_KEY`) | `sk-...` |
| `BABAS_LLM_MODEL` | Modelo a usar (por defecto `gpt-5.2`) | `qwen2.5:7b` |

```bash
# OpenRouter
BABAS_LLM_BASE_URL=https://openrouter.ai/api/v1
BABAS_LLM_API_KEY=sk-or-...
BABAS_LLM_MODEL=el-modelo-que-elija

# Modelo local con Ollama (no necesita clave; inícielo con OLLAMA_CONTEXT_LENGTH=16384)
BABAS_LLM_BASE_URL=http://localhost:11434/v1
BABAS_LLM_MODEL=qwen2.5:7b

# Claude (pip install -e '.[anthropic]')
BABAS_LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
BABAS_LLM_MODEL=el-modelo-que-elija
```

Otras variables útiles: `BABAS_LANG` (`es` o `en`, por defecto `es`), `BABAS_ALLOWED_DOMAINS` (sitios que se suman a
los permitidos; `*` permite todos), `BABAS_MAX_STEPS`, `BABAS_MAX_RETRIES`, `BABAS_BRIDGE_PORT` y `BABAS_LOG_LEVEL`. Para
la voz: `BABAS_STT_PROVIDER` (`auto`, `chat`, `whisper`, `local` u `off`), `BABAS_STT_MODEL`, `BABAS_STT_BASE_URL`,
`BABAS_STT_API_KEY` y `BABAS_STT_LANGUAGE`. La
lista completa, los proveedores probados y el modo JSON están en [`docs/providers.md`](docs/providers.md).

## Uso

**Panel lateral (recomendado).** Con `python -m babas serve` en marcha, abra el panel de BABAS, escriba la tarea y pulse
Enter, por ejemplo «busque videos de recetas en YouTube». BABAS muestra cada paso, le pide permiso cuando hace falta y
tiene un botón **Detener**.

**Voz.** En el panel, pulse **Hablar**, diga lo que necesita y pulse **Listo** (también se detiene solo cuando usted hace
pausa). Lo que dijo aparece escrito en la caja para que lo lea y lo corrija; no se envía hasta que usted pulse **Enviar**.
La primera vez el navegador pide permiso para el micrófono; si no lo pide, el panel muestra «Dar permiso». Hace falta
configurar el reconocimiento de voz (con Gemini usa la misma clave; también hay Whisper o un modelo local sin internet):
vea la sección 4 de [`docs/providers.md`](docs/providers.md).

**Terminal.** `python -m babas` abre una consola con su propio navegador (Playwright). Comandos: `help`, `tabs`, `new`,
`screenshot`, `mute` y `exit`. La voz en la consola es opcional: `pip install -e '.[voice]'` (micrófono, con el mismo reconocimiento de voz de arriba) y
`ELEVENLABS_API_KEY` en `.env` solo si quiere que BABAS también le hable.

| Comando | Qué hace |
|---|---|
| `python -m babas` | Consola con IA en su navegador |
| `python -m babas serve` | Servicio para el panel lateral (`--host`, `--port`, `--mcp-http`, `--repl`) |
| `python -m babas serve --repl` | Consola de desarrollo sin IA: `goto URL`, `ls`, `click N`, `type N texto` |
| `python -m babas pair [--reset]` | Muestra u olvida el emparejamiento con la extensión |
| `python -m babas mcp` | Servidor MCP por stdio (`pip install -e '.[mcp]'`) |

**Otras aplicaciones de IA (MCP).** BABAS también es un servidor MCP (`run_task`, `get_page_state`, `act`) para Claude
Code, Claude Desktop o Cursor. Las confirmaciones siguen apareciendo en el panel de BABAS y no se pueden saltar.

```bash
claude mcp add babas -- /ruta/a/.venv/bin/python -m babas mcp                  # stdio
python -m babas serve --mcp-http                                               # HTTP: http://127.0.0.1:8766/mcp
```

La configuración de cada cliente y sus límites están en [`docs/providers.md`](docs/providers.md).

## Solución de problemas

- **«Sin conexión con BABAS» en el panel:** el programa no está en marcha o usa otro puerto. Ejecute
  `python -m babas serve` y revise que `BABAS_BRIDGE_PORT` sea el mismo. Si reinstaló la extensión, ejecute
  `python -m babas pair --reset` y vuelva a abrir el panel.
- **Dice que falta la clave:** revise el archivo `.env` (`OPENAI_API_KEY` o `BABAS_LLM_API_KEY`). BABAS arranca sin ella
  y le avisa en cada tarea qué variable falta.
- **BABAS no puede usar una página:** las páginas internas de Chrome (`chrome://...`) y la tienda de extensiones están
  cerradas para las extensiones. Abra una página normal en la pestaña.
- **Un sitio no responde a los clics:** algunos sitios ignoran los eventos automáticos. Pruebe otra forma o avise en
  [`docs/manual-tests.md`](docs/manual-tests.md).
- **Con un modelo local las respuestas se cortan:** inicie Ollama con `OLLAMA_CONTEXT_LENGTH` de 16384 o más.

## Desarrollo

```bash
pip install -e '.[dev]' && pytest -q                 # pruebas de Python (las e2e necesitan extension/dist)
cd extension && npm test && npm run typecheck        # pruebas de la extensión
```

Mapa del código, convenciones y trampas conocidas: [`CLAUDE.md`](CLAUDE.md). Plan por fases: [`docs/PLAN.md`](docs/PLAN.md) y
[`docs/tasks/`](docs/tasks/). Especificaciones: [`docs/PROTOCOL.md`](docs/PROTOCOL.md) y [`docs/SAFETY.md`](docs/SAFETY.md).
Revisión manual en sitios reales: [`docs/manual-tests.md`](docs/manual-tests.md).

## Licencia

BABAS se distribuye bajo la licencia **MIT**: puede usar, copiar, modificar y compartir el programa, incluso con fines
comerciales, siempre que conserve el aviso de derechos y la licencia (ver [`LICENSE`](LICENSE)). El programa se ofrece
«tal cual», sin garantía de ningún tipo. Copyright (c) 2026 charlyguz.

## English summary

BABAS is an LLM browser assistant for people with little computer experience (first of all older adults in Mexico). You
type a task in plain language; BABAS plans 1-3 steps, drives your real Chrome through a Manifest V3 extension with a side
panel, and explains each step in plain Spanish. It asks before submitting forms, sending, paying, deleting or typing
personal data (CURP, RFC, NSS, email, phone, card); it never types passwords or solves CAPTCHAs; Stop always works.
Setup: `pip install -e .`, `cd extension && npm ci && npm run build`, load `extension/dist` in `chrome://extensions`, put
`OPENAI_API_KEY` (or `BABAS_LLM_BASE_URL` / `BABAS_LLM_API_KEY` / `BABAS_LLM_MODEL` for any OpenAI-compatible provider,
e.g. OpenRouter, Ollama) in `.env`, then run `python -m babas serve`. `python -m babas` is the terminal agent and
`python -m babas mcp` an MCP server. Page text is sent to the AI provider you choose; use a local model to keep it on your
machine. Licensed under MIT, see [`LICENSE`](LICENSE) and [`docs/providers.md`](docs/providers.md).
