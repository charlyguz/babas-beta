# BABAS - Browser Automation Bot Assistant System

<!-- preview beta V0.1 tested done formating done -->

AI-powered web automation with voice control.

## Quick Start

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Create `.env` file:
```
OPENAI_API_KEY=your_key_here
ELEVENLABS_API_KEY=your_key_here  # Optional, for voice output
```

3. Run:
```bash
python -m babas
```

## Features

- **Smart Navigation**: AI creates step-by-step plans for web tasks
- **Auto-Recovery**: Automatically retries with new strategies if something fails
- **Multi-Tab**: Work with multiple browser tabs
- **Voice Commands**: Speak your commands (optional)
- **Voice Butler**: Jarvis-style voice feedback (optional)

## Commands

| Command | Description |
|---------|-------------|
| `exit` | Close BABAS |
| `help` | Show help |
| `tabs` | List open tabs |
| `new` | Open new tab |
| `screenshot` | Take screenshot |
| `mute` | Mute voice butler |

## Examples

```
"open youtube and search lofi music"
"go to gmail"
"search two sum on leetcode"
"in new tab open twitter"
```

## Extension and side panel (preview)

BABAS can drive your real Chrome/Chromium through a browser extension with a side panel, instead of Playwright.
The panel is Spanish-first (it follows the browser language) and BABAS always asks before anything important.

```bash
cd extension && npm ci && npm run build      # builds extension/dist
```

1. Open `chrome://extensions`, turn on Developer mode, click "Load unpacked" and choose `extension/dist`.
2. Put `OPENAI_API_KEY` in `.env` and start the app: `python -m babas serve`
3. Click the BABAS icon to open the side panel and type a task, for example "busque videos de recetas en YouTube".
   The first connection pairs the extension with the app automatically; if it ever says "Sin conexión con BABAS" after a
   reinstall, run `python -m babas pair --reset`.

What it asks you about: submitting forms, sending/paying/deleting, typing personal data (CURP, RFC, NSS, email, phone),
and any site outside the allowed list (gob.mx, google.com, youtube.com, whatsapp.com, wikipedia.org). Logins and
CAPTCHAs are always left to you, and the red Stop button always works.

Developer console without the AI: `python -m babas serve --repl` (`goto URL`, `ls`, `click N`, `type N text`, `help`).
Manual checklist per site: `docs/manual-tests.md`.

Tests: `pip install -e '.[dev]' && pytest -q` and `cd extension && npm test`.

## Voice Mode

Select Voice mode at startup to use the GUI control panel with:
- Click-to-record voice commands
- Plan confirmation buttons
- Stop execution button

## License

MIT
