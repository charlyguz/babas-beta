# BABAS protocol reference

Three channels exist. The **bridge** connects Python and the service worker (SW) over a WebSocket. The **panel port**
connects the side panel and the SW (`chrome.runtime.connect`). **Page commands** go from the SW to the content script,
or from Python to `page_script.js` in the Playwright driver.

Source of truth in code:
- `babas/bridge/protocol.py`: Python models.
- `extension/src/shared/protocol.ts`: TS types and guards.
- `extension/src/shared/messages.ts`: panel-port types (created in P2).

If you change a message, update this file, both code files and `tests/fixtures/protocol/` in the same commit.
Items marked **(P2)** do not exist yet.

## 1. Bridge (Python ⇄ SW)

- Python listens on `ws://127.0.0.1:8765` (config `BABAS_BRIDGE_PORT`), binds 127.0.0.1 only, and accepts only the
  Origin `chrome-extension://ooflejhodolbaphddedikkpleeephhmp`. The ID is pinned by the `key` in `extension/public/manifest.json`.
  Override with `BABAS_EXTENSION_ORIGINS` (comma-separated).
- The SW reads the port from `chrome.storage.local.bridgePort` (default 8765). On close it reconnects with backoff
  (0.5 s, doubling to a 5 s cap), and an alarm wakes it every 30 s to retry.
- Only one extension connection is active. A new authenticated connection replaces the old one, and the old one's
  pending requests fail with `NO_EXTENSION`.
- Frames are JSON text. Every frame is either an **envelope** `{v:1, id, type, payload}` or a **result**
  `{v:1, id, type:"result", ok, payload | error:{code, message, data?}}`. A result echoes the request's `id`.

### Handshake
1. SW → `hello {ext_version, protocol: 1, token?}`. The server waits 5 s for it, otherwise it closes with 1008.
2. The server checks `protocol == 1`. (P2) It also checks pairing: see §4.
3. Server → `welcome {server_version, token?}`. `token` is only sent when a new pairing was just created (P2).
4. The SW sends `ping {}` every 20 s; the server ignores it. This keeps the MV3 worker alive.

### Requests Python → SW (each answered with a result)
| type | payload | result payload |
|---|---|---|
| `page.state` | `{highlight?: bool, max_elements?: 250, max_text?: 2000}` | `PageState` + `{tab_id}` |
| `page.describe` | `{target}` | `{element: ElementInfo, host, url}` |
| `page.exists` | `{target}` | `{exists: bool, visible: bool}` |
| `page.act` | `ActPayload` (below) | `{result: {...}, tab_id, url, title}` |
| `tab.goto` | `{url}` (http/https only) | `{tab_id, url, title}` after load + DOM quiet |
| `tab.info` | `{}` | `{tab_id, url, title}` |
| `tab.new` | `{url?}` | `{tab_id, url, title}` (the new tab becomes current) |
| `tab.switch` | `{tab_id}` | `{tab_id, url, title}` |
| `tab.close` | `{tab_id?}` (default current) | `{closed: tab_id}` |
| `tab.screenshot` | `{}` | `{png_base64}` (visible area of the current tab) |
| `ui.confirm` (P2) | `{actions: [ActPayload], reasons: [str], explanation: str, timeout_s?: 300}` | `{approved: bool, confirmation_id?: str, reason?: "denied"\|"timeout"\|"stopped"}` |
| `ui.ask` (P2) | `{question: str, kind: "text"\|"handoff", timeout_s?: 600}` | `{answer: str\|null, reason?: "cancelled"\|"timeout"\|"stopped"}` |
| `ui.allow_site` (P2) | `{host, url, timeout_s?: 300}` | `{approved: bool, reason?}` |
| `ui.log` (P2) | `{kind: "assistant"\|"step"\|"result"\|"error"\|"info", text, ok?: bool}` | `{}` |
| `ui.run_state` (P2) | `{state: "running"\|"done"\|"failed"\|"stopped", summary?: str}` | `{}` |

`ActPayload` = `{op, target?, text?, key?, value?, direction?, amount?, max_chars?, mode?, confirmation_id?}`:
| op | required | result |
|---|---|---|
| `click` | `target` | `{label}`, or `{navigated: true}` if the page unloaded first |
| `type` | `target`, `text` | `{label}` |
| `press` | `key` (`Enter`, `Tab`, `Escape`, `Backspace`, `PageDown`, `PageUp`, or one character); `target` optional | `{submitted: bool}` |
| `select` | `target`, `value` (option value or visible text, accent/case-insensitive) | `{selected}` |
| `scroll` | `direction` `up`\|`down`, `amount` px (default 500) | `{scrolled, y}` |
| `read` | optional `target`, `max_chars` (6000) | `{text, truncated}` |
| `highlight` | `mode` `all`\|`off`\|`target` (+`target`) | `{mode}` |

After `click`, `press` and `select`, the SW waits for the page to settle: 250 ms, then load complete, then up to 2 s of DOM quiet.

`Target` = `{index?, snapshot_id?, selector?, text?}`, and at least one of `index`, `selector` or `text` is required.
- `index` + `snapshot_id`: an element from that snapshot. A wrong or old snapshot gives `STALE_SNAPSHOT`.
- `selector`: CSS, searching open shadow roots too. Two Playwright-style forms are also accepted: `BASE:has-text('x')`
  and `text=x` / `text='x'` (exact).
- `text`: the best visible interactive element whose label or text matches (exact > prefix > contains), else the
  deepest visible element containing the text.

### Events SW → Python (no result)
| type | payload | when |
|---|---|---|
| `ping` | `{}` | every 20 s |
| `task.start` (P2) | `{text}` | the user sends a task from the side panel |
| `run.stop` (P2) | `{}` | the user presses Stop |

### Shapes
`ElementInfo` = `{index, tag, role, label, text, type?, value?, href?, options?, checked?, flags: Flag[], in_viewport}`.
`value` is never set for password fields. `options` holds the first 25 option texts of a `<select>`.

`Flag` ∈ `submit` (submit control of a form), `search_form` (search-only form or search field), `consequential`
(label is a consequential verb), `password`, `pii`, `payment`, `captcha`, `disabled`. Exact rules: `docs/SAFETY.md`.

`PageState` = `{snapshot_id, url, title, elements: ElementInfo[], text (visible text excerpt), has_captcha, has_password, truncated}`.

## 2. Error codes
| code | raised by | meaning, and what the agent does |
|---|---|---|
| `STALE_SNAPSHOT` | page | Index from an old snapshot or a detached element. Observe again. |
| `NOT_FOUND` | page | No element matches. Re-plan. |
| `NOT_VISIBLE` | page | Element is hidden. Scroll or re-plan. |
| `OBSCURED` | page | Something covers it (`data.blocker`). Close that first. |
| `DISABLED` | page | Element is disabled. Fill the required fields first. |
| `NOT_EDITABLE` | page | Not a text field, or the value was rejected. |
| `PASSWORD_FIELD` | page, gate | Typing into a password field is refused. Hand off to the user. |
| `CONFIRMATION_REQUIRED` | page | Gated action without a valid confirmation (`data.reasons`, `data.label`). The gate asks the user, then retries once. |
| `DOMAIN_NOT_ALLOWED` | SW (P2) | Host not in the allowlist (`data.host`, `data.url`). The gate asks to allow the site. |
| `HALTED` | SW (P2) | The user pressed Stop. The run ends. |
| `TIMEOUT` | SW, bridge | Something took too long. Counts as a failure. |
| `NO_EXTENSION` | bridge | The extension is not connected. Tell the user to open Chrome. |
| `TAB_NOT_FOUND` | SW | The tab is gone or outside BABAS's tabs. |
| `UNSCRIPTABLE` | SW | chrome://, Web Store or similar page. Use `goto` first. |
| `NAVIGATION_FAILED` | drivers | Navigation error. |
| `BAD_REQUEST` | all | Malformed request. This is a bug: fix the caller. |
| `UNSUPPORTED` | page, drivers | Operation not possible here (for example, select on a non-`<select>`). |
| `INTERNAL` | all | Unexpected error. Logged, and counts as a failure. |

## 3. Panel port (side panel ⇄ SW) (P2)
The panel calls `chrome.runtime.connect({name: "sidepanel"})`. The SW accepts the port only if
`port.sender.url` starts with `chrome.runtime.getURL("sidepanel.html")`. Approvals arrive only through this port.

Panel → SW: `{type:"task.start", text, windowId}`, `{type:"stop"}`, `{type:"resume"}`,
`{type:"confirm.answer", request_id, approved}`, `{type:"ask.answer", request_id, answer|null}`,
`{type:"allow.answer", request_id, approved}`, `{type:"settings.get"}`, `{type:"settings.set", bridgePort?, removeSite?}`,
`{type:"voice.audio", request_id, audio_base64, duration_ms}` (section 6).

SW → panel: `{type:"status", connected, halted, running}`,
`{type:"log", entry:{kind, text, ok?, ts}}`,
`{type:"confirm", request_id, title, lines: string[], explanation}`,
`{type:"ask", request_id, question, kind}`,
`{type:"allow", request_id, host, url}`,
`{type:"request.closed", request_id}` (answered elsewhere, timed out or stopped),
`{type:"run_state", state, summary?}`,
`{type:"settings", bridgePort, sites: string[]}`,
`{type:"voice.result", request_id, text}`, `{type:"voice.error", request_id, code, message}` (section 6).

When a panel connects, the SW immediately sends `status`, `settings` and every still-pending confirm, ask or allow request.

## 4. Flows (P2)
**Confirmation**
1. Python: `ui.confirm{actions, reasons, explanation}`.
2. SW: for each action, `describe` its target and build a factual Spanish line (`docs/SAFETY.md` §4). The panel shows
   the card; if no panel is open, the action badge shows "!" and the request waits.
3. The user clicks "Sí". The SW mints `confirmation_id = crypto.randomUUID()` and stores
   `{tabId, digests: Set(actionDigest(tabId, a)), expiresAt: now + 120 s}`.
4. Python sends `page.act` with that `confirmation_id`. The SW recomputes the digest. If it is in the set, it removes the
   digest (one-time use) and runs the command with `authorized: true`; otherwise it runs with `authorized: false`, and the
   content script refuses gated actions.

`actionDigest(tabId, a)` joins these fields with `"␟"`:
`tabId, a.op, target.snapshot_id ?? "", target.index ?? "", target.selector ?? "", target.text ?? "", a.text ?? "", a.value ?? "", a.key ?? ""`.

**Allow site:** `ui.allow_site` shows a card. On "Permitir", the SW appends the host (lowercase, without `www.`) to
`chrome.storage.local.allowedSites` and answers `{approved: true}`. Python also remembers the host for the session.

**Stop:** panel `stop` → the SW sets `halted = true` (stored in `chrome.storage.session`), clears approvals, closes every
pending ui request with `reason: "stopped"`, emits `run.stop`, and clears the page highlights. While halted, every
`page.*` and `tab.*` request fails with `HALTED`. `task.start` or `resume` clears the halt.

**Pairing (trust on first use)**
- The server keeps `$BABAS_HOME/pairing.json` (default `~/.babas`, mode 0600) as `{"token": "..."}`.
- If no file exists, the first valid `hello` is accepted, a token is generated with `secrets.token_urlsafe(32)` and
  saved, and it is sent in `welcome.token`. The SW stores it as `chrome.storage.local.pairToken` and sends it in every later `hello`.
- If the file exists, `hello.token` must match (`hmac.compare_digest`); otherwise the server closes with 1008 `not paired`.
- `python -m babas pair --reset` deletes the file.

## 5. Service worker behavior as built (P2)
- Rules for `page.*` and `tab.*` in order: Stop (`HALTED`), working tab must be a BABAS tab (else `TAB_NOT_FOUND`), allowlist
  (`DOMAIN_NOT_ALLOWED` with `{host, url}`; `host` is `''` for `file:`/`ftp:`), then, for `page.act`, the approval.
- Exempt from the allowlist (they read no page content): `tab.info`, `tab.switch`, `tab.close`, `tab.new` without a URL.
  Still subject to Stop. `tab.switch`/`tab.close` need a BABAS tab; `tab.close` without `tab_id` closes only the current
  BABAS tab. `tab.new` with a non-http(s) URL is `BAD_REQUEST`. The panel is never adopted as the working tab.
- `allowedSites` defaults (`gob.mx`, `google.com`, `google.com.mx`, `youtube.com`, `whatsapp.com`, `wikipedia.org`) apply
  while the storage key is unset; the first add/remove materializes the list.
- `ui.confirm` replies `{approved: true, confirmation_id}` or `{approved: false, reason: "denied"|"timeout"|"stopped"}`;
  `ui.ask` replies `{answer: string}` (the handoff "Listo" button sends a string) or `{answer: null, reason}`;
  `ui.allow_site` replies `{approved: true}` or `{approved: false, reason}`. A halted worker answers all three negatively
  at once. `ui.run_state` "running" is ignored while halted.
- Approvals: `{tabId, host, digests[], expiresAt}`; `consumeApproval` is atomic; the digest escapes `\` and U+241F
  inside fields (identical to the section 4 string for ordinary values). The authorization is consumed in a `prepare`
  hook right before the page command is sent, after re-checking Stop and the allowlist on the current URL.
- Pending UI requests are capped at 50 (more are denied), and all open cards are resolved as stopped when the bridge drops.
- Pairing close: the server answers a wrong/missing token with 1008 "not paired" (the extension keeps retrying every
  <= 5 s); a corrupt/unwritable pairing file is rejected the same way and only the server log says why.

## 6. Voice input (dictation into the side panel)
The panel records the user's voice after a click, the Python side transcribes it with the configured speech-to-text
backend (`babas/stt.py`), and the transcript only **fills the text box**: it never starts a task by itself.

**Panel → SW** (port): `{type:"voice.audio", request_id, audio_base64, duration_ms}`. `request_id` is 1-128 characters,
`audio_base64` is a 16 kHz mono PCM16 WAV (at most 30 s, so <= 1.5 M characters of base64), `duration_ms` is an integer
1..30000. Anything else is rejected by the guard.

**SW → panel** (port): `{type:"voice.result", request_id, text}` or `{type:"voice.error", request_id, code, message}`.
`code` ∈ `no_connection` (bridge down), `timeout` (no answer in 90 s), `busy` (another transcription is running),
`not_configured` (no usable speech backend), `provider` (the backend failed), `bad_audio`, `empty` (no speech found).
`message` is plain es-MX text that is safe to show (never provider text, keys or audio).

**SW → Python** (bridge event): `stt.transcribe {request_id, audio_base64, mime:"audio/wav", duration_ms}`.
**Python → SW** (bridge request, answered with `{}`): `ui.transcript {request_id, text}` on success, or
`ui.transcript {request_id, error:{code, message}}` with the same codes. Exactly one of `text` / `error` is present;
`text` is at most 4000 characters. The SW forwards it to the panel as `voice.result` / `voice.error`.

Rules: one transcription at a time per Python process (a second one gets `busy`); the audio and the transcript are
never persisted by the SW or written to logs (only sizes and durations are logged); Python validates every field
(`mime` must be `audio/wav`, decoded size <= 3 MiB) and answers every `stt.transcribe` it can parse, even on failure.
The frame limit of the bridge (16 MiB) already covers the audio.

As built (voice):
- The panel records with a `ScriptProcessorNode` and sends a canonical 44-byte-header PCM16 mono 16 kHz WAV (at most 960,044
  bytes / 30 s). It stops by itself after 2 s of quiet once speech was heard, and refuses recordings under 300 ms locally.
- Failures that never reach Python are shown from `_locales` by local code (`permission`, `no_device`, `mic_busy`, `capture`);
  the panel shows its own text for every code and ignores the `message` string. A 100 s watchdog in the panel ends a stuck
  transcription with `timeout`.
- The SW keeps at most 8 transcriptions in flight (`busy` beyond that), forgets them when their port closes, and answers
  `no_connection` when the bridge drops.
- Python releases `busy` as soon as the backend returns, before sending `ui.transcript`. HTTP backends give up after about 62 s;
  the first use of the `local` backend may download the model and exceed the SW's 90 s, so pre-download it (`docs/providers.md`).
- `micpermission.html` is an extension page opened in a tab with `chrome.tabs.create` when the side panel cannot get the
  microphone (a side panel may not show the permission prompt); it asks once on a click, stops the tracks and tells the person to
  go back to BABAS. It is not web-accessible.

