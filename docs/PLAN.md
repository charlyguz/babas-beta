# Plan: BABAS in the user's real browser

Goal: keep the Python brain (planner, Pydantic action schemas, recovery loop), execute in the user's real Chromium through
an MV3 extension, and expose the agent over MCP. BABAS is a general-purpose assistant: YouTube, web search, Google
Classroom, Gmail, WhatsApp, and government procedures (SAT, IMSS, CURP). It is built for people with low digital
literacy (older adults in Mexico) and explains every step in plain Spanish.

**How to execute:** follow `docs/tasks/P1.md` → `P2.md` → `P3.md` → `P4.md` step by step. They list the files,
interfaces, verification commands and commit points. The specs are `docs/PROTOCOL.md` (every message) and
`docs/SAFETY.md` (normative safety rules). The agent workflow rules are in `CLAUDE.md`.

## Status
| Phase | State | Notes |
|---|---|---|
| P1 | done | Package split, `PlaywrightDriver`, `ExtensionDriver`, bridge, `serve --repl`, 169 vitest + 79 pytest tests, CI workflow (green on `dcac297`). See "As built" in `docs/tasks/P1.md`. Not done: independent code review of P1 (started, then stopped). Next: P2 |
| P2 | done | Action union, LLM seam, safety core, channels, `SafeDriver` gate, actions on primitives, observe → act `Agent`, SW enforcement (approvals, Stop, allowlist, pairing), Spanish side panel, `Session`, `serve`/`pair`/CLI on the new agent; 386 vitest + 967 pytest (75 e2e) green. See "As built" in `docs/tasks/P2.md`. Not done: real LLM/site runs (manual: `docs/manual-tests.md`), independent review. Next: P3 |
| P3 | not started | |
| P4 | not started | Live flows need the user (P4.7) |

## Decisions (answered 2026-10-11)
- `babas.py` becomes the `babas/` package; run it with `python -m babas`.
- Search-only forms (`role=search`, `type=search`, a single query box) skip the submit confirmation, to avoid confirmation fatigue.
- Cloud LLMs may see page text, including personal data. Local models stay optional, and the README carries a privacy note.
- P4 flows: (1) Consulta CURP (read-only); (2) IMSS "Consulta tu NSS" (submits a form, emails the NSS).
- Distribution for now: a helper installs Python and the unpacked extension. The Web Store and an installer come later.
- Implementation is executed by a cheaper agent from the step playbooks. The verified P1 scaffold is kept.
- Pairing is trust-on-first-use (no code to type). A "No" from the user ends the run cleanly.

## Changes to the brief, and why
1. **Observe → act, not plan-once.** Extension element indices are only valid for the snapshot they came from, and the
   legacy `create_plan()` ran blind (`main()` never passed `context`). Each turn reads `PageState`, plans 1-3 steps,
   executes them and observes again. Recovery stays: an error becomes input to the next turn.
2. **Async `BrowserDriver`.** The WS bridge (`websockets`) and the MCP SDK are asyncio, and Playwright's sync API can't run
   inside an event loop. `PlaywrightDriver` moves to `playwright.async_api`, and Stop becomes a task cancel.
3. **Per-action schemas.** The flat, all-optional `Step` becomes a discriminated union on `action` (same JSON shape). This
   gives real validation errors for recovery, plus the JSON Schema for MCP `act` and for structured outputs.
4. **Safety in two layers.** Python `SafetyPolicy` decides; the extension, as executor, enforces. Gated actions need a
   one-time confirmation id that only a side-panel click can mint, bound to the exact action. Password fields are
   refused, and the allowlist and Stop are checked in the service worker.
5. **General-purpose allowlist.** `host_permissions` is `<all_urls>` and content scripts are injected on demand. BABAS's
   own allowlist ships with common sites (gob.mx, google.com, google.com.mx, youtube.com, whatsapp.com, wikipedia.org)
   and asks in the side panel before using a new site. The extension stores approvals; Python cannot add sites on its own.
6. **Consequential clicks, not just forms.** Gmail's "Enviar" or Classroom's "Entregar" are buttons, not form submits.
   Buttons whose label is a consequential verb need confirmation. The LLM may also mark a step `consequential`: either
   signal triggers a confirmation.

## Phases
### P1: BrowserDriver + WebSocket bridge + minimal extension → `docs/tasks/P1.md`
Package split (async), `PlaywrightDriver`, `ExtensionDriver`, `serve --repl`, tests (bridge, protocol, contract on
both drivers, vitest), CI.
Acceptance:
- the CLI behaves as before;
- `import babas.agent` needs no key;
- the REPL lists, highlights, clicks and types on a real page through the extension;
- a foreign Origin is rejected;
- CI is green, and the committed `page_script.js` is current.

### P2: All actions + side panel + confirmation flow → `docs/tasks/P2.md`
Action union, LLM seam, safety core, user channels, gate (`SafeDriver`), actions on primitives, the observe → act agent,
SW enforcement (halt, allowlist, approvals, tab scope, pairing), the Spanish side panel, a serve-mode session, the CLI on
the new agent, E2E tests, and the manual checklist.
Acceptance:
- all 25 actions are tested;
- Stop takes effect within 1 s;
- forged or reused confirmations are refused;
- "No" ends the run;
- search-only forms run without a prompt, and unknown sites prompt;
- CAPTCHA and password trigger a handoff;
- no raw PII appears in logs.

### P3: OpenAI-compatible provider + MCP server → `docs/tasks/P3.md`
`Settings` from env, `OpenAICompatibleClient` (JSON modes with automatic fallback), an optional `AnthropicClient`,
logging on stderr with redaction, and the MCP server (`run_task`, `get_page_state`, `act`) over stdio and streamable
HTTP, using the mcp 2.x API.
Acceptance:
- the JSON-mode fallback is tested against a fake server;
- an in-memory MCP client runs a gated `act` that waits for approval;
- stdout stays clean;
- provider and MCP docs are written.

### P4: Spanish assistant prompt, 2 real flows, README, license → `docs/tasks/P4.md`
Spanish prompt (general tasks plus trámites guidance), flow docs, fixture rehearsals of both flows, README (Spanish
first), MIT `LICENSE`, and a demo GIF from a rehearsal run.
Acceptance:
- the rehearsals pass with the expected confirmations;
- the user runs both live flows and the manual checklist and reports success;
- a fresh clone runs its first task in ≤20 min.

## Definition of done (every phase)
- The step checkboxes are ticked.
- All tests pass locally (`pytest -q`, `npm test`, `npm run typecheck`, `npm run build`), and CI is green.
- `CLAUDE.md` (Status, Layout, Commands) and the README match reality.
- No secrets or real PII are in the repo.
- Every step is committed, and the branch is pushed.

## Later (not planned)
Native-messaging auto-start of `serve`, Web Store packaging, Spanish voice in the side panel (Whisper `es`, Spanish
butler), LLM vision (screenshots), `chrome.debugger` trusted input for sites that ignore synthetic events, and same-origin
iframe support in the indexer.
