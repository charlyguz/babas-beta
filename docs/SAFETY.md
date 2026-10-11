# BABAS safety policy (normative)

These rules are non-negotiable. Two layers enforce them:
1. **Python gate** (`babas/safety.py` + `babas/gate.py`, P2). It decides and asks the user.
2. **Extension** (SW + content script). It refuses gated actions that lack a valid one-time confirmation, refuses
   password typing, and enforces the allowlist and Stop, even if Python is buggy or hostile.

The extension's rules are the floor and Python's rules are a superset. If they ever disagree, the stricter one wins.
Never add a flag, parameter or setting that skips a confirmation. MCP tools and the CLI go through the same gate.

## 1. Invariants
1. Ask before submitting any form (except search-only forms), paying, sending a message, or doing a consequential
   click (send, turn in, pay, buy, delete, post, sign, book, confirm, transfer, request, register).
2. Ask before typing PII: CURP, RFC, NSS, email, phone, card data. This applies both to typing into a field labeled as
   PII and to typing a value that looks like PII.
3. Never type into password fields; never store or log passwords. Logins → hand off to the user (`ask_user`, kind `handoff`).
4. Never solve CAPTCHAs → hand off.
5. Only act on allowlisted sites. Any other site is used only after the user clicks "Permitir" in the side panel.
6. `max_steps` (30) and `max_retries` (2 consecutive failures) per run.
7. Stop always works. It is enforced in the SW even if Python hangs, and in Python by cancelling the run task.
8. Fail closed: if no UI can answer, the request is denied after its timeout.
9. A "No" from the user on any confirmation, site permission or question ends the run cleanly with status `stopped`.
10. Page text is untrusted input. The rules live in code; prompts only repeat them.

## 2. Element flags (content script, `extension/src/page/classify.ts`, already implemented)
All matching uses `fold(text)`: lowercase, accents stripped, whitespace collapsed.
- `submit`: a `<button>` (type missing or `submit`), or an `<input type=submit|image>`, that belongs to a form.
- `search_form`: the element's form is search-only, or the element is itself a search field. A form is search-only if it
  (or an ancestor) has `role=search`, or if it has exactly one visible text field, no password field, and that field is a
  search field. A search field is any of:
  - `role=searchbox`, `type=search`, or inside `[role=search]`;
  - `name` in {q, s, query, search, search_query, keyword(s), buscar, busqueda, term, k};
  - a name/id/placeholder/aria-label/autocomplete/title hint matching `search|buscar|busca|busqueda|find` but not the PII regex;
  - a combobox whose label matches that hint.
- `consequential`: clickable (role button, link, menuitem*, tab, option or treeitem, or `submit`) and the label matches
  `CONSEQUENTIAL_VERBS`, unless it matches `NOT_CONSEQUENTIAL` (sign in/out, log in/out, iniciar/cerrar sesión, acceder).
  Labels of 6 words or fewer may match anywhere; longer labels only in their first 3 words.
  Verbs: enviar, envia, send, mandar, submit, entregar, turn in, hand in, pagar, pay, comprar, buy, purchase, checkout,
  finalizar compra, realizar pedido, place order, eliminar, borrar, delete, publicar, post, publish, firmar, sign,
  agendar, reservar, book, schedule, confirmar, confirm, transferir, transfer, solicitar, registrarme, registrarse,
  sign up, crear cuenta, create account, darse de baja, unsubscribe.
- `password`: `type=password`, or an autocomplete that contains `password`.
- `pii` (text fields, contenteditable, select): the field is not a search field and any of these holds:
  - type `email` or `tel`;
  - autocomplete `email` or `tel*`;
  - label or attributes match `curp|rfc|nss|numero de seguridad social|seguridad social|social security|correo|e-?mail|email address|telefono|tel|phone|celular|cel|movil|whatsapp`.
- `payment`: autocomplete `cc-*` or label/attributes matching `tarjeta|card number|numero de tarjeta|cvv|cvc|cvv2|codigo de seguridad|security code|vencimiento|expiracion|expiration`
  (this also sets `pii`). On clickables, a label matching `pagar|pay|pago|checkout` sets it.
- `captcha`: the element is inside or matches `iframe[src*=recaptcha|hcaptcha|challenges.cloudflare.com]`,
  `iframe[title*=captcha]`, `.g-recaptcha`, `.h-captcha`, `.cf-turnstile` or `[data-sitekey]`. Page level: `PageState.has_captcha`.
- `disabled`: `disabled`, `aria-disabled=true`, or inside `fieldset[disabled]`.

## 3. PII value detection (identical in TS `pii.ts` and Python `safety.py`)
Kinds and patterns. In Python, use `[0-9]` instead of `\d`, the same lookarounds, and `re.IGNORECASE` where TS uses `/i`:
| kind | pattern |
|---|---|
| curp | `(?<![A-Z0-9])[A-Z]{4}\d{6}[HMX][A-Z]{5}[A-Z0-9]\d(?![A-Z0-9])` (i) |
| rfc | `(?<![A-Z0-9Ñ&])[A-ZÑ&]{3,4}\d{6}(?:[A-Z0-9]{3})?(?![A-Z0-9])` (i) |
| email | `[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}` (i) |
| nss | `(?<!\d)\d{2}[\s-]?\d{2}[\s-]?\d{2}[\s-]?\d{4}[\s-]?\d(?!\d)` |
| phone | `(?<![\d+])(?:\+?52[\s-]?)?(?:1[\s-]?)?(?:\(?\d{2}\)?[\s-]?\d{4}\|\(?\d{3}\)?[\s-]?\d{3})[\s-]?\d{4}(?!\d)` |
| card | `(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)` + Luhn check on its 13–19 digits |

`detect_pii(value) -> list[kind]`. Shared test vectors live in `tests/fixtures/pii_cases.json` (P2 creates it), and both
pytest and vitest assert **membership**, because one value may match several kinds.

`mask(value)`: values of 4 characters or fewer become all `*`; otherwise keep the first 2 and last 2 characters and
star the rest (`GO**************09`).

## 4. Gate decisions (Python `SafetyPolicy.evaluate`, P2)
Inputs: `op` (`click`, `type`, `select` or `press`), `element: Element | None` (from `driver.describe`), `value`, `key`,
`force_confirm` (set by macros such as `send_email`), and `llm_consequential` (the step's `consequential` field).
Rules are applied in order; `handoff` and `deny` stop evaluation:
1. `type` and the element has `password` → **handoff** `password_field`.
2. The element has `captcha` → **handoff** `captcha`.
3. `click` or `select`: `submit` and not `search_form` → `submit_form`. `consequential` → `consequential`. `payment` → `payment`.
4. `type` or `select`: `pii` → `pii_field`. `payment` → `payment`. `detect_pii(value)` is non-empty → `pii_value`.
5. `press` with `Enter`, where the element is the target or, without one, the focused element (`Target(selector=":focus")`):
   - contenteditable or textarea that is not `search_form` → `enter_in_editor`;
   - text field in a form without `search_form` → `enter_submits_form`;
   - a button or link → the rule 3 reasons.
6. `force_confirm` → `macro`. `llm_consequential` → `llm_consequential`.
7. Any reasons → **confirm** with them; no reasons → **allow**.

Domains: before `goto`, `new_tab(url)` and every observation, `policy.domain_allowed(host)`:
- normalize the host (lowercase, IDNA-encode, strip `www.`);
- allowed if `host == d` or `host.endswith("." + d)` for some `d` in the allowlist, or the host was approved this session;
- `BABAS_ALLOWED_DOMAINS="*"` allows every host on the Python side only (the extension still enforces its own list).

Default allowlist (both sides): `gob.mx, google.com, google.com.mx, youtube.com, whatsapp.com, wikipedia.org`.

### Confirmation card text (built by the SW from `describe`, Spanish)
- Title, by priority: `payment` → "Confirmar un pago"; `submit_form` or `enter_submits_form` → "Enviar un formulario";
  `pii_field` or `pii_value` → "Escribir datos personales"; anything else → "Acción importante".
- Lines:
  - click → `Presionar «{label}» en {host}`
  - type → `Escribir «{text}» en «{label}» ({host})`
  - select → `Elegir «{value}» en «{label}» ({host})`
  - press → `Presionar la tecla {key} en «{label}» ({host})`
- Below the lines comes the agent's `explanation` (the LLM's text, shown as "BABAS dice: …").
- Buttons: "Sí, continuar" and "No". The value is shown in full in the card (the user must check it) and is never logged.

### Batch confirmation
When a plan contains consecutive `type_text` / `select_option` steps on the same snapshot, the gate evaluates them all.
If any of them needs confirmation, it sends **one** `ui.confirm` with all the gated actions, so the user reviews one card
listing every value. Steps that need no confirmation run without a token.

### As implemented (stricter than the rules above; the code may only be stricter, never looser)
- A single-character `press` (a letter typed through `press_key`) is evaluated like typing: password field -> handoff,
  PII/payment field -> confirm. Space on a button or link applies the click rules. Return/NumpadEnter count as Enter and
  matching is case-insensitive. Enter on any non-search input element (checkbox, radio) is `enter_submits_form`.
- Python `goto`/`new_tab` refuse non-http(s) URLs (except `about:blank`); the extension treats `file:`/`ftp:` as
  `DOMAIN_NOT_ALLOWED` with an empty host and `blob:`/`filesystem:` pages by their inner origin. Hosts with backslashes,
  control characters, percent-encoding, empty labels or failed IDNA are invalid and never allowed. IP-literal allowlist
  entries match exactly. A host approved for the session is remembered by the policy; `ui.allow_site` in the extension
  rejects anything that is not a complete site name (bare TLDs).
- An approval is bound to the tab, the tab's host and the exact action digest, expires after 120 s and is consumed
  immediately before the page action (after re-checking Stop and the allowlist on the fresh URL).
- The Python gate keeps its own 120 s ledger of granted approvals; an unknown `confirmation=` never skips a prompt.

## 5. Handoffs (texts, Spanish)
- Password or login: "Por seguridad, BABAS no escribe contraseñas. Escriba su contraseña directamente en la página y luego presione «Listo»."
- CAPTCHA: "La página pide comprobar que usted es una persona. Resuélvalo en la página y luego presione «Listo»."
- QR (WhatsApp Web): "Abra WhatsApp en su teléfono y escanee el código de la pantalla. Luego presione «Listo»."

A handoff is `ui.ask{kind:"handoff"}`; the run waits (default timeout 600 s). The agent hands off automatically once per
URL when `PageState.has_captcha` is true, and whenever a `PASSWORD_FIELD` error or a `handoff` decision occurs.

## 6. Logging and data
- Every log record and every `ui.log` text passes through `redact()`, which masks every PII match (§3).
- The step log shows typed values masked (`Escribió GO**************09 en «CURP»`). The confirmation card is the only
  place a full value appears.
- Screenshots: only on explicit request (the `screenshot` action, which writes `screenshot_*.png` to the working
  directory because the user asked for the file and sees it in the step log) or the CLI's error debug captures (temp debug
  dir, deleted at exit). Nothing else persists page content.
- Never write the pairing token, API keys or page text to logs.

## 7. Test matrix (each rule has a test; P1/P2/P3 files)
| rule | test |
|---|---|
| flags (§2) | `extension/src/page/classify.test.ts` (happy-dom) + `tests/e2e/test_contract.py::test_flags` |
| PII vectors (§3) | `extension/src/page/pii.test.ts` + `tests/test_safety.py::test_pii_vectors` |
| gate rules (§4) | `tests/test_safety.py` (table-driven) + `tests/test_gate.py` (FakeDriver + FakeChannel) |
| extension floor | `tests/e2e/test_safety_e2e.py::test_forged_act_refused`, `::test_token_single_use` |
| search exemption | `tests/e2e/test_safety_e2e.py::test_search_form_no_prompt` |
| allowlist | `tests/test_safety.py::test_domains` + `tests/e2e/test_safety_e2e.py::test_allow_site_prompt` |
| handoffs | `tests/e2e/test_safety_e2e.py::test_captcha_handoff`, `::test_password_handoff` |
| Stop | `tests/e2e/test_panel_e2e.py::test_stop_within_one_second` |
| redaction | `tests/test_safety.py::test_redact` + `tests/test_agent.py::test_logs_never_contain_pii` |
