# BABAS - Browser Automation Bot Assistant System

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
python babas.py
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

## Voice Mode

Select Voice mode at startup to use the GUI control panel with:
- Click-to-record voice commands
- Plan confirmation buttons
- Stop execution button

## License

MIT
