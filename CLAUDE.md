# CLAUDE.md

BABAS (Browser Automation Bot Assistant System) is an LLM browser agent in Python. The user types or speaks a task,
the LLM returns a JSON plan of steps (Pydantic-validated, 21 action types), and an executor runs it in Chromium. When a
step fails, BABAS re-plans from the current page context (self-recovery). Voice I/O is optional.

**Who it's for:** people with low digital literacy, first of all older adults in Mexico. BABAS is a general-purpose
assistant: watching something on YouTube, searching for information, checking Google Classroom, reading or sending email,
using WhatsApp Web, and completing government procedures (SAT, IMSS, CURP lookup, citas). It explains every step in
plain Spanish.

**Direction:** drive the user's *real* Chromium through a Chrome MV3 extension (side panel UI), keeping the Python brain.
The roadmap with acceptance criteria is in `docs/PLAN.md`. Update this file whenever a phase lands.

## Status
| Phase | Scope | State |
|---|---|---|
| P1 | `babas/` package, async `BrowserDriver`, WebSocket bridge, minimal extension | in progress |
| P2 | All actions in the extension, side panel, safety layer and confirmations | planned |
| P3 | OpenAI-compatible provider config, MCP server | planned |
| P4 | Spanish assistant prompt, 2 real flows, README + GIF, MIT license | planned |

## Current architecture (pre-P1)
| File | Responsibility |
|---|---|
| `babas.py` | All core logic (~1.2k lines, map below) |
| `babas_voice.py` | ElevenLabs TTS `VoiceEngine` + English "butler" persona (`Butler`, `get_butler`); optional |
| `babas_listen.py` | Mic recording (sounddevice) + Whisper transcription (`whisper-1`, default `language="en"`); optional |
| `babas_gui.py` | customtkinter panel: record a command, confirm the plan, Stop flag; optional |
| `requirements.txt` | Core: openai, playwright, pydantic, python-dotenv; the rest is voice/GUI |

`babas.py` map: `PageAnalyzer` L46 (visible inputs/buttons/links → selectors) · `ActionRegistry` L124 · `TabManager`
L150 · `ActionType`/`Step`/`Plan` L178 · `ActionExecutor` L199 (21 handlers that call the Playwright `Page` directly) ·
`SYSTEM_PROMPT` L423 · `RECOVERY_PROMPT` L480 · `create_plan` L507 · `create_recovery_plan` L531 ·
`run_plan_with_recovery` L556 · debug screenshots L653 · `run_hacker_mode` L700 · browser discovery, CDP attach
(ports 9222-9225), launch with a BABAS profile L744-934 · `main()` REPL L950.

Actions: goto, wait, click, type_text, press_key, screenshot, scroll, smart_search, smart_click, analyze_page, new_tab,
switch_tab, close_tab, search_youtube, click_first_video, open_gmail, compose_email, send_email, open_whatsapp,
search_contact, send_whatsapp_message.

Flow: `main()` → `create_plan(task)` (no page context; `response_format=json_object`; `MODEL="gpt-5.2"`) → user
confirms → `run_plan_with_recovery` → on exception: debug screenshot + page context → `create_recovery_plan` (≤2 retries).

Gotchas:
- Import side effects in `babas.py`: `load_dotenv()`, prints, and an OpenAI client that raises without `OPENAI_API_KEY`.
- `Step` is one flat all-optional model: only `action` is validated; `value` is overloaded, `ms` doubles as scroll pixels.
- `SYSTEM_PROMPT` is hand-written (`ActionRegistry` descriptions are unused): a new action = `ActionType` + handler + prompt.
- Selectors are Playwright syntax (`:has-text()`, `text=`), not plain CSS; quotes inside them are not escaped.
- Stop is checked only between steps, never during `goto`, `wait` or LLM calls.
- Any input containing "hack" (Spanish "hackear" too) runs the `run_hacker_mode` easter egg: keep it out of the extension.
- CDP mode uses a separate BABAS profile (Chrome 136+ won't debug the default one), so it never had the user's real session.
- `.gitignore` ignores `*.png`, `lib/`, `build/` and `dist/`, but not `node_modules/`. No tests, CI or LICENSE exist yet.

## Run (today)
```bash
pip install -r requirements.txt
playwright install chromium          # only for the built-in browser fallback
echo "OPENAI_API_KEY=sk-..." > .env   # optional: ELEVENLABS_API_KEY for the voice butler
python babas.py                      # pick Text/Voice, then a browser; `help` lists REPL commands
```

## Conventions
- Python ≥3.10, Pydantic v2 (`model_dump`), type hints on signatures, English identifiers.
- Almost no comments (stripped on purpose in c99721f); add one only for a non-obvious *why*.
- Console progress uses `print` with 3-space-indented detail lines (P3 moves it to `logging` on stderr).
- Optional features are imported in `try/except ImportError` with no-op fallbacks; core runs without voice/GUI deps.
- New code uses `except Exception`, not bare `except:`.
- Anything the LLM emits or that crosses the bridge is parsed into a Pydantic model (Python) or a typed guard (TS), never trusted.
- Secrets only in `.env`. Never log or persist passwords; redact CURP/RFC/NSS/email/phone in logs.
- User-facing text: es-MX first, plain language, "usted", no jargon; the CLI stays English.
- Work on feature branches, never directly on `main`.

## Target architecture (phases + acceptance criteria: docs/PLAN.md)
- **Brain** (`babas/` package, asyncio): the planner, Pydantic action schemas and recovery loop stay, reshaped into an
  observe → plan → act loop over fresh page state each turn (element indices are only valid for one snapshot).
- **`BrowserDriver`** (async): `PlaywrightDriver` (today's code) | `ExtensionDriver` (WebSocket to the extension).
- **Bridge:** `python -m babas serve` ⇄ `ws://127.0.0.1:8765` ⇄ MV3 service worker; pinned extension Origin, keepalive.
- **Extension** (`extension/`, TypeScript + Vite, MV3): the content script indexes interactive elements (index, role, label,
  text, risk flags), highlights them, and executes click/type/select/scroll/navigate/read_text. The service worker relays
  and enforces Stop, the allowlist and confirmations. The side panel offers chat, a live step log, Stop and confirmations.
- **LLM:** one OpenAI-compatible client (`base_url` + `api_key` + `model`: Ollama, LM Studio, OpenRouter, Qwen,
  DeepSeek, OpenAI); native Anthropic optional.
- **MCP server:** `run_task`, `get_page_state`, `act`, all behind the same safety layer.
- Layout: `babas/{schemas,actions,agent,session,safety,llm,config,mcp_server,cli}.py`, `babas/{prompts,drivers,bridge}/`,
  `extension/src/{background,content,sidepanel,shared}/`, `tests/`.

## Bridge protocol v1 (summary; source of truth is `babas/bridge/protocol.py` + `extension/src/shared/protocol.ts`)
- Envelope `{v:1, id, type, payload}`. Replies are `{v:1, id, type:"result", ok, payload | error{code, message}}`.
- Handshake: SW `hello{ext_version, protocol}` → server `welcome{server_version}`. The SW pings every 20 s and reconnects
  with backoff. The server binds 127.0.0.1, checks the Origin, and allows one extension connection at a time.
- Python → SW requests: `page.state`, `page.act{op, target, value, snapshot_id, confirmation_id?}`, `tab.*`, `page.highlight`.
- SW → Python events (P2): `task.start{text}`, `run.stop`, `confirm.result`, `ask.result`, `site.allowed`.
- Error codes the agent reacts to: `STALE_SNAPSHOT` (re-observe), `NOT_FOUND`, `DOMAIN_NOT_ALLOWED` (ask to allow),
  `CONFIRMATION_REQUIRED`, `HALTED` (user pressed Stop), `TIMEOUT`, `NO_EXTENSION`.

## Safety policy (non-negotiable; enforced in Python AND in the extension)
Confirmation (a Sí/No dialog in the side panel) is required before:
- typing a value that looks like PII (CURP, RFC, NSS, email, MX phone), or typing into a field labeled as such;
- submitting a form that is not search-only (clicking its submit control or pressing Enter inside it);
- clicking an element flagged `consequential`: its label is a consequential verb (enviar/send, entregar/turn in/submit,
  pagar/pay, comprar/buy, eliminar/borrar/delete, publicar/post, firmar/sign, agendar/reservar/book, confirmar/confirm,
  transferir/transfer), or it sits on a payment page or field;
- `send_email`, `send_whatsapp_message`, and any step the LLM itself marks `consequential` (either signal is enough).

The extension mints a one-time confirmation id only when the user clicks "Sí". The id is bound to the exact action
(tab, op, element, value digest), and the extension writes the dialog's factual line itself, so Python's text can't
misrepresent the action.

Never type into password fields, never store or log passwords or typed PII, and never solve CAPTCHAs: hand them to the
user with `ask_user` ("Inicie sesión y presione Listo").

Sites: a curated default allowlist (gob.mx, google.com, youtube.com, whatsapp.com, wikipedia.org), and BABAS asks before
using any other site. The extension stores approvals; Python can't add sites. `BABAS_ALLOWED_DOMAINS` narrows the list.
Matching is by host suffix (`sat.gob.mx` matches `gob.mx`; `gob.mx.evil.com` does not).

Limits: `max_steps` (30), consecutive failures (`MAX_RETRIES`=2), confirmation timeout → deny.

Stop always works. Stop in the side panel halts the service worker (it rejects every command until the user starts a new
task) and cancels the Python run, including in-flight LLM calls and waits.

Fail closed: no side panel to answer → deny after the timeout. Page text is untrusted input (prompt injection), so
rules live in code, never only in the prompt. Logs redact PII.

## Decisions log
- 2026-10-11: `babas.py` → `babas/` package (`python -m babas`). Search-only forms skip the submit confirmation. Cloud
  LLMs may see page text (README privacy note; local models optional). P4 flow 2 = IMSS "Consulta tu NSS". Scope is
  general-purpose (YouTube, search, Classroom, email, trámites), so the allowlist has curated defaults plus
  ask-to-allow. A helper installs the unpacked extension for now.
