# AI providers and MCP clients

BABAS plans with a language model and acts through your real Chrome. Any task works: web search, media, forms, email,
school or government sites, general browsing. The model is configured with environment variables (put them in a `.env`
file in the repository root, or in the `env` block of your MCP client). Nothing else needs to change.

## 1. Choosing a provider

`BABAS_LLM_BASE_URL`, `BABAS_LLM_API_KEY` and `BABAS_LLM_MODEL` select any OpenAI-compatible endpoint.

| Provider | `BABAS_LLM_BASE_URL` | `BABAS_LLM_API_KEY` | `BABAS_LLM_MODEL` (example) | Notes |
|---|---|---|---|---|
| OpenAI | (leave empty) | `sk-…` | `gpt-5.2` | The default. `OPENAI_API_KEY` also works. |
| OpenRouter | `https://openrouter.ai/api/v1` | `sk-or-…` | any listed model id | Some models reject structured output: see JSON mode below. |
| DeepSeek | `https://api.deepseek.com/v1` | DeepSeek key | `deepseek-chat` | |
| Groq | `https://api.groq.com/openai/v1` | `gsk_…` | e.g. `llama-3.3-70b-versatile` | Pick a model Groq lists as available; if answers are rejected set `BABAS_LLM_JSON_MODE=json_object`. |
| Qwen (DashScope) | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` | DashScope key | `qwen-plus` | |
| Ollama | `http://localhost:11434/v1` | not needed (any text) | a pulled model, e.g. `qwen2.5:7b` | Start Ollama with `OLLAMA_CONTEXT_LENGTH=16384` or longer: long prompts are silently cut otherwise. |
| LM Studio | `http://localhost:1234/v1` | not needed (any text) | the loaded model id | Set `BABAS_LLM_JSON_MODE=json_schema` if `auto` misbehaves. |
| Anthropic (native) | set `BABAS_LLM_PROVIDER=anthropic` | `sk-ant-…` (`ANTHROPIC_API_KEY` also works) | required, no default | Needs `pip install 'babas[anthropic]'`. |

Model names change often: take the example as a hint and use the id your provider lists. A key is only required for
the cloud providers (no `BABAS_LLM_BASE_URL`) and for Anthropic. If it is missing, BABAS starts anyway and each task says
which variable to set.

### JSON mode

BABAS asks the model for a JSON plan. `BABAS_LLM_JSON_MODE` controls how:

| Value | Meaning |
|---|---|
| `auto` (default) | Try structured output (`json_schema`), then `json_object`, then plain text. It only moves on when the provider answers HTTP 400/422 and remembers what worked. |
| `json_schema` / `json_object` | Always use that response format. |
| `none` | Send no response format; the JSON is extracted from the text (fences, `<think>` blocks and surrounding prose are handled). |

If a local model is slow or loops with `auto`, try `json_object` or `none`.

### All settings

| Variable | Default | Meaning |
|---|---|---|
| `BABAS_LLM_PROVIDER` | `openai` | `openai` (any OpenAI-compatible endpoint) or `anthropic` |
| `BABAS_LLM_BASE_URL` | – | Endpoint URL (http/https) |
| `BABAS_LLM_API_KEY` | – | Falls back to `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` |
| `BABAS_LLM_MODEL` | `gpt-5.2` (openai) | Required for Anthropic |
| `BABAS_LLM_JSON_MODE` | `auto` | See above |
| `BABAS_LLM_TIMEOUT` | `60` | Seconds per model call |
| `BABAS_ALLOWED_DOMAINS` | gob.mx, google.com, google.com.mx, youtube.com, whatsapp.com, wikipedia.org | Comma list that EXTENDS the defaults; `*` allows every site. Other sites still ask first in the side panel. |
| `BABAS_MAX_STEPS` / `BABAS_MAX_RETRIES` | `30` / `2` | Limits per task |
| `BABAS_LANG` | `es` | Language of explanations and answers (`es` or `en`) |
| `BABAS_BRIDGE_HOST` / `BABAS_BRIDGE_PORT` | `127.0.0.1` / `8765` | Where the extension connects |
| `BABAS_EXTENSION_ORIGINS` | the pinned extension | Comma list of `chrome-extension://<id>` allowed to connect |
| `BABAS_HOME` | `~/.babas` | Pairing file and MCP screenshots |
| `BABAS_MCP_HTTP_PORT` | `8766` | Port of `serve --mcp-http` |
| `BABAS_LOG_LEVEL` | `INFO` | Logs go to stderr, with personal data masked |

An invalid value stops the command with a message that names the variable (exit code 2).

## 2. Privacy

BABAS sends the text of the pages it works on (which may include personal data) to the model you configure. Cloud
models receive it. Local models (Ollama, LM Studio) keep it on your machine. What you type as the task is sent as
written; personal data typed into forms is always shown to you on a confirmation card first and is masked in logs and
in the step history.

## 3. Using BABAS from MCP clients

BABAS exposes three tools to any MCP client (Claude Code, Claude Desktop, Cursor, …):

| Tool | What it does |
|---|---|
| `run_task(task)` | BABAS plans and performs the whole task with its own model. Returns `{status, answer, steps}` (`status`: `done`, `failed`, `stopped`, `max_steps`). Reports progress. Needs the model settings above. |
| `get_page_state()` | The current page: URL, title, numbered interactive elements with their risk flags, and the visible text. Values are masked and password values never appear. |
| `act(action)` | One action (click, type, goto, scroll, select, read, tabs, …) using the element numbers from `get_page_state`. Returns `{ok, detail, url, title}`. `get_page_state` and `act` do not need a BABAS model: the client's own model drives. |

The safety rules are the same as in the side panel and cannot be bypassed from a client: submitting forms, sending,
paying, deleting, typing personal data and visiting new sites are shown as cards in the BABAS side panel and need the
user's "Sí". Logins and CAPTCHAs are always done by the user. A refusal comes back as a tool error ("The user declined
this action in BABAS."). **Stop** in the panel cancels a running `run_task` and halts the browser until the user presses
"Reanudar".

Before connecting a client: build and load the extension (see the README), open the side panel, and keep Chrome open.

### stdio (recommended)

Use the Python of the virtual environment where BABAS is installed (`pip install -e '.[mcp]'`):

- Claude Code: `claude mcp add babas -- /path/to/.venv/bin/python -m babas mcp`
- Claude Desktop (`claude_desktop_config.json`) and Cursor (`~/.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "babas": {
      "command": "/path/to/.venv/bin/python",
      "args": ["-m", "babas", "mcp"],
      "env": { "BABAS_LLM_BASE_URL": "http://localhost:11434/v1", "BABAS_LLM_MODEL": "qwen2.5:7b" }
    }
  }
}
```

The `env` block is optional: a `.env` file in the repository root is read automatically. Stdio mode starts its own
bridge on port 8765, so stop `python -m babas serve` first (or use HTTP). The server writes nothing but protocol
messages to stdout; logs go to stderr.

### HTTP

`python -m babas serve --mcp-http` runs the side-panel service and an MCP endpoint at `http://127.0.0.1:8766/mcp`:

- Claude Code: `claude mcp add --transport http babas http://127.0.0.1:8766/mcp`

The endpoint listens on `127.0.0.1` only and rejects any request whose `Host` or `Origin` is not loopback (protection
against web pages and DNS-rebinding). It has no password, so do not expose it with a proxy or tunnel.

### Limits to know

- One browser-touching call at a time; a task started from the side panel and an MCP call exclude each other.
- Each tool call starts from the extension's current working tab; tab names created by `new_tab` in one `act` call are
  not remembered in the next. After a page-changing `act`, call `get_page_state` again before using element numbers
  (a stale number answers `STALE_SNAPSHOT`).
- `act` with the `screenshot` action saves the file under `~/.babas/screenshots/` (or `$BABAS_HOME/screenshots/`).
- `run_task` masks personal data in its answer, like the side panel does.
