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

## Choose your AI provider

OpenAI by default. Any OpenAI-compatible service works by setting three variables in `.env`: `BABAS_LLM_BASE_URL`,
`BABAS_LLM_API_KEY` and `BABAS_LLM_MODEL` (OpenRouter, DeepSeek, Groq, Qwen, Ollama, LM Studio, ...), or
`BABAS_LLM_PROVIDER=anthropic` for Claude. Local models keep your pages on your computer. Table of providers, JSON modes
and every setting: [`docs/providers.md`](docs/providers.md).

## Use BABAS from other AI apps (MCP)

BABAS is also an MCP server (`run_task`, `get_page_state`, `act`), so Claude Code, Claude Desktop, Cursor or any MCP client
can browse with your real Chrome. Confirmations still appear in the BABAS side panel and cannot be skipped.

```bash
pip install -e '.[mcp]'
claude mcp add babas -- /path/to/.venv/bin/python -m babas mcp      # stdio
python -m babas serve --mcp-http                                     # or HTTP: http://127.0.0.1:8766/mcp
```

Setup for Claude Desktop and Cursor, the HTTP endpoint and limits: [`docs/providers.md`](docs/providers.md).

Tests: `pip install -e '.[dev]' && pytest -q` and `cd extension && npm test`.

## Voice Mode

Select Voice mode at startup to use the GUI control panel with:
- Click-to-record voice commands
- Plan confirmation buttons
- Stop execution button

## License

MIT
