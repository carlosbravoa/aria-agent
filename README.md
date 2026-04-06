# Aria Agent

A lean, multi-channel AI agent that runs against any OpenAI-compatible LLM
endpoint — local (Ollama, LM Studio) or cloud (Anthropic, OpenAI) — with
persistent markdown workspace, pluggable tools, and support for Telegram,
WhatsApp, and scheduled tasks.

---

## Install

```bash
# Terminal + Telegram
pip install -e .

# With Telegram bot support
pip install -e ".[telegram]"
```

> **If pip defaults to user install and things break:**
> ```bash
> pip install -e . --break-system-packages
> # or use a virtualenv:
> python3 -m venv .venv && source .venv/bin/activate && pip install -e .
> ```

---

## First run

On first launch Aria detects there is no config and runs a setup wizard
that creates `~/.aria/` with a `.env` template, then exits with instructions:

```bash
aria
# ~/.aria has been created — edit ~/.aria/.env then run aria again
```

---

## Configure

Edit `~/.aria/.env`. Only the LLM block is required to get started.
Everything else is optional and only needed for the features you use.

```ini
# ── LLM (required) ───────────────────────────────────────────────────────────
LLM_BASE_URL=https://api.anthropic.com/v1
LLM_API_KEY=sk-ant-...
LLM_MODEL=claude-haiku-4-5-20251001
AGENT_NAME=Aria

# ── Telegram (required for aria-telegram and aria --notify) ──────────────────
TELEGRAM_TOKEN=<from @BotFather>
TELEGRAM_ALLOWED=<your chat ID from @userinfobot>

# ── WhatsApp (required for aria-whatsapp, skip if not using) ─────────────────
# ARIA_WA_PORT=7532
# ARIA_WA_SECRET=pick-any-random-string
# WHATSAPP_ALLOWED=34612345678    # international format, no +

# ── Gmail / gog (required for the gmail tool) ────────────────────────────────
GMAIL_CLI=gog
GOG_ACCOUNT=you@gmail.com

# ── Agent behaviour (optional, shown with defaults) ──────────────────────────
# ARIA_MAX_LOOPS=20       # raise if agent hits loop limit on complex tasks
# ARIA_MAX_HISTORY=60     # conversation turns kept in context

# ── Path overrides (optional) ────────────────────────────────────────────────
# ARIA_WORKSPACE=~/.aria/workspace
# ARIA_TOOLS_DIR=~/.aria/tools
```

### Recommended models

Aria uses a plain-text `TOOL:` / `INPUT:` protocol that requires solid
instruction-following. These models work well:

| Provider | Model | Notes |
|----------|-------|-------|
| Anthropic | `claude-haiku-4-5-20251001` | Fast, reliable, recommended |
| Anthropic | `claude-sonnet-4-5` | More capable, slower |
| OpenAI | `gpt-4o-mini` | Good balance |
| Ollama | `llama3.2`, `mistral`, `qwen2.5` | Best local options |

> **Avoid:** on-device runtimes like MediaPipe/Gemma — limited context window
> and unreliable structured output.

---

## Usage

```bash
aria                                  # interactive REPL
aria "query"                          # single-shot, prints to stdout
aria --notify "query"                 # single-shot, sends result to Telegram
aria --notify --chat 123 "query"      # send to a specific Telegram chat ID
aria-telegram                         # Telegram bot (long-running)
aria-whatsapp                         # WhatsApp bridge (long-running, needs Node.js)
```

### REPL commands

| Command        | Description                      |
|----------------|----------------------------------|
| `/memory`      | Print current memory             |
| `/tools`       | List available tools             |
| `/clear`       | Clear conversation history       |
| `/save <note>` | Append a note directly to memory |
| `/quit`        | Exit                             |

---

## Channels

Aria supports multiple messaging channels simultaneously. Each channel is
independent — if you don't need one, simply don't configure or run it.
Nothing will break.

Sessions are keyed by `(channel, user_id)` so each user on each channel gets
isolated conversation history, while the workspace (memory, tools) is shared.

### Telegram

1. Create a bot via [@BotFather](https://t.me/BotFather), copy the token.
2. Get your chat ID from [@userinfobot](https://t.me/userinfobot).
3. Add `TELEGRAM_TOKEN` and `TELEGRAM_ALLOWED` to `~/.aria/.env`.
4. Run: `aria-telegram`

Available bot commands: `/start` `/memory` `/tools` `/clear` `/save <note>`

### WhatsApp

Requires Node.js. Uses [whatsapp-web.js](https://github.com/pedroslopez/whatsapp-web.js).

```bash
# 1. Copy the Node.js bridge to ~/.aria/whatsapp/
mkdir -p ~/.aria/whatsapp
cp whatsapp/bridge.js whatsapp/package.json ~/.aria/whatsapp/
cd ~/.aria/whatsapp && npm install

# 2. Add WhatsApp vars to ~/.aria/.env
# ARIA_WA_PORT=7532
# ARIA_WA_SECRET=your-secret
# WHATSAPP_ALLOWED=34612345678

# 3. Start both processes
nohup aria-whatsapp >> ~/.aria/whatsapp.log 2>&1 &
nohup node ~/.aria/whatsapp/bridge.js >> ~/.aria/whatsapp-node.log 2>&1 &
```

On first run `bridge.js` shows a QR code — scan it with WhatsApp once.
Auth is persisted in `~/.aria/whatsapp/.wwebjs_auth/`.

### Scheduled tasks

Use `aria --notify` from cron to run tasks and receive results on Telegram:

```cron
# Every morning at 8am
0 8 * * * /home/$USER/.local/bin/aria --notify "summarise my unread emails"

# Every Friday at 6pm
0 18 * * 5 /home/$USER/.local/bin/aria --notify "weekly digest of my emails"
```

The agent can also schedule tasks itself using the `notify` tool to deliver
results back to Telegram without any manual setup.

---

## Tool protocol

Aria uses a plain-text protocol that works with any LLM:

```
TOOL: file_access
INPUT: {"action": "list", "path": "~"}

RESULT: Documents Downloads ...
```

For saving facts to memory:

```
REMEMBER: User prefers dark mode.
```

Both are intercepted directly in Python — the LLM never needs to know about
file paths or API internals.

---

## Built-in tools

| Tool          | Description                                                    |
|---------------|----------------------------------------------------------------|
| `file_access` | Read/write/append/patch/list/delete files. Supports base64 encoding and paginated reads for large files. |
| `shell_run`   | Run shell commands. Pass `script` field to avoid JSON escaping issues. Supports `stdin`, `timeout`, and `interpreter`. |
| `web_fetch`   | Fetch and extract text from a web page.                        |
| `gmail`       | Search/read/send/mark-read Gmail via `gog` CLI.               |
| `notify`      | Push a message to the user via Telegram.                       |

### Writing scripts without escaping issues

Use the `script` field in `shell_run` to pass code directly — no JSON
escaping needed:

```json
{
  "script": "#!/usr/bin/env python3\nprint('hello \"world\"')",
  "interpreter": "python3"
}
```

### Editing files without truncation

Use `file_access` `patch` to replace a specific string without rewriting
the whole file:

```json
{
  "action": "patch",
  "path": "~/script.py",
  "old": "def old_function():",
  "new": "def new_function():"
}
```

Use `offset` and `limit` to read large files in chunks:

```json
{"action": "read", "path": "~/big_file.py", "offset": 100, "limit": 50}
```

---

## Adding tools

Drop a `.py` file into `~/.aria/tools/`. Two things required:

```python
DEFINITION = {
    "name": "my_tool",
    "description": "What this tool does.",
    "parameters": {
        "type": "object",
        "properties": {
            "input": {"type": "string", "description": "The input value."}
        },
        "required": ["input"],
    },
}

def execute(args: dict) -> str:
    return f"Result: {args['input']}"
```

Restart `aria` — the tool is auto-discovered.

---

## Workspace

All state is plain markdown, fully human-editable:

```
~/.aria/
├── .env                              ← config
├── tools/                            ← custom tool .py files
├── whatsapp/                         ← Node.js WhatsApp bridge
└── workspace/
    ├── memory/
    │   └── core.md                   ← long-term memory (agent writes here)
    ├── soul/
    │   └── identity.md               ← agent persona (edit to customise)
    ├── sessions/
    │   └── session_YYYYMMDD_HHMMSS.md
    └── tools_registry/
        └── available_tools.md
```

---

## Gmail setup

```bash
# Authenticate once
gog auth credentials ~/Downloads/client_secret_....json
gog auth add you@gmail.com

# Add to ~/.aria/.env
GOG_ACCOUNT=you@gmail.com
GMAIL_CLI=gog
```

---

## Running as a background service

```bash
# Start Telegram bot
nohup aria-telegram >> ~/.aria/telegram.log 2>&1 &

# Check logs
tail -f ~/.aria/telegram.log
```

For a more robust setup, use a systemd user service:

```ini
# ~/.config/systemd/user/aria-telegram.service
[Unit]
Description=Aria Telegram Bot

[Service]
ExecStart=/home/$USER/.local/bin/aria-telegram
Restart=on-failure
EnvironmentFile=/home/$USER/.aria/.env

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now aria-telegram
journalctl --user -fu aria-telegram
```

---

## Project structure

```
aria_pkg/
├── pyproject.toml
├── README.md
├── whatsapp/                        ← copy to ~/.aria/whatsapp/
│   ├── package.json
│   └── bridge.js
└── src/
    └── aria/
        ├── __init__.py
        ├── agent.py                 ← ReAct loop, streaming, tool dispatch
        ├── channel.py               ← multi-channel session registry
        ├── config.py                ← path resolution, .env loading
        ├── main.py                  ← CLI entry point
        ├── setup.py                 ← first-run wizard
        ├── telegram_bot.py          ← Telegram bot interface
        ├── telegram_notify.py       ← push-only Telegram sender
        ├── whatsapp_bridge.py       ← HTTP bridge for whatsapp-web.js
        ├── workspace.py             ← markdown persistence
        └── tools/
            ├── __init__.py          ← auto-loader & dispatcher
            ├── _env.py              ← subprocess env builder
            ├── file_access.py
            ├── gmail.py
            ├── notify.py
            ├── shell_run.py
            └── web_fetch.py
```
