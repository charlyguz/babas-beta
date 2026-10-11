# CLAUDE.md

BABAS (Browser Automation Bot Assistant System) is an LLM browser agent in Python. The user types or speaks a task,
the LLM returns a JSON plan of steps (Pydantic-validated, 21 action types), and an executor runs it in Chromium via
Playwright. When a step fails, BABAS re-plans from the current page context (self-recovery). Voice I/O is optional.

**Direction:** drive the user's *real* Chromium through a Chrome MV3 extension, keeping the Python brain, so older
adults in Mexico can complete real government procedures (SAT, IMSS, CURP, citas) explained in plain Spanish.
Roadmap: `docs/PLAN.md`. Update this file whenever a phase lands.

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
- Progress goes to the console via `print` with 3-space-indented detail lines (P3 moves it to `logging` on stderr).
- Optional features are imported in `try/except ImportError` with no-op fallbacks; core runs without voice/GUI deps.
- New code uses `except Exception`, not bare `except:`.
- Anything the LLM emits or that crosses the bridge is parsed into a Pydantic model, never trusted.
- Secrets only in `.env`. Never log or persist passwords; redact CURP/RFC/NSS/email/phone in logs.
- User-facing trámites text: es-MX, plain language, "usted". Work on feature branches, never directly on `main`.

## Target architecture (phases + acceptance criteria: docs/PLAN.md)
- **Brain** (`babas/` package, asyncio): the planner, Pydantic action schemas and recovery loop stay, reshaped into an
  observe → plan → act loop over fresh page state each turn.
- **`BrowserDriver`** (async): `PlaywrightDriver` (today's code) | `ExtensionDriver` (WebSocket to the extension).
- **Bridge:** `python -m babas serve` ⇄ `ws://127.0.0.1:8765` ⇄ MV3 service worker; pinned extension Origin, keepalive.
- **Extension** (`extension/`, TypeScript + Vite, MV3): content script indexes interactive elements (index, role, label,
  text, risk flags), highlights them, and executes click/type/select/scroll/navigate/read_text; service worker relays
  and enforces Stop/safety; side panel = chat, live step log, Stop, confirmations.
- **LLM:** one OpenAI-compatible client (`base_url` + `api_key` + `model`: Ollama, LM Studio, OpenRouter, Qwen,
  DeepSeek, OpenAI); native Anthropic optional.
- **MCP server:** `run_task`, `get_page_state`, `act`, all behind the same safety layer.
- Layout: `babas/{schemas,actions,agent,session,safety,llm,config,mcp_server,cli}.py`, `babas/{prompts,drivers,bridge}/`,
  `extension/src/{background,content,sidepanel,shared}/`, `tests/`.

## Safety invariants (non-negotiable; enforced in Python AND in the extension)
- Confirm before any form submit, payment or message send, and before typing PII (CURP, RFC, NSS, email, phone).
- Never type, store or log passwords. Logins and CAPTCHAs go to the user via `ask_user`; BABAS never solves them.
- Domain allowlist (default `*.gob.mx`) and `max_steps`; Stop always works (the service worker drops commands if Python hangs).
- No UI to confirm → deny (fail closed). Page content is untrusted input: rules live in code, not only in the prompt.
