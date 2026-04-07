# Aria Agent

A lean, multi-channel AI agent that runs against any OpenAI-compatible LLM
endpoint — local (Ollama, LM Studio) or cloud (Anthropic, OpenAI) — with
persistent markdown workspace, pluggable tools, session continuity, autonomous
memory reflection, and support for Telegram, WhatsApp, and scheduled tasks.

---

## Install

```bash
# Clone or download the project
cd aria_pkg

# Terminal interface only
pip install -e .

# With Telegram bot support
pip install -e ".[telegram]"
```

> **If pip defaults to user install and things break:**
> ```bash
> pip install -e . --break-system-packages
> # or use a virtualenv (recommended):
> python3 -m venv .venv
> source .venv/bin/activate
> pip install -e ".[telegram]"
> ```

After install, these commands are available system-wide:

| Command         | Description                                      |
|-----------------|--------------------------------------------------|
| `aria`          | Interactive REPL or single-shot query            |
| `aria-telegram` | Telegram bot (long-running)                      |
| `aria-whatsapp` | WhatsApp HTTP bridge (long-running, needs Node.js)|
| `aria-reflect`  | Analyse session history and update memory        |

---

## First run

On first launch, a setup wizard creates `~/.aria/` with a `.env` template
and prints instructions, then exits:

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

# ── WhatsApp (required for aria-whatsapp — skip if not using) ────────────────
# ARIA_WA_PORT=7532
# ARIA_WA_SECRET=pick-any-random-string
# WHATSAPP_ALLOWED=34612345678    # international format, no +

# ── Gmail / gog (required for the gmail and calendar tools) ──────────────────
GMAIL_CLI=gog
GOG_ACCOUNT=you@gmail.com

# ── Agent behaviour (optional, shown with defaults) ──────────────────────────
# ARIA_MAX_LOOPS=20              # raise if agent hits loop limit on complex tasks
# ARIA_MAX_HISTORY=60            # conversation turns kept in context

# ── Session continuity ────────────────────────────────────────────────────────
# Minutes of inactivity before Telegram/WhatsApp session is summarised
# ARIA_CHANNEL_IDLE_MINUTES=60

# ── Memory reflection ─────────────────────────────────────────────────────────
# Sessions analysed per reflection run
# ARIA_REFLECT_BATCH=10
# Max chars read per session log during reflection
# ARIA_REFLECT_SESSION_CHARS=3000

# ── Path overrides (optional) ────────────────────────────────────────────────
# ARIA_WORKSPACE=~/.aria/workspace
# ARIA_TOOLS_DIR=~/.aria/tools
```

### Recommended models

Aria uses a plain-text `TOOL:` / `INPUT:` protocol that requires solid
instruction-following. These models work well:

| Provider  | Model                       | Notes                        |
|-----------|-----------------------------|------------------------------|
| Anthropic | `claude-haiku-4-5-20251001` | Fast, reliable, recommended  |
| Anthropic | `claude-sonnet-4-5`         | More capable, slower         |
| OpenAI    | `gpt-4o-mini`               | Good balance                 |
| Ollama    | `llama3.2`, `mistral`, `qwen2.5` | Best local options      |

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
aria-whatsapp                         # WhatsApp bridge (long-running)
aria-reflect                          # analyse sessions, update memory patterns
aria-reflect --notify                 # same + Telegram notification
```

### REPL commands

| Command        | Description                      |
|----------------|----------------------------------|
| `/memory`      | Print current memory             |
| `/tools`       | List available tools             |
| `/clear`       | Clear conversation history       |
| `/save <note>` | Append a note directly to memory |
| `/quit`        | Exit                             |

On exit, the REPL automatically summarises the session and saves it to
`memory/last_session.md` for continuity in the next session.

---

## Session continuity

At the end of each session, a brief LLM-generated summary (3–5 bullet points)
is saved to `~/.aria/workspace/memory/last_session.md`. This is loaded into
the system prompt on the next session so the agent has lightweight continuity
without replaying full conversation history.

This works across all interfaces — REPL, single-shot, Telegram, WhatsApp.
For long-running channel processes (Telegram, WhatsApp), the summary is saved
automatically after a configurable idle period (`ARIA_CHANNEL_IDLE_MINUTES`).

---

## Memory reflection

`aria-reflect` is an autonomous background process that scans session logs,
extracts behavioural patterns, and writes them to `memory/patterns.md`.
This file is loaded into every session so the agent progressively improves
its understanding of your preferences and workflows.

```bash
# Run manually
aria-reflect

# Schedule daily at 3am (add to crontab -e)
0 3 * * * /home/$USER/.local/bin/aria-reflect --notify
```

The agent can also trigger reflection itself mid-conversation:

```
You: analyse our past conversations and learn from them
Aria: 🔧 calling reflect...
      Reflection complete: 23 sessions analysed, patterns updated.
```

**What it extracts** (only what's actually observed):
- Recurring topics and domains
- Preferred response style and format
- Common workflows and tool sequences
- Implicit preferences from corrections and feedback
- Technical context (languages, tools, systems)

A watermark file ensures sessions are never re-analysed, so it scales
efficiently regardless of history length.

---

## Channels

Each channel is independent — don't configure it if you don't need it.
Sessions are keyed by `(channel, user_id)` so history is isolated per
channel per user, while memory and tools are shared across all channels.

### Telegram

1. Create a bot via [@BotFather](https://t.me/BotFather), copy the token.
2. Get your chat ID from [@userinfobot](https://t.me/userinfobot).
3. Add to `~/.aria/.env`: `TELEGRAM_TOKEN` and `TELEGRAM_ALLOWED`.
4. Run: `nohup aria-telegram >> ~/.aria/telegram.log 2>&1 &`

Bot commands: `/start` `/memory` `/tools` `/clear` `/save <note>`

### WhatsApp

Requires Node.js and [whatsapp-web.js](https://github.com/pedroslopez/whatsapp-web.js).

```bash
# 1. Copy the bridge to ~/.aria/whatsapp/
mkdir -p ~/.aria/whatsapp
cp whatsapp/bridge.js whatsapp/package.json ~/.aria/whatsapp/
cd ~/.aria/whatsapp && npm install

# 2. Add to ~/.aria/.env
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

```cron
# Every morning at 8am
0 8 * * * /home/$USER/.local/bin/aria --notify "summarise my unread emails"

# Every Friday at 6pm
0 18 * * 5 /home/$USER/.local/bin/aria --notify "weekly digest of my emails"

# Daily memory reflection at 3am
0 3 * * * /home/$USER/.local/bin/aria-reflect --notify
```

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

Both are intercepted in Python — the LLM never needs to know file paths
or API internals.

---

## Built-in tools

| Tool          | Description                                                         |
|---------------|---------------------------------------------------------------------|
| `file_access` | Read/write/append/patch/list/delete files. Supports base64 encoding and paginated reads for large files. |
| `shell_run`   | Run shell commands or scripts. Pass `script` field to avoid JSON escaping issues. |
| `web_fetch`   | Fetch and extract text from a web page.                             |
| `gmail`       | Search/read/send/mark-read Gmail via `gog` CLI.                     |
| `calendar`    | List/create/update/delete/respond to Google Calendar events via `gog`. |
| `notify`      | Push a message to the user via Telegram.                            |
| `reflect`     | Analyse past sessions and update memory patterns on demand.         |

### Writing scripts without escaping issues

Use the `script` field in `shell_run`:

```json
{"script": "#!/usr/bin/env python3\nprint('hello \"world\"')", "interpreter": "python3"}
```

### Editing large files safely

Use `patch` to replace a specific string without rewriting the whole file:

```json
{"action": "patch", "path": "~/script.py", "old": "def old():", "new": "def new():"}
```

Use `offset`/`limit` to read large files in chunks:

```json
{"action": "read", "path": "~/big_file.py", "offset": 100, "limit": 50}
```

---

## Adding tools

Drop a `.py` file into `~/.aria/tools/` — it's auto-discovered on next start:

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

---

## Workspace

All state is plain markdown — fully human-editable:

```
~/.aria/
├── .env                                  ← config
├── tools/                                ← custom tool .py files (auto-discovered)
├── whatsapp/                             ← Node.js WhatsApp bridge
└── workspace/
    ├── memory/
    │   ├── core.md                       ← explicit facts (REMEMBER: lines)
    │   ├── last_session.md               ← rolling session summary
    │   ├── patterns.md                   ← behavioural patterns (aria-reflect)
    │   └── reflect_watermark             ← tracks analysed sessions
    ├── soul/
    │   └── identity.md                   ← agent persona (edit to customise)
    ├── sessions/
    │   └── session_YYYYMMDD_HHMMSS.md    ← per-session conversation logs
    └── tools_registry/
        └── available_tools.md            ← auto-generated tool docs
```

---

## Gmail & Calendar setup

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
# nohup (simple)
nohup aria-telegram >> ~/.aria/telegram.log 2>&1 &
tail -f ~/.aria/telegram.log
```

For a robust setup, use systemd:

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
        ├── agent.py                 ← ReAct loop, streaming, session continuity
        ├── channel.py               ← multi-channel session registry + idle timer
        ├── config.py                ← path resolution, .env loading
        ├── main.py                  ← CLI entry point (aria)
        ├── reflect.py               ← autonomous memory reflection (aria-reflect)
        ├── setup.py                 ← first-run wizard
        ├── telegram_bot.py          ← Telegram bot interface
        ├── telegram_notify.py       ← push-only Telegram sender
        ├── whatsapp_bridge.py       ← HTTP bridge for whatsapp-web.js
        ├── workspace.py             ← markdown persistence
        └── tools/
            ├── __init__.py          ← auto-loader & dispatcher
            ├── _env.py              ← subprocess env builder
            ├── calendar.py          ← Google Calendar via gog
            ├── file_access.py       ← read/write/patch/list files
            ├── gmail.py             ← Gmail via gog
            ├── notify.py            ← Telegram push notification
            ├── reflect.py           ← on-demand memory reflection
            ├── shell_run.py         ← shell commands + script field
            └── web_fetch.py         ← web page fetcher
```
