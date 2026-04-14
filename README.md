# Aria Agent

A lean AI agent that runs against any OpenAI-compatible LLM endpoint — local
(Ollama, LM Studio) or cloud (Anthropic, OpenAI) — with persistent markdown
workspace, pluggable tools, session continuity, and autonomous memory
reflection. Optionally extends to Telegram, WhatsApp, and scheduled tasks.

---

## Table of contents

1. [Requirements](#requirements)
2. [Quickstart — CLI only](#quickstart--cli-only)
3. [Quickstart — with services](#quickstart--with-services)
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
17. [Jira setup](#jira-setup)
19. [Running as a background service](#running-as-a-background-service)
19. [Workspace layout](#workspace-layout)
19. [Project structure](#project-structure)

---

## Requirements

- Python 3.11+
- An OpenAI-compatible LLM endpoint
- Node.js 18+ *(only for WhatsApp)*

---

## Quickstart — CLI only

The simplest setup. Just a terminal, no bots, no background services.

```bash
# 1. Clone and install
git clone <repo-url>
cd aria_pkg
pip install -e .

# 2. Run — wizard creates ~/.aria/.env on first launch
aria
```

The wizard will ask for your LLM endpoint and model. When it asks about
Telegram, WhatsApp, Supervisor, and Gmail — answer **no** to all of them.

```
  Telegram bot? [Y/n]: n
  WhatsApp bridge? [Y/n]: n
  Autonomous supervisor? [Y/n]: n
  Gmail & Calendar? [Y/n]: n
```

That's it. No services are installed. `aria` works from the terminal:

```bash
aria                              # interactive REPL
aria "explain this error: ..."    # single-shot query
aria-reflect                      # analyse past sessions, update memory
```

> **pip install fails?** Try:
> ```bash
> pip install -e . --break-system-packages
> # or use a virtualenv:
> python3 -m venv .venv && source .venv/bin/activate && pip install -e .
> ```

---

## Quickstart — with services

For Telegram notifications, WhatsApp, and autonomous background tasks.

```bash
# 1. Clone and install
git clone <repo-url>
cd aria_pkg
pip install -e .

# 2. Run the setup wizard
aria-install
```

`aria-install` guides you through feature selection and configuration,
then installs and starts everything as systemd services automatically.

```
  Telegram bot? [Y/n]: y           → asks for token + chat ID
  WhatsApp bridge? [Y/n]: n        → skipped
  Autonomous supervisor? [Y/n]: y  → asks for poll interval
  Gmail & Calendar? [Y/n]: y       → asks for GOG_ACCOUNT
```

After the wizard completes, services start immediately and restart
automatically on reboot. Re-run at any time to update configuration:

```bash
aria-install              # full wizard — reconfigure + reinstall
aria-install --services   # reinstall services only (after git pull)
aria-install --dry-run    # preview changes without applying
aria-install --uninstall  # remove all services
```

---

## Configure

All configuration lives in `~/.aria/.env`. The wizard creates and manages
this file, but you can edit it directly at any time.

```ini
# ── LLM (required) ────────────────────────────────────────────────────────────
LLM_BASE_URL=https://api.anthropic.com/v1
LLM_API_KEY=sk-ant-...
LLM_MODEL=claude-haiku-4-5-20251001
AGENT_NAME=Aria

# ── Telegram ──────────────────────────────────────────────────────────────────
# Required for: aria-telegram bot and aria --notify
# Get TELEGRAM_TOKEN from @BotFather — get chat ID from @userinfobot
TELEGRAM_TOKEN=<bot token>
TELEGRAM_ALLOWED=<your chat ID>

# ── WhatsApp ──────────────────────────────────────────────────────────────────
# Required for: aria-whatsapp (skip entirely if not using)
# ARIA_WA_PORT=7532
# ARIA_WA_SECRET=pick-any-random-string
# WHATSAPP_ALLOWED=34612345678      # international format, no +

# ── Gmail & Calendar ──────────────────────────────────────────────────────────
# Required for: gmail and calendar tools
# GMAIL_CLI=gog
# GOG_ACCOUNT=you@gmail.com

# ── Agent behaviour ───────────────────────────────────────────────────────────
# ARIA_MAX_LOOPS=20                # max tool-call loops per turn
# ARIA_MAX_HISTORY=60              # conversation turns kept in context
# ARIA_CHANNEL_IDLE_MINUTES=60     # idle minutes before channel session summarised

# ── Supervisor ────────────────────────────────────────────────────────────────
# ARIA_SUPERVISOR_INTERVAL=30      # seconds between task queue polls
# ARIA_REFLECT_EVERY=86400         # seconds between reflection runs (0 = off)
# ARIA_REFLECT_NOTIFY=true         # Telegram notification after reflection

# ── Memory reflection ─────────────────────────────────────────────────────────
# ARIA_REFLECT_BATCH=10            # sessions analysed per batch
# ARIA_REFLECT_SESSION_CHARS=3000  # max chars read per session log
# ARIA_REFLECT_MAX_LINES=40        # max bullet points in patterns.md

# ── Path overrides ────────────────────────────────────────────────────────────
# ARIA_WORKSPACE=~/.aria/workspace
# ARIA_TOOLS_DIR=~/.aria/tools
```

### Recommended models

| Provider  | Model                        | Notes                       |
|-----------|------------------------------|-----------------------------|
| Anthropic | `claude-haiku-4-5-20251001`  | Fast, reliable, recommended |
| Anthropic | `claude-sonnet-4-5`          | More capable, slower        |
| OpenAI    | `gpt-4o-mini`                | Good balance                |
| Ollama    | `llama3.2`, `mistral`, `qwen2.5` | Best local options      |

> **Avoid** on-device runtimes like MediaPipe/Gemma — limited context window
> and unreliable structured output cause tool-call failures.

---

## CLI commands

```bash
# Interactive REPL
aria

# Single-shot — run a query and exit
aria "summarise this error log"

# Single-shot — send result to Telegram (requires Telegram config)
aria --notify "summarise my unread emails"
aria --notify --chat 123456789 "daily briefing"

# Analyse session history and update memory patterns
aria-reflect
aria-reflect --notify          # Telegram notification when done
aria-reflect --verbose         # debug output

# Task supervisor (requires supervisor config)
aria-supervisor
aria-supervisor --once         # process pending tasks once and exit

# Install / manage services
aria-install                   # full wizard
aria-install --services        # reinstall services only
aria-install --dry-run         # preview changes
aria-install --uninstall       # remove all services
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

On exit, a brief session summary is saved to `memory/last_session.md` and
loaded into the next session for lightweight continuity.

---

## Telegram

1. Create a bot via [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token.
2. Get your chat ID from [@userinfobot](https://t.me/userinfobot).
3. Run `aria-install` and answer **yes** to Telegram, or add to `~/.aria/.env`:
   ```ini
   TELEGRAM_TOKEN=<token>
   TELEGRAM_ALLOWED=<chat ID>
   ```
4. Start: `nohup aria-telegram >> ~/.aria/telegram.log 2>&1 &`

Bot commands: `/start` `/memory` `/tools` `/clear` `/save <note>`

Sessions are summarised after `ARIA_CHANNEL_IDLE_MINUTES` of inactivity
so the agent has context when you return.

---

## WhatsApp

Requires Node.js 18+.

```bash
# 1. Copy the Node.js bridge
mkdir -p ~/.aria/whatsapp
cp whatsapp/bridge.js whatsapp/package.json ~/.aria/whatsapp/
cd ~/.aria/whatsapp && npm install

# 2. Run aria-install and answer yes to WhatsApp, or add to ~/.aria/.env:
# ARIA_WA_PORT=7532
# ARIA_WA_SECRET=your-secret
# WHATSAPP_ALLOWED=34612345678

# 3. Start both processes
nohup aria-whatsapp >> ~/.aria/whatsapp.log 2>&1 &
nohup node ~/.aria/whatsapp/bridge.js >> ~/.aria/whatsapp-node.log 2>&1 &
```

On first run `bridge.js` shows a QR code — scan with WhatsApp once.
Auth persists in `~/.aria/whatsapp/.wwebjs_auth/`.

---

## Scheduled tasks

Use `aria --notify` from cron (requires Telegram):

```cron
# Daily email summary at 8am
0 8 * * * /home/$USER/.local/bin/aria --notify "summarise my unread emails"

# Weekly digest every Friday at 6pm
0 18 * * 5 /home/$USER/.local/bin/aria --notify "weekly email and calendar digest"
```

The supervisor handles memory reflection automatically — no cron entry needed
for that if `aria-supervisor` is running.

---

## Autonomous supervisor

The supervisor runs in the background, executing queued tasks and running
memory reflection on a schedule. Enable it via `aria-install` or start manually:

```bash
aria-supervisor
```

### Task file format

Drop a `.task` file into `~/.aria/tasks/pending/`:

```ini
prompt: check my calendar for conflicts tomorrow and notify me
notify: true
priority: 3
run_after: 2026-04-10T08:00:00
max_retries: 2
source: user
```

| Field         | Default | Description                               |
|---------------|---------|-------------------------------------------|
| `prompt`      | —       | What to ask the agent (required)          |
| `notify`      | `true`  | Send result via Telegram                  |
| `priority`    | `5`     | 1 (urgent) to 10 (low)                    |
| `run_after`   | now     | ISO datetime: `2026-04-10T08:00:00`       |
| `max_retries` | `2`     | Retry count on failure                    |
| `source`      | `user`  | `cron`, `agent`, `user`, or `script`      |

The agent can also schedule tasks itself using the `schedule` tool:

```
You: remind me to follow up on that PR tomorrow at 9am
Aria: 🔧 calling schedule...
      Task a3f8c21b queued at 2026-04-10T09:00:00
```

Task queue states:
```
~/.aria/tasks/
├── pending/    ← waiting to run
├── running/    ← currently executing
├── done/       ← completed (result appended)
└── failed/     ← retries exhausted
```

---

## Memory reflection

Scans session logs, extracts behavioural patterns, and writes them to
`memory/patterns.md` which is loaded into every session.

If the supervisor is running, reflection happens automatically every 24 hours
(`ARIA_REFLECT_EVERY=86400`). Run manually at any time:

```bash
aria-reflect
aria-reflect --notify    # Telegram notification when done
aria-reflect --verbose   # debug output
```

The agent can also trigger it mid-conversation via the `reflect` tool.

**Two-phase process:**
1. **Extraction** — analyses only new sessions (watermark prevents re-processing)
2. **Consolidation** — merges with existing patterns, prunes weak signals, caps at `ARIA_REFLECT_MAX_LINES` bullet points

---

## Session continuity

At the end of each session a 3–5 bullet summary is saved to
`memory/last_session.md`. The next session loads it under
`## Previous Session` in the system prompt — no full history replay needed.

Works across all interfaces: REPL, single-shot, Telegram, WhatsApp.

---

## Tool protocol

Aria uses plain text that any LLM can produce:

```
TOOL: file_access
INPUT: {"action": "list", "path": "~"}

RESULT: Documents Downloads projects
```

For saving facts:
```
REMEMBER: User prefers bullet-point responses.
```

Tools are auto-discovered from `src/aria/tools/` and `~/.aria/tools/`
at startup — no registration needed.

---

## Built-in tools

| Tool          | Description                                                               |
|---------------|---------------------------------------------------------------------------|
| `file_access` | Read, write, append, patch, list, delete files. Supports `base64` encoding and paginated reads (`offset`/`limit`). |
| `shell_run`   | Run shell commands. Pass `script` field to avoid JSON escaping issues.    |
| `web_fetch`   | Fetch readable text from a web page.                                      |
| `gmail`       | Search, read, send, mark-read via `gog` CLI.                              |
| `calendar`    | List, create, update, delete, RSVP Google Calendar events via `gog`.      |
| `notify`      | Push a message to the user via Telegram.                                  |
| `schedule`    | Queue a task for the supervisor to execute at a given time.               |
| `reflect`     | Trigger memory reflection on demand.                                      |
| `jira`        | Create, search, comment, transition Jira issues via REST API.             |

### Writing scripts without escaping issues

```json
{"script": "print('hello \"world\"')", "interpreter": "python3"}
```

### Editing large files safely

```json
{"action": "patch", "path": "~/script.py", "old": "def old():", "new": "def new():"}
```

```json
{"action": "read", "path": "~/big_file.py", "offset": 100, "limit": 50}
```

---

## Adding custom tools

Drop a `.py` file into `~/.aria/tools/` — auto-discovered on next start:

```python
DEFINITION = {
    "name": "my_tool",
    "description": "One-line description the agent uses to decide when to call this.",
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

## Gmail & Calendar setup

Both tools use [gogcli](https://github.com/steipete/gogcli).

```bash
# Install gog
# macOS:  brew install steipete/tap/gogcli
# Linux:  download from https://github.com/steipete/gogcli/releases

# Store OAuth credentials (download Desktop app JSON from Google Cloud Console)
gog auth credentials ~/Downloads/client_secret_....json

# Authenticate
gog auth add you@gmail.com --services gmail,calendar

# Verify
gog gmail search 'is:unread' --max 3

# Add to ~/.aria/.env
GOG_ACCOUNT=you@gmail.com
GMAIL_CLI=gog
```

> **Running as a systemd service?** Re-run `aria-install --services` after authenticating
> to regenerate service files — they now include `PassEnvironment` to forward the
> keyring session so gog can access stored tokens without any extra config.

## Jira setup

The `jira` tool uses the Jira REST API directly — no extra binary needed,
`httpx` is already a project dependency.

```bash
# 1. Get an API token
#    https://id.atlassian.com/manage-profile/security/api-tokens

# 2. Add to ~/.aria/.env
JIRA_BASE_URL=https://yourcompany.atlassian.net
JIRA_EMAIL=you@yourcompany.com
JIRA_API_TOKEN=your-api-token
JIRA_DEFAULT_PROJECT=PROJ        # optional — used when project not specified
```

That's it — the tool is auto-discovered on next start. If any var is missing
it returns a clear error message with instructions.

Supported actions: `create`, `get`, `search` (JQL), `comment`, `transition`,
`assign`, `list_projects`. Useful JQL examples the agent knows:

```
assignee = currentUser() AND statusCategory != Done   # my open tickets
project = PROJ AND issuetype = Bug AND status != Done # open bugs
duedate <= 7d AND statusCategory != Done              # due this week
```

---

## Running as a background service

### Install wizard (recommended)

```bash
aria-install
```

Detects binaries, writes service files, enables lingering, starts everything.

```bash
aria-install --services    # reinstall after git pull + pip install -e .
aria-install --uninstall   # remove all services
```

### Manual — nohup

```bash
nohup aria-telegram    >> ~/.aria/telegram.log    2>&1 &
nohup aria-supervisor  >> ~/.aria/supervisor.log  2>&1 &
tail -f ~/.aria/telegram.log
```

### Manual — systemd

```bash
# Check status
systemctl --user status aria-telegram
systemctl --user status aria-supervisor

# Live logs
journalctl --user -fu aria-telegram

# Restart after code update
systemctl --user restart aria-telegram aria-supervisor
```

---

## Workspace layout

```
~/.aria/
├── .env                                      ← configuration
├── tools/                                    ← custom tool .py files
├── whatsapp/                                 ← Node.js WhatsApp bridge
│   ├── package.json
│   ├── bridge.js
│   └── .wwebjs_auth/                         ← WhatsApp session (auto-created)
├── tasks/                                    ← supervisor task queue
│   ├── pending/
│   ├── running/
│   ├── done/
│   └── failed/
└── workspace/
    ├── memory/
    │   ├── core.md                           ← explicit facts (REMEMBER: lines)
    │   ├── last_session.md                   ← rolling session summary
    │   ├── patterns.md                       ← behavioural patterns (aria-reflect)
    │   └── reflect_watermark                 ← tracks last analysed session
    ├── soul/
    │   └── identity.md                       ← agent persona (edit freely)
    ├── sessions/
    │   └── session_YYYYMMDD_HHMMSS.md        ← per-session logs
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
        ├── install.py                 ← setup wizard (aria-install)
        ├── main.py                    ← CLI entry point (aria)
        ├── reflect.py                 ← memory reflection engine (aria-reflect)
        ├── setup.py                   ← first-run wizard
        ├── supervisor.py              ← task supervisor (aria-supervisor)
        ├── task.py                    ← task data model and queue operations
        ├── telegram_bot.py            ← Telegram bot
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
