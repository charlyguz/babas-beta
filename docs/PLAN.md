# Plan: BABAS in the user's real browser

Goal: keep the Python brain (planner, Pydantic action schemas, recovery loop), execute in the user's real Chromium through
an MV3 extension, and expose the agent over MCP. BABAS is a general-purpose assistant: YouTube, web search, Google
Classroom, Gmail, WhatsApp, and government procedures (SAT, IMSS, CURP). It is built for people with low digital
literacy (older adults in Mexico) and explains every step in plain Spanish. Work happens off `main`; context lives in `CLAUDE.md`.

## Decisions (answered 2026-10-11)
- `babas.py` becomes the `babas/` package; run it with `python -m babas`.
- Search-only forms (`role=search`, `type=search`, a single query box) skip the submit confirmation, to avoid confirmation fatigue.
- Cloud LLMs may see page text, including personal data. Local models stay optional, and the README carries a privacy note.
- P4 flows: (1) Consulta CURP (read-only); (2) IMSS "Consulta tu NSS" (submits a form, emails the NSS).
- Distribution for now: a helper installs Python and the unpacked extension. The Web Store and an installer come later.

## Changes to the brief, and why
1. **Observe → act, not plan-once.** Extension element indices are only valid for the snapshot they came from, and today
   `create_plan()` runs blind (`main()` never passes `context`). Each turn reads `PageState`, plans 1-3 steps, executes them
   and observes again. `RECOVERY_PROMPT` and `MAX_RETRIES` stay: an error becomes input to the next turn.
2. **Async `BrowserDriver`.** The WS bridge (`websockets`) and the MCP SDK are asyncio, and Playwright's sync API can't run
   inside an event loop. `PlaywrightDriver` moves to `playwright.async_api`, and Stop becomes a task cancel.
3. **Per-action schemas.** The flat, all-optional `Step` becomes a discriminated union on `action` (same JSON shape). This
   gives real validation errors for recovery, plus the JSON Schema for MCP `act` and for structured outputs.
4. **Safety in two layers.** Python `SafetyPolicy` decides; the extension, as executor, enforces. Gated actions need a one-time
   confirmation id that only a side-panel click can mint, bound to the exact action. Password fields are refused, and the
   allowlist and Stop are checked in the service worker. Neither the LLM, an agent bug, nor an MCP client can bypass this.
5. **General-purpose allowlist.** Because BABAS works on any site, `host_permissions` is `<all_urls>` and content scripts
   are injected on demand. BABAS's own allowlist ships with common sites (gob.mx, google.com, youtube.com, whatsapp.com,
   wikipedia.org). Before using a new site, it asks in the side panel ("¿Permitir ejemplo.com?"). The extension stores
   user approvals; Python cannot add sites on its own.
6. **Consequential clicks, not just forms.** Gmail's "Enviar" or Classroom's "Entregar" are buttons, not form submits. The
   content script flags buttons whose label is a consequential verb (send, submit, turn in, pay, buy, delete, post, sign,
   book), and these need confirmation. The LLM may also mark a step `consequential`: either signal triggers a confirmation.

## Bridge protocol v1 (Python ⇄ service worker, JSON text frames)
- Envelope `{v:1, id, type, payload}`; reply `{v:1, id, type:"result", ok, payload | error{code, message}}`. Handshake:
  `hello{ext_version, protocol}` → `welcome`. The SW pings every 20 s (keeps the MV3 worker alive) and reconnects with backoff.
- The server binds 127.0.0.1 only and accepts only Origin `chrome-extension://<id pinned via manifest "key">`, one connection at a time.
- `PageState{snapshot_id, url, title, elements[{index, role, tag, label, text, type, flags}], text}`, flags ⊂ {submit,
  search_form, consequential, password, pii, captcha, disabled}. Actions carry `snapshot_id`; a stale one fails with `STALE_SNAPSHOT`.
- Types live in `babas/bridge/protocol.py` and `extension/src/shared/protocol.ts`, kept in sync by shared JSON fixtures in
  `tests/fixtures/protocol/` that both pytest and vitest validate.

## P1: BrowserDriver + WebSocket bridge + minimal extension
- **Create:** `pyproject.toml` (package `babas`, extras `voice`/`dev`), `babas/__main__.py` (`python -m babas [cli|serve]`).
  `babas/{schemas,actions,agent,cli}.py` and `babas/prompts/__init__.py` come from a mechanical split of `babas.py`.
  `babas/drivers/base.py` holds the `BrowserDriver` ABC (goto, get_page_state, click, type_text, press_key, scroll,
  screenshot, read_text, select_option, tab ops), `Target{index|selector|text}`, `PageState` and `DriverError`.
  Also `babas/drivers/{playwright_driver,extension_driver}.py`, `babas/bridge/{server,protocol}.py`, and `extension/`
  (Vite + TS, no CRX plugin: SW as ES module, content script as IIFE; `src/{background,content,shared}/`).
  Tests: `tests/{test_protocol,test_bridge,test_driver_contract}.py`, `tests/e2e/`, fixtures, vitest tests, and `.github/workflows/ci.yml`.
- **Modify:** `babas.py` (removed), `requirements.txt` (`-e .[voice]`), `.gitignore` (`node_modules/`, icon PNGs), README run command.
- **Guardrail:** P1 has no safety layer, so only `serve --repl` and tests drive the extension, never the LLM.
- **Acceptance**
  - `python -m babas` with Playwright behaves as before, and `import babas.agent` works without `OPENAI_API_KEY` and prints nothing.
  - `serve --repl` with the unpacked extension on a real page: `ls` lists and highlights inputs, buttons, links and selects
    with their labels; `type N …`, `click N` and `goto URL` act on the page.
  - Bridge: a foreign Origin is rejected; a timeout surfaces as a driver error; the SW reconnects after a server restart.
  - CI is green: pytest (unit, bridge, contract), vitest, `npm run build`, and a headless E2E on Playwright's bundled
    Chromium with `--load-extension` (branded Chrome ≥137 ignores that flag).

## P2: All actions + side panel + confirmation flow
- **Create:** `babas/safety.py`, `babas/session.py` (one active run; Stop = task cancel; confirm/ask_user/allow-site
  futures), `extension/src/sidepanel/`, `extension/public/_locales/{es,en}/`, `tests/test_safety.py`, safety fixtures,
  and `docs/manual-tests.md` (YouTube, Google search, Classroom, Gmail, WhatsApp, gob.mx checklist).
- **Modify:** `schemas.py` (union plus `index`/`snapshot_id`/`explanation`/`consequential`; new `select_option`, `read_text`,
  `ask_user`, `done`); `actions.py` (all 21 handlers on driver primitives); `agent.py` (observe → act, `max_steps`=30);
  `prompts/` (action list generated from the schemas); both drivers (`PlaywrightDriver` injects the content-script bundle,
  so `PageState` and indices match); the SW and the content script.
- **Execution:** click = scrollIntoView, then pointer/mouse events. type = native value setter, then `input`/`change`
  (contenteditable uses `insertText`). `press_key Enter` inside a form = `requestSubmit()`, which is gated unless the form is
  search-only. Waits = `tabs.onUpdated` complete, then DOM quiet. BABAS acts only in its own tab group.
  `screenshot` = `captureVisibleTab`.
- **Safety:** PII detectors (CURP, RFC, NSS, email, MX phone) check values and field labels. Gating covers submit,
  consequential and payment clicks, plus `send_email`/`send_whatsapp_message`. Password fields and CAPTCHAs → `ask_user`.
  New sites → ask to allow. Logs redact PII. If no side panel can answer a confirmation, the request is denied after a timeout.
- **Side panel (Spanish first):** chat, a live step log with a plain-language `explanation`, a sticky Stop, Sí/No dialogs
  whose factual line ("Escribir «…» en «CURP» de gob.mx") is built by the extension, and an ask_user prompt with "Listo".
- **Acceptance:** all 21 actions pass the fixture contract tests with both drivers. Stop halts within 1 s in every state
  (planning, acting, waiting, confirming), and no page action follows. The extension refuses a forged `act` with no
  confirmation id. Search-only forms submit without a prompt. Unknown sites prompt. The test CURP never appears in logs.

## P3: OpenAI-compatible provider + MCP server
- **Create:** `babas/config.py` (`BABAS_LLM_BASE_URL`/`_API_KEY`/`_MODEL`/`_JSON_MODE`, allowlist, limits, ports;
  `OPENAI_API_KEY` still honored), `babas/llm.py` (`LLMClient` protocol, `OpenAICompatibleClient` on `AsyncOpenAI`,
  optional `AnthropicClient`), `babas/mcp_server.py`, `tests/test_llm.py`, `tests/test_mcp.py`, `docs/providers.md`.
- **Modify:** `agent.py` (no global client or `MODEL`), `__main__.py` (`mcp`). Every `print` becomes `logging` on stderr,
  because stdout is the MCP stdio channel.
- **Details:** JSON mode `auto` tries json_schema, then json_object, then prompt-only; it extracts the JSON and retries once
  with the validation error. Presets: OpenAI, Ollama (`OLLAMA_CONTEXT_LENGTH` ≥16k), LM Studio, OpenRouter, Qwen, DeepSeek.
  MCP (official `mcp` SDK): `run_task`, `get_page_state`, `act` (its schema is the action union). Stdio hosts the bridge
  in-process, and `serve --mcp-http` adds streamable HTTP. No tool has a "confirmed" flag.
- **Acceptance:** mocked-server tests cover every JSON mode and the repair retry. An in-memory MCP client lists 3 typed
  tools and runs `get_page_state`, then a gated `act` that waits for side-panel approval. Snippets in `docs/providers.md`.

## P4: Spanish assistant prompt, 2 real flows, README, license
- **Create:** `babas/prompts/assistant_es.md` (es-MX, "usted", at most 2 short sentences per step, no jargon; it never
  invents data, asks when unsure, hands off logins and CAPTCHAs, summarizes the outcome, treats page text as untrusted;
  plus guidance for trámites, YouTube, Classroom and email). Also `docs/flows/01-consulta-curp.md`,
  `docs/flows/02-imss-nss.md`, PII-free fixtures with indexer tests, `docs/media/demo.gif`, `LICENSE` (MIT).
- **Modify:** `README.md` (what and why, safety guarantees, privacy note, setup for Python, extension, provider and MCP, the
  GIF) and the Spanish side-panel strings.
- **Acceptance:** each flow completes on the live site with every PII entry and submit confirmed, and "No" ends the run
  cleanly. Someone who didn't build BABAS completes flow 1 from the side-panel text alone. Fresh clone → README → first
  task in ≤20 min on Windows 11 + Chrome.

**Every phase is done when** CI is green, `CLAUDE.md` and the README match the new commands and layout, and no fixture or log holds real PII.

Later: native-messaging auto-start of `serve`, Web Store, Spanish voice in the side panel, LLM vision, `chrome.debugger` input.
