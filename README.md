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

## Extension (developer preview)

BABAS can also drive your real Chrome/Chromium through a browser extension instead of Playwright.
This preview has no AI planner attached yet: a developer console lets you list, highlight, click and type on a real page.

```bash
cd extension && npm ci && npm run build      # builds extension/dist
```

1. Open `chrome://extensions`, turn on Developer mode, click "Load unpacked" and choose `extension/dist`.
2. Start the bridge with the console: `python -m babas serve --repl`
3. In the console: `goto https://www.wikipedia.org`, `ls` (numbers every element and highlights it on the page), `type 0 pozole`, `click 1`. Type `help` for all commands.

Tests: `pip install -e '.[dev]' && pytest -q` and `cd extension && npm test`.

## Voice Mode

Select Voice mode at startup to use the GUI control panel with:
- Click-to-record voice commands
- Plan confirmation buttons
- Stop execution button

## License

MIT
