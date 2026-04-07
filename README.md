# Aria Agent

A lean, multi-channel AI agent that runs against any OpenAI-compatible LLM
endpoint — local (Ollama, LM Studio) or cloud (Anthropic, OpenAI) — with
persistent markdown workspace, pluggable tools, session continuity, autonomous
memory reflection, proactive task scheduling, and support for Telegram and WhatsApp.

---

## Table of contents

1. [Requirements](#requirements)
2. [Install](#install)
3. [First run](#first-run)
4. [Configure](#configure)
5. [CLI commands](#cli-commands)
6. [Interactive REPL](#interactive-repl)
7. [Channels — Telegram](#telegram)
8. [Channels — WhatsApp](#whatsapp)
9. [Scheduled tasks](#scheduled-tasks)
10. [Autonomous supervisor](#autonomous-supervisor)
11. [Memory reflection](#memory-reflection)
12. [Session continuity](#session-continuity)
13. [Tool protocol](#tool-protocol)
14. [Built-in tools](#built-in-tools)
15. [Adding custom tools](#adding-custom-tools)
16. [Gmail & Calendar setup](#gmail--calendar-setup)
17. [Running as a background service](#running-as-a-background-service)
18. [Workspace layout](#workspace-layout)
19. [Project structure](#project-structure)

---

## Requirements

- Python 3.11+
- An OpenAI-compatible LLM endpoint (see [Configure](#configure))
- Node.js 18+ (only for WhatsApp)

---

## Install

```bash
# Clone the repo
git clone <repo-url>
cd aria_pkg

# Option A — virtualenv (recommended)
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[telegram]"

# Option B — user install
pip install -e ".[telegram]" --break-system-packages

# Option C — terminal only (no Telegram bot dependency)
pip install -e .
```

After install, these commands are available in your shell:

| Command            | Description                                        |
|--------------------|----------------------------------------------------|
| `aria`             | Interactive REPL or single-shot query              |
| `aria-telegram`    | Telegram bot (long-running)                        |
| `aria-whatsapp`    | WhatsApp HTTP bridge (long-running, needs Node.js) |
| `aria-reflect`     | Analyse session history, update memory patterns    |
| `aria-supervisor`  | Autonomous task queue supervisor (long-running)    |

---

## First run

```bash
aria
```

On first launch, a setup wizard creates `~/.aria/` with a `.env` template,
prints step-by-step instructions, and exits. Edit the config file, then run
`aria` again.

---

## Configure

All configuration lives in `~/.aria/.env`. Only the LLM block is required.
Everything else is optional — features that aren't configured simply won't
load, and nothing breaks.

```ini
# ── LLM (required) ────────────────────────────────────────────────────────────
LLM_BASE_URL=https://api.anthropic.com/v1
LLM_API_KEY=sk-ant-...
LLM_MODEL=claude-haiku-4-5-20251001
AGENT_NAME=Aria

# ── Telegram ──────────────────────────────────────────────────────────────────
# Required for: aria-telegram, aria --notify
# Get TELEGRAM_TOKEN from @BotFather on Telegram
# Get TELEGRAM_ALLOWED (your chat ID) from @userinfobot on Telegram
TELEGRAM_TOKEN=<bot token>
TELEGRAM_ALLOWED=<your chat ID>

# ── WhatsApp ──────────────────────────────────────────────────────────────────
# Required for: aria-whatsapp  (skip entirely if not using WhatsApp)
# ARIA_WA_PORT=7532
# ARIA_WA_SECRET=pick-any-random-string
# WHATSAPP_ALLOWED=34612345678        # your number, international format, no +

# ── Gmail & Calendar (gog CLI) ────────────────────────────────────────────────
# Required for: gmail and calendar tools
# See "Gmail & Calendar setup" section below
# GMAIL_CLI=gog
# GOG_ACCOUNT=you@gmail.com

# ── Agent behaviour ───────────────────────────────────────────────────────────
# ARIA_MAX_LOOPS=20              # max tool-call loops per turn
# ARIA_MAX_HISTORY=60            # conversation turns kept in context
# ARIA_CHANNEL_IDLE_MINUTES=60   # inactivity before Telegram/WhatsApp session summarised

# ── Memory reflection ─────────────────────────────────────────────────────────
# ARIA_REFLECT_BATCH=10          # sessions analysed per batch
# ARIA_REFLECT_SESSION_CHARS=3000 # max chars read per session log
# ARIA_REFLECT_MAX_LINES=40      # max bullet points in patterns.md

# ── Supervisor ────────────────────────────────────────────────────────────────
# ARIA_SUPERVISOR_INTERVAL=30    # seconds between task queue polls

# ── Path overrides ────────────────────────────────────────────────────────────
# ARIA_WORKSPACE=~/.aria/workspace
# ARIA_TOOLS_DIR=~/.aria/tools
```

### Recommended models

Aria uses a plain-text `TOOL:` / `INPUT:` protocol requiring solid
instruction-following. These models work well:

| Provider  | Model                        | Notes                       |
|-----------|------------------------------|-----------------------------|
| Anthropic | `claude-haiku-4-5-20251001`  | Fast, reliable, recommended |
| Anthropic | `claude-sonnet-4-5`          | More capable, slower        |
| OpenAI    | `gpt-4o-mini`                | Good balance                |
| Ollama    | `llama3.2`, `mistral`, `qwen2.5` | Best local options      |

> **Avoid** on-device runtimes like MediaPipe/Gemma — they have limited context
> windows and unreliable structured output, causing tool-call failures.

---

## CLI commands

```bash
# Interactive REPL
aria

# Single-shot — run a query and exit
aria "what's the weather in Santiago?"

# Single-shot — run query and send result to Telegram
aria --notify "summarise my unread emails"

# Single-shot — send to a specific Telegram chat ID
aria --notify --chat 123456789 "daily briefing"

# Start the Telegram bot (long-running)
aria-telegram

# Start the WhatsApp bridge (long-running, needs Node.js side too)
aria-whatsapp

# Analyse session history and update memory patterns
aria-reflect
aria-reflect --notify          # send result to Telegram
aria-reflect --verbose         # debug output

# Start the autonomous task supervisor (long-running)
aria-supervisor
aria-supervisor --once         # process pending tasks once and exit
aria-supervisor --verbose      # debug output
```

---

## Interactive REPL

```bash
aria
```

| Command        | Description                            |
|----------------|----------------------------------------|
| `/memory`      | Print current memory                   |
| `/tools`       | List all loaded tools                  |
| `/clear`       | Clear conversation history             |
| `/save <note>` | Append a note directly to memory       |
| `/help`        | Show command list                      |
| `/quit`        | Exit (saves session summary)           |

On exit, a brief session summary is automatically saved to
`memory/last_session.md` and loaded into the next session for continuity.

---

## Telegram

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token
2. Message [@userinfobot](https://t.me/userinfobot) → copy your chat ID
3. Add to `~/.aria/.env`:
   ```ini
   TELEGRAM_TOKEN=<token>
   TELEGRAM_ALLOWED=<chat ID>
   ```
4. Start the bot:
   ```bash
   nohup aria-telegram >> ~/.aria/telegram.log 2>&1 &
   ```

**Bot commands:** `/start` `/memory` `/tools` `/clear` `/save <note>`

Sessions are summarised automatically after `ARIA_CHANNEL_IDLE_MINUTES` of
inactivity so the agent maintains context across conversations.

---

## WhatsApp

Requires Node.js 18+. Uses [whatsapp-web.js](https://github.com/pedroslopez/whatsapp-web.js).

**Step 1 — Python bridge:**
```bash
# Add to ~/.aria/.env
# ARIA_WA_PORT=7532
# ARIA_WA_SECRET=your-secret
# WHATSAPP_ALLOWED=34612345678

# Start the Python bridge
nohup aria-whatsapp >> ~/.aria/whatsapp.log 2>&1 &
```

**Step 2 — Node.js bridge:**
```bash
mkdir -p ~/.aria/whatsapp
cp whatsapp/bridge.js whatsapp/package.json ~/.aria/whatsapp/
cd ~/.aria/whatsapp
npm install
nohup node bridge.js >> ~/.aria/whatsapp-node.log 2>&1 &
```

On first run, `bridge.js` prints a QR code — scan it with WhatsApp once.
Auth is persisted in `~/.aria/whatsapp/.wwebjs_auth/` so you won't need
to scan again.

---

## Scheduled tasks

Use `aria --notify` from cron to run tasks on a schedule and receive
results on Telegram:

```bash
crontab -e
```

```cron
# Daily email summary at 8am
0 8 * * * /home/$USER/.local/bin/aria --notify "summarise my unread emails from today"

# Weekly digest every Friday at 6pm
0 18 * * 5 /home/$USER/.local/bin/aria --notify "give me a weekly summary of my emails and calendar"

# Daily memory reflection at 3am
0 3 * * * /home/$USER/.local/bin/aria-reflect --notify
```

> **Tip:** Use `aria-supervisor --once` in cron instead of `aria` if you want
> tasks to survive process crashes and support retries.

---

## Autonomous supervisor

The supervisor polls `~/.aria/tasks/pending/` for due tasks, executes them
through the agent, and routes results to Telegram.

```bash
# Start as a long-running background process
nohup aria-supervisor >> ~/.aria/supervisor.log 2>&1 &

# Or process pending tasks once (useful in cron)
aria-supervisor --once
```

### Task file format

Tasks are plain text files dropped into `~/.aria/tasks/pending/`:

```ini
prompt: summarise my unread emails and check today's calendar
notify: true
priority: 3
run_after: 2026-04-10T08:00:00
max_retries: 2
source: cron
```

**Fields:**

| Field         | Default | Description                                          |
|---------------|---------|------------------------------------------------------|
| `prompt`      | —       | What to ask the agent (required)                     |
| `notify`      | `true`  | Send result via Telegram                             |
| `priority`    | `5`     | 1 (urgent) to 10 (low) — lower runs first            |
| `run_after`   | now     | ISO datetime: `2026-04-10T08:00:00`                  |
| `max_retries` | `2`     | Retry count on failure                               |
| `source`      | `user`  | Who created it: `cron`, `agent`, `user`, `script`    |

### Creating tasks from Python

```python
from aria.task import Task, enqueue

enqueue(Task(
    prompt    = "check my emails and summarise anything urgent",
    run_after = "2026-04-10T08:00:00",
    notify    = True,
    priority  = 3,
))
```

### Agent-created tasks

The agent can schedule its own follow-up work using the `schedule` tool:

```
You: remind me to follow up on the PR tomorrow at 9am
Aria: 🔧 calling schedule...
      Task a3f8c21b queued at 2026-04-10T09:00:00: follow up on the PR
```

### Task queue states

```
~/.aria/tasks/
├── pending/    ← waiting to run
├── running/    ← currently executing (crash-safe hand-off)
├── done/       ← completed, result appended to file
└── failed/     ← retries exhausted
```

---

## Memory reflection

`aria-reflect` scans new session logs, extracts behavioural patterns, and
writes a concise consolidated list to `memory/patterns.md`. This is loaded
into every session so the agent progressively learns your preferences.

```bash
aria-reflect                  # analyse new sessions
aria-reflect --notify         # same + Telegram notification
aria-reflect --verbose        # with debug output
```

**Two-phase process:**
1. **Extraction** — analyses only new sessions (watermark-gated, never re-processes old ones)
2. **Consolidation** — merges with existing patterns, prunes redundant entries, enforces a hard cap (`ARIA_REFLECT_MAX_LINES=40`)

**What it extracts** (only from actual evidence):
- Recurring topics and domains
- Preferred response style and format
- Common workflows and tool sequences
- Implicit preferences from corrections and feedback
- Technical context — languages, tools, systems

**Automate with cron:**
```cron
0 3 * * * /home/$USER/.local/bin/aria-reflect --notify
```

The agent can also trigger reflection on demand:
```
You: analyse our past conversations and update your memory
Aria: 🔧 calling reflect...
      Reflection complete: 8 sessions analysed, patterns consolidated to 23 lines.
```

---

## Session continuity

At the end of each session, a 3–5 bullet LLM summary is saved to
`memory/last_session.md`. This is loaded into the `## Previous Session`
block of the system prompt on the next session.

This works across all interfaces — REPL, single-shot, Telegram, WhatsApp —
so a conversation started on Telegram can be continued in the terminal
without losing context.

For Telegram and WhatsApp, summaries are saved automatically after
`ARIA_CHANNEL_IDLE_MINUTES` of inactivity (default 60 minutes), and also
on clean shutdown of the bot process.

---

## Tool protocol

Aria uses a plain-text protocol that works with any LLM:

```
TOOL: file_access
INPUT: {"action": "list", "path": "~"}

RESULT: Documents Downloads projects
```

For saving facts to memory:
```
REMEMBER: User prefers responses in bullet points.
```

Both are intercepted directly in Python — the LLM never needs to know
about file paths or API internals. Tools are auto-discovered at startup
from `src/aria/tools/` and `~/.aria/tools/`.

---

## Built-in tools

| Tool          | Description                                                               |
|---------------|---------------------------------------------------------------------------|
| `file_access` | Read, write, append, patch, list, delete files. Supports `base64` encoding and paginated reads (`offset`/`limit`) for large files. |
| `shell_run`   | Run shell commands. Pass `script` to avoid JSON escaping. Supports `stdin`, `timeout`, `interpreter`. |
| `web_fetch`   | Fetch and extract readable text from a web page.                          |
| `gmail`       | Search, read, send, mark-read via `gog` CLI.                              |
| `calendar`    | List, create, update, delete, RSVP to Google Calendar events via `gog`.   |
| `notify`      | Push a message to the user via Telegram.                                  |
| `schedule`    | Queue a task for the supervisor to execute at a given time.               |
| `reflect`     | Trigger memory reflection on demand.                                      |

### Writing scripts without escaping issues

```json
{
  "script": "#!/usr/bin/env python3\nprint('hello \"world\"')",
  "interpreter": "python3"
}
```

### Editing large files safely

Replace a specific string without rewriting the whole file:
```json
{"action": "patch", "path": "~/script.py", "old": "def old():", "new": "def new():"}
```

Read a large file in chunks:
```json
{"action": "read", "path": "~/big_file.py", "offset": 100, "limit": 50}
```

---

## Adding custom tools

Drop a `.py` file into `~/.aria/tools/` — it's auto-discovered on next start.
No registration needed.

```python
DEFINITION = {
    "name": "my_tool",
    "description": "One-line description the agent uses to decide when to call this.",
    "parameters": {
        "type": "object",
        "properties": {
            "input": {
                "type": "string",
                "description": "The input value.",
            }
        },
        "required": ["input"],
    },
}

def execute(args: dict) -> str:
    return f"Result: {args['input']}"
```

The `DEFINITION` description is injected into the system prompt verbatim,
so write it as clear instructions the agent can act on.

---

## Gmail & Calendar setup

Both tools use the [gogcli](https://github.com/steipete/gogcli) (`gog`) CLI.

```bash
# 1. Install gog
#    macOS:
brew install steipete/tap/gogcli
#    Linux: download from https://github.com/steipete/gogcli/releases

# 2. Get OAuth credentials from Google Cloud Console
#    (APIs & Services → Credentials → Create → Desktop app)
gog auth credentials ~/Downloads/client_secret_....json

# 3. Authenticate your account
gog auth add you@gmail.com

# 4. Verify
gog gmail search 'is:unread' --max 3

# 5. Add to ~/.aria/.env
GOG_ACCOUNT=you@gmail.com
GMAIL_CLI=gog
```

---

## Running as a background service

### nohup (simple)

```bash
nohup aria-telegram    >> ~/.aria/telegram.log    2>&1 &
nohup aria-supervisor  >> ~/.aria/supervisor.log  2>&1 &

tail -f ~/.aria/telegram.log
```

### systemd (robust, auto-restart)

Create a service file for each process you want to run:

```ini
# ~/.config/systemd/user/aria-telegram.service
[Unit]
Description=Aria Telegram Bot
After=network.target

[Service]
ExecStart=/home/$USER/.local/bin/aria-telegram
Restart=on-failure
RestartSec=10
EnvironmentFile=/home/$USER/.aria/.env

[Install]
WantedBy=default.target
```

```bash
# Enable and start
systemctl --user enable --now aria-telegram

# Check status and logs
systemctl --user status aria-telegram
journalctl --user -fu aria-telegram
```

Repeat for `aria-supervisor` and `aria-whatsapp` as needed.

---

## Workspace layout

All state is plain markdown — fully human-readable and editable:

```
~/.aria/
├── .env                                      ← configuration
├── tools/                                    ← custom tool .py files (auto-discovered)
├── whatsapp/                                 ← Node.js WhatsApp bridge
│   ├── package.json
│   ├── bridge.js
│   └── .wwebjs_auth/                         ← WhatsApp session (auto-created)
├── tasks/                                    ← supervisor task queue
│   ├── pending/                              ← tasks waiting to run
│   ├── running/                              ← currently executing
│   ├── done/                                 ← completed (with result)
│   └── failed/                               ← retries exhausted
└── workspace/
    ├── memory/
    │   ├── core.md                           ← explicit facts (REMEMBER: lines)
    │   ├── last_session.md                   ← rolling session summary
    │   ├── patterns.md                       ← behavioural patterns (aria-reflect)
    │   └── reflect_watermark                 ← tracks last analysed session
    ├── soul/
    │   └── identity.md                       ← agent persona (edit freely)
    ├── sessions/
    │   └── session_YYYYMMDD_HHMMSS.md        ← per-session conversation logs
    └── tools_registry/
        └── available_tools.md                ← auto-generated tool reference
```

---

## Project structure

```
aria_pkg/
├── pyproject.toml
├── README.md
├── whatsapp/                          ← copy to ~/.aria/whatsapp/
│   ├── package.json
│   └── bridge.js
└── src/
    └── aria/
        ├── __init__.py
        ├── agent.py                   ← ReAct loop, streaming, session continuity
        ├── channel.py                 ← multi-channel registry, idle timer
        ├── config.py                  ← path resolution, .env loading
        ├── main.py                    ← CLI entry point (aria)
        ├── reflect.py                 ← memory reflection engine (aria-reflect)
        ├── setup.py                   ← first-run wizard
        ├── supervisor.py              ← task queue supervisor (aria-supervisor)
        ├── task.py                    ← task data model and queue operations
        ├── telegram_bot.py            ← Telegram bot interface
        ├── telegram_notify.py         ← push-only Telegram sender
        ├── whatsapp_bridge.py         ← HTTP bridge for whatsapp-web.js
        ├── workspace.py               ← markdown persistence layer
        └── tools/
            ├── __init__.py            ← auto-loader and dispatcher
            ├── _env.py                ← subprocess environment builder
            ├── calendar.py            ← Google Calendar via gog
            ├── file_access.py         ← read/write/patch/list files
            ├── gmail.py               ← Gmail via gog
            ├── notify.py              ← Telegram push notification
            ├── reflect.py             ← on-demand memory reflection
            ├── schedule.py            ← queue a task for the supervisor
            ├── shell_run.py           ← shell commands and scripts
            └── web_fetch.py           ← web page fetcher
```
