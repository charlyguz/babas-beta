# Plan: BABAS in the user's real browser

Goal: keep the Python brain (planner, Pydantic action schemas, recovery loop), execute in the user's real Chromium through
an MV3 extension, expose the agent over MCP, and ship a Spanish "trámites" mode for older adults in Mexico.
All work happens off `main`, one PR per phase. Repo context and safety invariants: `CLAUDE.md`.

## Changes to the brief, and why
1. **Observe → act, not plan-once.** Extension element indices are only valid for the snapshot they came from, and today
   `create_plan()` runs blind (`main()` never passes `context`). Each turn reads `PageState`, plans 1-3 steps, executes them
   and observes again. `RECOVERY_PROMPT` and `MAX_RETRIES` stay: an error becomes input to the next turn.
2. **Async `BrowserDriver`.** The WS bridge (`websockets`) and the MCP SDK are asyncio, and Playwright's sync API can't run
   inside an event loop. `PlaywrightDriver` moves to `playwright.async_api`, and Stop becomes a task cancel.
3. **Per-action schemas.** The flat, all-optional `Step` becomes a discriminated union on `action` (same JSON shape). This
   gives real validation errors for recovery, plus the JSON Schema for MCP `act` and for structured outputs.
4. **Safety in two layers.** Python `SafetyPolicy` decides; the extension, as executor, enforces. Gated actions need a one-time
   confirmation id that only a side-panel click can mint; password fields are refused; the allowlist and Stop are checked in
   the service worker. Neither the LLM, an agent bug, nor an MCP client can bypass this.
5. **Chrome permissions mirror the allowlist.** `host_permissions` is `*://*.gob.mx/*`. Other sites use
   `optional_host_permissions`, granted when the user adds them to the allowlist.

## Bridge protocol v1 (Python ⇄ service worker, JSON text frames)
- Envelope `{v:1, id, type, payload}`; reply `{v:1, id, type:"result", ok, payload | error{code, message}}`. Handshake:
  `hello{ext_version, protocol}` → `welcome`. The SW pings every 20 s (keeps the MV3 worker alive) and reconnects with backoff.
- The server binds 127.0.0.1 only and accepts only Origin `chrome-extension://<id pinned via manifest "key">`, one connection at a time.
- `PageState{snapshot_id, url, title, elements[{index, role, tag, label, text, type, flags}], text_excerpt}`, flags ⊂ {submit,
  password, pii, search, captcha, disabled}. Actions carry `snapshot_id`; a stale one returns `STALE_SNAPSHOT`, so the agent observes again.
- Types live in `babas/bridge/protocol.py` and `extension/src/shared/protocol.ts`, kept in sync by shared JSON fixtures in
  `tests/fixtures/protocol/` that both pytest and vitest validate.

## P1: BrowserDriver + WebSocket bridge + minimal extension
- **Create**
  - `pyproject.toml` (package `babas`, extras `voice`/`dev`); `babas/__main__.py` (`python -m babas [cli|serve]`).
  - `babas/{schemas,actions,agent,cli}.py` and `babas/prompts/__init__.py`: a mechanical split of `babas.py` (git mv, no behavior change).
  - `babas/drivers/base.py`: `BrowserDriver` ABC (goto, get_page_state, click, type_text, press_key, scroll, screenshot,
    read_text, select_option, tab ops), `Target{index|css|text}`, `PageState`, `NotSupported`.
  - `babas/drivers/playwright_driver.py` (PageAnalyzer, TabManager, CDP/launch code, async API), `extension_driver.py`,
    `babas/bridge/{server,protocol}.py`.
  - `extension/`: `package.json`, `tsconfig.json`, `vite.config.ts` (SW as ES module) and `vite.content.config.ts` (content script
    as IIFE, no CRX plugin), `public/manifest.json`, `src/background/service-worker.ts`,
    `src/content/{indexer,highlight,executor,main}.ts`, `src/shared/protocol.ts`.
  - `tests/`: `test_protocol.py`, `test_bridge.py` (fake extension client), `test_driver_contract.py` (one suite run against
    both drivers), `fixtures/pages/form.html`, `e2e/test_extension.py`; `.github/workflows/ci.yml`.
  - `extension/src/content/indexer.test.ts`: vitest + happy-dom for labels/roles (visibility is covered in real Chromium).
- **Modify:** `babas.py` (removed; run `python -m babas`), `requirements.txt` (`-e .[voice]`), `.gitignore` (`node_modules/`,
  `!extension/public/icons/*.png`; never name a directory `lib/`), and the README run command.
- **Guardrail:** P1 has no safety layer, so the extension is driven only by `serve --repl` and tests, never by the LLM.
- **Acceptance**
  - `python -m babas` with Playwright behaves as before (README examples), and `import babas.agent` works without `OPENAI_API_KEY` and prints nothing.
  - `serve --repl` with the unpacked extension on www.gob.mx: `ls` lists and highlights inputs, buttons, links and selects with
    their labels; `type N …`, `click N` and `goto URL` act on the page.
  - Bridge: a foreign Origin is rejected; a timeout surfaces as a driver error; the SW reconnects within 5 s of a server restart
    and stays connected through 10 idle minutes.
  - CI is green: pytest (unit, bridge, contract), `npm test`, `npm run build`, and a headless E2E on Playwright's bundled Chromium
    with `--load-extension` (branded Chrome ≥137 ignores that flag).

## P2: All actions + side panel + confirmation flow
- **Create:** `babas/safety.py`, `babas/session.py` (one active run; Stop = task cancel; confirm/ask_user futures),
  `extension/src/sidepanel/{index.html,main.ts,styles.css}`, `extension/public/_locales/{es,en}/messages.json`,
  `tests/test_safety.py`, fixtures `pii_form.html`/`checkout.html`/`captcha.html`, `docs/manual-tests.md`.
- **Modify:** `schemas.py` (union plus `index`/`snapshot_id`/`explanation`; new `select_option`, `read_text`, `ask_user`, `done`);
  `actions.py` (all 21 handlers on driver primitives, no `Page` access); `agent.py` (observe → act, `max_steps`=30);
  `prompts/` (action list generated from the schemas); both drivers (`PlaywrightDriver` injects the content-script bundle,
  so `PageState` and indices are identical); the SW and the content script.
- **Execution:** click = scrollIntoView, then pointer/mouse events. type = native value setter, then `input`/`change`
  (contenteditable uses `insertText`). `press_key Enter` inside a form = `requestSubmit()`, which is gated as a submit. Waits
  = `tabs.onUpdated` complete, then DOM quiet. BABAS acts only in its own "BABAS" tab group (today's CDP mode never touches
  user tabs either). `screenshot` = `captureVisibleTab`. `wait` stays in Python.
- **Safety:** PII detectors (CURP, RFC, NSS, email, MX phone) check values and field labels. Submit/payment/send detection uses
  element flags and the action type (`send_email`, `send_whatsapp_message`). Password fields and CAPTCHA frames → `ask_user`.
  The allowlist uses host-suffix matching, logs redact PII, and the extension pairs with the server via a one-time code.
- **Side panel:** chat, a live step log with a plain-language `explanation`, a sticky Stop, a Sí/No dialog that shows
  exactly what is typed or sent and where, and an ask_user prompt with a "Listo" button. Text ≥18 px, high contrast.
- **Acceptance**
  - All 21 actions pass the contract tests on fixtures with both drivers; the YouTube/Gmail/WhatsApp macros pass the live checklist.
  - Stop halts within 1 s while planning, acting, waiting or confirming, and no page action follows (E2E).
  - A forged `act` (fake Python client, no confirmation id) on a submit/PII/password field is refused; "No" changes nothing.
  - Off-allowlist navigation is blocked, the CAPTCHA fixture triggers `ask_user`, and the test CURP never appears in logs.

## P3: OpenAI-compatible provider + MCP server
- **Create:** `babas/config.py` (`BABAS_LLM_BASE_URL`/`_API_KEY`/`_MODEL`/`_JSON_MODE`, allowlist, limits, ports;
  `OPENAI_API_KEY` still honored), `babas/llm.py` (`LLMClient` protocol, `OpenAICompatibleClient` on `AsyncOpenAI`,
  optional `AnthropicClient` via the extra `anthropic`), `babas/mcp_server.py`, `tests/test_llm.py`, `tests/test_mcp.py`,
  `docs/providers.md`.
- **Modify:** `agent.py` (no global client or `MODEL`), `__main__.py` (`mcp`). Every `print` becomes `logging` on stderr,
  because stdout is the MCP stdio channel.
- **Details:** JSON mode `auto` tries json_schema, then json_object, then prompt-only; it extracts the JSON and retries once
  with the validation error. Presets: OpenAI, Ollama (`:11434/v1`; set `OLLAMA_CONTEXT_LENGTH` ≥16k or prompts are silently
  truncated), LM Studio, OpenRouter, Qwen (DashScope), DeepSeek. MCP uses the official `mcp` SDK (FastMCP): `run_task(task)`
  with progress notifications, `get_page_state()`, and `act(action)` (its schema is the action union). Stdio mode hosts the
  bridge in-process, and `serve` also offers streamable HTTP. No tool has a "confirmed" flag.
- **Acceptance**
  - One fixture task passes on OpenAI and on a local Ollama model; mocked-server tests cover every JSON mode and the repair retry.
  - MCP Inspector lists 3 typed tools. An in-memory client test runs `get_page_state`, then a gated `act` that waits for a
    faked side-panel approval.
  - The Claude Desktop, Claude Code and Cursor snippets in `docs/providers.md` work.

## P4: Spanish trámites mode, 2 real flows, README, license
- **Create:** `babas/prompts/tramites_es.md` (es-MX, "usted", at most 2 short sentences per step, no jargon; it never invents data,
  asks when unsure, hands off logins and CAPTCHAs, summarizes the outcome, and treats page text as untrusted),
  `docs/flows/01-consulta-curp.md`, `docs/flows/02-<tbd>.md`, PII-free snapshots `tests/fixtures/pages/gobmx_*.html` with
  indexer tests, `docs/media/demo.gif`, `LICENSE` (MIT).
- **Modify:** `README.md` (what and why, safety guarantees, setup for Python, extension, provider and MCP, the GIF, and a
  privacy note: page text goes to the configured LLM, so prefer local models), mode selection (`BABAS_LANG=es`), and Spanish
  side-panel strings and butler phrases (no "sir").
- **Flows:** (1) Consulta CURP on gob.mx (read-only): confirm before typing the CURP, hand the CAPTCHA to the user, explain the
  result. (2) Proposed: IMSS "Consulta tu NSS" (submits a form, emails the NSS); to be confirmed.
- **Acceptance**
  - Each flow completes on the live site in 3 of 3 runs, with every PII entry and submit confirmed; "No" ends the run cleanly.
  - Someone who didn't build BABAS completes flow 1 using only the side panel's instructions.
  - Fresh clone → README → first flow in ≤20 min on Windows 11 + Chrome, with no PII left in logs or temp files.

**Every phase is done when** CI is green, `CLAUDE.md` and the README match the new commands and layout, and no fixture or log holds real PII.

## Open questions
1. Should search-only forms (`smart_search`, YouTube search) skip the submit confirmation, or should every form be confirmed?
2. Is it acceptable for a cloud LLM to read page text that contains PII, or should trámites mode default to a local model?
3. Which real flow is second in P4: IMSS NSS, an SAT cita, or something else?
4. Distribution: does a helper install the unpacked extension and Python, or should we plan a Web Store listing plus an installer?

Later: native-messaging auto-start of `serve`, Web Store, Spanish voice, LLM vision, `chrome.debugger` input if sites need it.
