# Manual test checklist (P2)

A human runs this; an agent never does live verification (no real accounts, logins, CAPTCHAs or personal data).
Use test accounts and a test contact. Type passwords only in the page itself, never in BABAS.

**Setup**
1. Put `OPENAI_API_KEY` in `.env`, then run `python -m babas serve` and leave it open.
2. Build the extension (`cd extension && npm ci && npm run build`). In `chrome://extensions` turn on Developer mode,
   choose "Load unpacked" and pick `extension/dist`.
3. Click the BABAS icon to open the side panel. The status must read "Conectado". Type each task below and press Enter.

**Pairing note.** The first connection creates `~/.babas/pairing.json` (trust on first use) and the extension stores the
token. After reinstalling the extension or wiping the Chrome profile the panel shows "Sin conexión con BABAS": run
`python -m babas pair --reset`, then reload the extension.

**Legend.** Confirm = a card with "Sí, continuar" / "No". Allow = a card "¿Permitir este sitio?" with "Permitir".
Handoff = a card with "Listo". **Every row:** steps appear in plain Spanish, typed personal data is masked in the log
(`GO**************09`), the full value shows only on the confirm card, and the run ends with "Listo.".
Fill Pass/Fail and Notes (what you saw, errors, URLs).

## YouTube (search and play)
| # | Task to type | Expected confirmations / handoffs | Pass/Fail | Notes |
|---|---|---|---|---|
| Y1 | Busque videos de recetas de pozole en YouTube | None (search-only form). Results page opens. | | |
| Y2 | Busque música de mariachi en YouTube y ponga el primer video | None. The first video plays. | | |

## Google search
| # | Task to type | Expected confirmations / handoffs | Pass/Fail | Notes |
|---|---|---|---|---|
| G1 | Busque en Google la receta del pozole rojo y abra el primer resultado | None for the search. If the first result is not a default site: Allow card naming its host. "Permitir" opens it, and the host appears under Ajustes > Sitios permitidos. | | |
| G2 | Same as G1, with a different topic, answering "No permitir" | The run ends with "Detenido. No se hizo nada más." and the site is not opened. | | |
| G3 | Press "Quitar" on that host in Ajustes, then repeat G1 | The Allow card appears again. | | |

## Google Classroom (read only)
| # | Task to type | Expected confirmations / handoffs | Pass/Fail | Notes |
|---|---|---|---|---|
| C1 | Abra Google Classroom y dígame qué tareas tengo pendientes | If logged out: password handoff ("Por seguridad, BABAS no escribe contraseñas…"). Log in on the page, press "Listo". No Confirm card. The answer lists the pending work. | | |
| C2 | Dígame cuál tarea vence primero | None. Nothing is clicked, typed or turned in. | | |

## Gmail
| # | Task to type | Expected confirmations / handoffs | Pass/Fail | Notes |
|---|---|---|---|---|
| M1 | Abra mi correo y léame el último mensaje | If logged out: password handoff. No Confirm card. Sender, subject and gist are shown in the panel. | | |
| M2 | Escriba un correo a (your test address) con asunto «Prueba» y texto «Hola, es una prueba» | Confirm "Escribir datos personales" with the address in full (the card may batch subject and body). Then a second Confirm for «Enviar» ("Acción importante"). Answer "Sí" to the first and "No" to the second: nothing is sent and the run ends "Detenido…". | | |
| M3 | Repeat M2 and answer "Sí, continuar" both times | The test message arrives in the other inbox. | | |

## WhatsApp Web
| # | Task to type | Expected confirmations / handoffs | Pass/Fail | Notes |
|---|---|---|---|---|
| W1 | Abra WhatsApp Web | QR handoff ("Abra WhatsApp en su teléfono y escanee el código de la pantalla…"). Scan it, press "Listo", the chat list loads. | | |
| W2 | Busque el contacto (test contact) en WhatsApp | None expected (search field). Record any card that appears. The chat opens. | | |
| W3 | Mande a (test contact) el mensaje «Hola, es una prueba» | Confirm listing the text to type and the Enter key. "No": nothing is sent. "Sí, continuar": the message is sent once. | | |

## gob.mx (no personal data)
| # | Task to type | Expected confirmations / handoffs | Pass/Fail | Notes |
|---|---|---|---|---|
| Z1 | Busque en gob.mx cómo consultar mi CURP | None (search-only form). BABAS explains where the CURP service is. | | |
| Z2 | Abra la página para consultar la CURP y dígame qué datos pide | BABAS only reads and lists the fields. It types nothing. If a CAPTCHA appears: handoff, then stop there. Enter no real data. | | |

## Known limitations to record
Some sites ignore synthetic DOM events (`isTrusted` is false): a click or typed text seems to do nothing, or an editor
(Gmail compose, WhatsApp message box, YouTube controls) keeps its old value. For each case, note the site, the action
and what BABAS reported in the table below (CLAUDE.md points here).

| Site | Action | Symptom | Notes |
|---|---|---|---|
| | | | |
| | | | |

## Voice (microphone)
Needs a speech backend (`docs/providers.md` section 4). Test in Chrome and in Brave, with a real microphone.
- [ ] **First use:** press "Hablar". Either the browser asks for the microphone, or the panel explains the problem and
      shows "Dar permiso"; that opens a tab, "Permitir el micrófono" grants it, and after going back "Hablar" works.
- [ ] **Dictation:** say "Busque videos de recetas en YouTube", wait for the pause (or press "Listo"). The sentence appears
      in the text box, nothing is sent, and you can edit it. Press "Enviar" to run it.
- [ ] **Silence:** press "Hablar" and say nothing. It tells you it heard nothing and the button works again.
- [ ] **30 s cap and Stop:** talk for more than 30 s (it ends by itself). Pressing "Detener" during a run still works.
- [ ] **Closing the panel mid-recording** turns the microphone indicator of the browser off.
- [ ] **Wrong key or no backend:** the message says what to do, in plain Spanish, without technical words.
Record here the browser, the backend and anything odd (for example, a side panel that never shows the prompt):

| Browser | Backend | Result / notes |
|---|---|---|
| | | |

## Stop checklist
Press "Detener" in each state. After each one the panel reads "Detenido. No se hizo nada más." within 1 s, and for the
next 10 s nothing is clicked, typed or navigated in any tab. Then press "Reanudar" and run Y1 to confirm it still works.
- [ ] **Planning:** send Y2 and press Stop before the first step appears.
- [ ] **Acting:** send Y2 and press Stop between two steps.
- [ ] **Waiting:** send "Espere 30 segundos y luego abra YouTube", press Stop during the wait. YouTube never opens.
- [ ] **Confirmation card:** start M2, press Stop while the card is open. The card disappears and nothing is typed.
- [ ] **Server frozen:** run M2, and when the card is open press Ctrl+Z in the `serve` terminal, then press Stop. The panel
      stops at once and the page does nothing. Run `fg` to resume the server, and nothing else happens.
