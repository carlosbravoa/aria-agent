# Aria Agent

A lean AI agent that runs against any OpenAI-compatible LLM endpoint — local
(Ollama, LM Studio) or cloud (Anthropic, OpenAI) — with persistent markdown
workspace, pluggable tools, session continuity, autonomous memory reflection,
and a rich terminal interface. Optionally extends to Telegram, WhatsApp, and
scheduled background tasks.

## Why Aria

I created this agent after experimenting with OpenClaw and noticing its tremendously high token consumption for relatively simple tasks — handling emails, fetching web content, managing reminders. The codebase is a mixture of many technologies, and context handling balloons quickly. So I decided to build my own: leaner, simpler, with stricter context handling, and capable of running well with local LLMs (which is why tool handling works differently here than in most agents).

The result is an agent that will impress you with how useful it can be while remaining trivial to maintain — and the best part: you pay a tiny fraction of what you would with OpenClaw. My daily usage covers managing personal email, scheduling reminders in ways a normal calendar cannot, handling to-dos, creating Jira tickets, summarising web content, and more. It runs on Ubuntu and works equally well as a CLI tool or as an IM agent on Telegram or WhatsApp.

## What it can do today

- **CLI (REPL)** — interactive terminal with Markdown rendering, arrow-key history, and tab completion for `/` commands
- **Telegram & WhatsApp** — full IM agent with formatted responses, model switching, and proactive scheduled messages
- **Rich tool ecosystem** — web content fetching (via trafilatura), file read/write, shell execution, Gmail and Google Drive (via gog), Google Calendar, IMAP email, Jira tickets, scheduled reminders, and memory reflection. You can also write your own tools — or ask the agent to write them for you.
- **Multi-model support** — switch between models mid-session (e.g. local Ollama and a cloud model) with `/model <name>`
- **Autonomous background tasks** — a supervisor runs scheduled tasks, sends proactive notifications, and reflects on past conversations to improve over time
- **Lean token usage** — careful context management and a plain-text tool protocol mean you get impressive capability at a fraction of the cost of comparable agents
- **Browser automation** *(experimental)* — control your real Chrome/Chromium with existing sessions via CDP; navigate, click, read content from any logged-in site

## What is on the roadmap

- **Knowledge base integration** — consuming content from document repositories, wikis, or vector stores for RAG-style retrieval
- **Your suggestions** — open an issue or ask the agent itself

---

## Table of contents

1. [Requirements](#requirements)
2. [Quickstart — CLI only](#quickstart--cli-only)
3. [Quickstart — with services](#quickstart--with-services)
4. [Configure](#configure)
5. [Model profiles](#model-profiles)
23. [CLI commands](#cli-commands)
23. [Interactive REPL](#interactive-repl)
23. [Channels — Telegram](#telegram)
8. [Channels — WhatsApp](#whatsapp)
23. [Scheduled tasks](#scheduled-tasks)
23. [Autonomous supervisor](#autonomous-supervisor)
23. [Memory reflection](#memory-reflection)
23. [Session continuity](#session-continuity)
23. [Tool protocol](#tool-protocol)
23. [Built-in tools](#built-in-tools)
23. [Web fetching](#web-fetching)
23. [Adding custom tools](#adding-custom-tools)
23. [Gmail & Calendar setup](#gmail--calendar-setup)
23. [Jira setup](#jira-setup)
23. [IMAP setup](#imap-setup)
23. [Running as a background service](#running-as-a-background-service)
23. [Workspace layout](#workspace-layout)
23. [Project structure](#project-structure)

---

## Requirements

### Always required

| Requirement | Notes |
|-------------|-------|
| Python 3.11+ | `python3 --version` to check |
| System libraries | Some trafilatura dependencies may need system packages (see [Web fetching](#web-fetching)) |
| pip | Usually bundled with Python |
| An OpenAI-compatible LLM endpoint | Anthropic, OpenAI, Ollama, LM Studio, etc. |

### Per feature

| Feature | External requirement | Install |
|---------|---------------------|---------|
| **Telegram bot** (`aria-telegram`) | Telegram bot token | Free — create via [@BotFather](https://t.me/BotFather) |
| **Gmail tool** | `gog` (gogcli) binary + Google OAuth | See [Gmail & Calendar setup](#gmail--calendar-setup) |
| **Calendar tool** | `gog` (gogcli) binary + Google OAuth | Same as Gmail |
| **Google Drive tool** | `gog` (gogcli) binary + Google OAuth | Same as Gmail |
| **IMAP tool** | None — stdlib only | Just add credentials to `.env` |
| **Jira tool** | None — REST API via `httpx` | Just add credentials to `.env` |
| **WhatsApp bridge** | Node.js 18+ and `npm` | `node --version` to check |
| **Background services** | systemd (Linux) | Pre-installed on most Linux distros |

> **No binary needed for Jira or IMAP** — they call REST APIs directly using
> `httpx` (already a project dependency) and Python's standard `imaplib`.
> Just add the credentials to `~/.aria/.env` and the tools are ready.

> **gog is a single binary** — no npm, no pip, no runtime required.
> Download once, authenticate once, works for Gmail, Calendar, and Drive.

---

## Quickstart — CLI only

The simplest setup. Just a terminal, no bots, no background services.

```bash
# 1. Clone and install
git clone https://github.com/your-org/aria-agent.git
cd aria-agent
pip install .

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
aria --version                    # show version
aria-reflect                      # analyse past sessions, update memory
```

> **pip install fails?** Try:
> ```bash
> pip install . --break-system-packages
> # or use a virtualenv (recommended):
> python3 -m venv .venv && source .venv/bin/activate && pip install .
> ```

---

## Quickstart — with services

For Telegram notifications, WhatsApp, and autonomous background tasks.

```bash
# 1. Clone and install
git clone https://github.com/your-org/aria-agent.git
cd aria-agent
pip install .

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
aria-install --services   # reinstall services only (after git pull + pip install .)
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
LLM_MODEL=claude-sonnet-4-6
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
# GOG_KEYRING_BACKEND=file
# GOG_KEYRING_PASSWORD=your-passphrase  # required for headless/service use

# ── IMAP (optional — any non-Gmail provider) ─────────────────────────────────
# IMAP_DEFAULT_HOST=imap.fastmail.com
# IMAP_DEFAULT_USER=you@fastmail.com
# IMAP_DEFAULT_PASSWORD=app-password
# IMAP_DEFAULT_PORT=993              # optional, default 993
# Additional accounts: IMAP_WORK_HOST=... IMAP_WORK_USER=... IMAP_WORK_PASSWORD=...

# ── Jira ──────────────────────────────────────────────────────────────────────
# Optional — configured at runtime, not via installer
# JIRA_BASE_URL=https://yourcompany.atlassian.net
# JIRA_EMAIL=you@yourcompany.com
# JIRA_API_TOKEN=your-api-token
# JIRA_DEFAULT_PROJECT=PROJ

# ── File access security ──────────────────────────────────────────────────────
# ARIA_FILE_READ_DIRS=~/Documents:~/projects   # workspace always included
# ARIA_FILE_WRITE_DIRS=~/projects              # workspace always included

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

| Provider  | Model                  | Notes                            |
|-----------|------------------------|----------------------------------|
| Anthropic | `claude-sonnet-4-6`    | Recommended — best balance       |
| Anthropic | `claude-haiku-4-5-20251001` | Faster, lighter               |
| OpenAI    | `gpt-4o-mini`          | Good alternative                 |
| Ollama    | `llama3.2`, `mistral`, `qwen2.5` | Best local options     |

> **Avoid** on-device runtimes like MediaPipe/Gemma — limited context window
> and unreliable structured output cause tool-call failures.

---

## Model profiles

Aria supports up to 9 named model profiles that can be switched mid-session without losing conversation history, memory, or tools.

### Configuration

Add profiles to `~/.aria/.env`. Unset fields inherit from the default `LLM_*` values:

```ini
# Default profile — unchanged from existing config
LLM_BASE_URL=https://api.anthropic.com/v1
LLM_API_KEY=sk-ant-...
LLM_MODEL=claude-sonnet-4-6

# Profile 1 — fast model (inherits BASE_URL and API_KEY from default)
LLM_PROFILE1_NAME=fast
LLM_PROFILE1_MODEL=claude-haiku-4-5-20251001

# Profile 2 — local model (different endpoint)
LLM_PROFILE2_NAME=local
LLM_PROFILE2_MODEL=llama3.2
LLM_PROFILE2_BASE_URL=http://localhost:11434/v1
LLM_PROFILE2_API_KEY=ollama

# Profile 3 — more powerful model
LLM_PROFILE3_NAME=strong
LLM_PROFILE3_MODEL=claude-opus-4-6
```

Profiles are numbered 1–9. Each has an optional `NAME` used for switching — if no name is set it defaults to `profile1`, `profile2`, etc.

### Switching profiles

**REPL:**
```
/models
  ──────────────────────────────────
  default      claude-sonnet-4-6   ← active
  fast         claude-haiku-4-5-20251001
  local        llama3.2
  ──────────────────────────────────

/model fast
  Switched to fast (claude-haiku-4-5-20251001)

/model default
  Switched to default (claude-sonnet-4-6)

/model
  fast  claude-haiku-4-5-20251001   (shows current)
```

**Telegram:**
```
/model          → lists all profiles with ✓ on active
/model fast     → switches and confirms
/model default  → back to default
```

**WhatsApp:**
```
/models         → lists all profiles
/model fast     → switches and confirms
```

> **Note:** Profile switches are per-session and per-channel. The supervisor always uses the default profile for scheduled tasks.

---

## CLI commands

```bash
# Interactive REPL (with arrow keys, history, tab completion, Markdown rendering)
aria

# Show version
aria --version

# Single-shot — run a query and exit
aria "summarise this error log"

# Single-shot — send result to Telegram (requires Telegram config)
aria --notify "summarise my unread emails"
aria --notify --chat 123456789 "daily briefing"

# Analyse session history and update memory patterns
aria-reflect
aria-reflect --notify          # Telegram notification when done
aria-reflect --verbose         # debug output

# Task supervisor
aria-supervisor                # long-running background process
aria-supervisor --once         # process pending tasks once and exit

# Install / manage services
aria-install                   # full wizard
aria-install --services        # reinstall services only (after git pull)
aria-install --dry-run         # preview changes
aria-install --uninstall       # remove all services
```

---

## Interactive REPL

```bash
aria
```

Arrow keys and history (↑/↓) work out of the box. Tab completion works for `/` commands — type `/` and press Tab to see options.
Responses are rendered as Markdown — headings, bold, code blocks, lists.

| Command        | Description                            |
|----------------|----------------------------------------|
| `/memory`      | Print current memory                   |
| `/tools`       | List all loaded tools                  |
| `/clear`       | Clear conversation history             |
| `/save <note>` | Append a note directly to memory       |
| `/version`     | Show version                           |
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

Bot commands: `/start` `/memory` `/tools` `/clear` `/save <note>` `/version`

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

Use `aria --notify` from cron for simple scheduled queries:

```cron
# Daily email summary at 8am
0 8 * * * /home/$USER/.local/bin/aria --notify "summarise my unread emails"
```

For recurring tasks with the supervisor, see the next section.

---

## Autonomous supervisor

The supervisor runs in the background, executing queued tasks and running
memory reflection on a schedule. Enable via `aria-install` or start manually:

```bash
aria-supervisor
```

### Task file format

Drop a `.task` JSON file into `~/.aria/tasks/pending/`, or ask the agent to
schedule one for you:

```json
{
  "prompt": "Check my calendar for today and send a morning briefing",
  "notify": true,
  "priority": 3,
  "run_after": "2026-04-30T08:00:00",
  "recur": "weekdays",
  "max_retries": 2,
  "source": "user"
}
```

| Field         | Default    | Description                                      |
|---------------|------------|--------------------------------------------------|
| `prompt`      | —          | What to ask the agent (required)                 |
| `notify`      | `true`     | Send result via Telegram                         |
| `priority`    | `5`        | 1 (urgent) to 10 (low)                           |
| `run_after`   | now        | ISO datetime: `2026-04-30T08:00:00`              |
| `recur`       | —          | `daily`, `weekly`, `weekdays`, or `<N>m`         |
| `max_retries` | `2`        | Retry count on failure                           |
| `source`      | `user`     | `cron`, `agent`, `user`, or `script`             |

### Recurring tasks

Set `recur` and the supervisor automatically re-enqueues the task after each
run — no need to reschedule manually. Ask the agent to create one:

```
You: create a daily morning briefing at 8am every weekday
Aria: 🔧 calling schedule...
      Task a3f8c21b queued at 2026-04-30T08:00:00, recurs weekdays
```

### Managing scheduled tasks

Ask the agent directly:

```
You: what tasks do I have scheduled?
Aria: 🔧 calling schedule...
      - [pending] id=a3f8c21b run_after=2026-04-30T08:00:00 [weekdays]: Check my calendar...
      - [pending] id=c91d4e02 run_after=2026-05-01T09:00:00: Follow up on the PR

You: cancel the PR reminder
Aria: 🔧 calling schedule...
      Task c91d4e02 cancelled.
```

### Task queue states

```
~/.aria/tasks/
├── pending/     ← waiting to run
├── running/     ← currently executing
├── done/        ← completed (result appended)
├── failed/      ← retries exhausted
└── cancelled/   ← manually cancelled
```

---

## Memory reflection

Scans session logs, extracts behavioural patterns, and writes them to
`memory/patterns.md` which is loaded into every session.

The supervisor runs reflection automatically every 24 hours
(`ARIA_REFLECT_EVERY=86400`). Run manually at any time:

```bash
aria-reflect
aria-reflect --notify    # Telegram notification when done
aria-reflect --verbose   # debug output
```

The agent can also trigger it mid-conversation:

```
You: analyse our past conversations and update your memory
Aria: 🔧 calling reflect...
      Reflection complete: 8 sessions analysed, patterns consolidated to 23 lines.
```

**Two-phase process:**
1. **Extraction** — analyses only new sessions (watermark prevents re-processing)
2. **Consolidation** — merges with existing patterns, prunes weak signals, caps at `ARIA_REFLECT_MAX_LINES` bullet points

---

## Session continuity

At the end of each session a summary is saved to `memory/last_session.md`
and loaded under `## Previous Session` in the next session's system prompt.
No full history replay — lightweight and token-efficient.

Works across all interfaces: REPL, single-shot, Telegram, WhatsApp.

---

## Tool protocol

Aria uses plain text that any LLM can produce:

```
TOOL: file_access
INPUT: {"action": "list", "path": "~/projects"}

RESULT: my-app/ notes.md script.py
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
| `file_access` | Read, write, append, patch, list, delete files. Supports `base64` encoding and paginated reads (`offset`/`limit`). Read/write restricted to configured directories. |
| `shell_run`   | Run shell commands or scripts. Interpreter whitelist enforced. Destructive commands require confirmation or are blocked in non-interactive mode. |
| `web_fetch`   | Fetch readable text from a web page using trafilatura for clean content extraction. |
| `gmail`       | Search, read, send, mark-read via `gog` CLI.                              |
| `calendar`    | List, create, update, delete, RSVP Google Calendar events via `gog`.      |
| `notify`      | Push a message to the user via Telegram.                                  |
| `schedule`    | Create, list, and cancel scheduled tasks for the supervisor.              |
| `reflect`     | Trigger memory reflection on demand.                                      |
| `jira`        | Create, search, comment, transition Jira issues via REST API.             |
| `browser`     | *(experimental)* Control Chrome/Chromium via CDP — viewport-based snapshots, click, type, read, scroll. Uses your real sessions. |
| `imap`        | List, search, read, move, delete emails on any IMAP provider.             |
| `drive`       | List, search, read, download, upload, organise Google Drive files via gog. |

### Writing scripts without JSON escaping issues

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

### File access security

Read and write operations are restricted to an allow-list. Configure in `~/.aria/.env`:

```ini
ARIA_FILE_READ_DIRS=~/Documents:~/projects   # workspace always included
ARIA_FILE_WRITE_DIRS=~/projects              # workspace always included
```

Delete is always restricted to the workspace. Sensitive paths (`~/.ssh`,
`~/.aria/.env`, etc.) are always blocked regardless of configuration.

---

## Web fetching

`web_fetch` uses [trafilatura](https://trafilatura.readthedocs.io) for content extraction — the same approach as Firefox Reader Mode. It identifies the main article or documentation body and discards navigation, ads, footers, and boilerplate, dramatically improving signal-to-noise ratio compared to plain HTML stripping.

trafilatura is installed automatically with `pip install .` but some of its dependencies have system-level requirements that pip alone cannot satisfy.

**If `pip install .` fails** with errors related to `pandas-stubs`, `pyproj`, or similar:

```bash
# Debian/Ubuntu
sudo apt install python3-pyproj
pip install pandas-stubs
pip install .   # retry

# macOS
brew install proj
pip install .   # retry
```

If you cannot install the system dependencies, trafilatura degrades gracefully to a regex-based HTML stripper — web fetching still works, just with more noise in the output.

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

# Switch to file-based keyring — required for headless/service use
gog auth keyring file

# Authenticate (opens browser; add --manual for SSH/headless)
gog auth add you@gmail.com --services gmail,calendar

# Verify it works without a TTY
GOG_KEYRING_BACKEND=file GOG_KEYRING_PASSWORD=your-passphrase \
  gog gmail search 'is:unread' --max 3

# Add to ~/.aria/.env
GOG_ACCOUNT=you@gmail.com
GMAIL_CLI=gog
GOG_KEYRING_BACKEND=file
GOG_KEYRING_PASSWORD=your-passphrase
```

The `aria-install` wizard asks for all of these in the Gmail section.

---

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

The tool is auto-discovered on next start. If any var is missing it returns
a clear error message. Not included in the `aria-install` wizard — configure
directly in `~/.aria/.env`.

Supported actions: `create`, `get`, `search` (JQL), `comment`, `transition`,
`assign`, `list_projects`. Useful JQL patterns the agent knows:

```
assignee = currentUser() AND statusCategory != Done   # my open tickets
project = PROJ AND issuetype = Bug AND status != Done # open bugs
duedate <= 7d AND statusCategory != Done              # due this week
```

---

## IMAP setup

The `imap` tool uses `imaplib` from the Python standard library — no extra dependencies.

```ini
# Default account
IMAP_DEFAULT_HOST=imap.fastmail.com
IMAP_DEFAULT_USER=you@fastmail.com
IMAP_DEFAULT_PASSWORD=your-app-password

# Second account (any prefix works)
IMAP_WORK_HOST=outlook.office365.com
IMAP_WORK_USER=me@company.com
IMAP_WORK_PASSWORD=your-app-password
```

Use the `account` parameter to select which account:
```
You: check my work emails
Aria: 🔧 calling imap...
      {"action": "list", "account": "WORK"}
```

Provider reference:

| Provider       | Host                        | Port |
|----------------|-----------------------------|------|
| Gmail          | `imap.gmail.com`            | 993  |
| Outlook/O365   | `outlook.office365.com`     | 993  |
| iCloud         | `imap.mail.me.com`          | 993  |
| Fastmail       | `imap.fastmail.com`         | 993  |
| Yahoo          | `imap.mail.yahoo.com`       | 993  |
| ProtonMail     | `127.0.0.1` (Bridge)        | 1143 |

> Most providers require an **app password** when 2FA is enabled — generate one in your account security settings, not your regular login password.

Search shorthands the agent understands:
```
"unread"                    → UNSEEN
"today"                     → SINCE today's date
"from:boss@company.com"     → FROM "boss@company.com"
"subject:invoice"           → SUBJECT "invoice"
"unread from:bank today"    → combined
```

---

## Browser automation *(experimental)*

> ⚠️ **Experimental feature.** Browser automation works well for many tasks but is still being refined. Complex SPAs, heavily iframe-based pages, and sites with aggressive anti-bot measures may not work as expected. Feedback welcome.

The `browser` tool lets Aria control your real Chrome or Chromium browser on your behalf — navigating sites, clicking elements, filling forms, and reading content from pages where you are already logged in. Because it uses your actual browser with your existing sessions and cookies, there is no authentication to configure and no credentials to share with Aria.

### How it works

Most browser automation tools (Playwright, Selenium, Puppeteer) work by downloading their own browser binary and controlling it via a Node.js process. Aria takes a different approach: it talks directly to your existing Chrome or Chromium via the **Chrome DevTools Protocol (CDP)** — a standard HTTP + WebSocket API built into every Chrome/Chromium release. No Playwright, no Node.js, no bundled browsers.

```
Aria (Python) ──httpx──▶ http://localhost:9222/json  (tab list)
              ──websockets──▶ ws://localhost:9222/...  (CDP commands)
                                        │
                                        ▼
                            Your Chrome/Chromium
                            (with your sessions)
```

**Dependencies:** `websockets` (pure Python, ~50KB) — already included as a core dependency. Nothing else to install.

### Viewport-first design

Rather than parsing the entire page DOM (which can be millions of nodes on complex apps like Gmail), Aria only looks at what is currently **visible in the viewport**. This approach:

- Works identically on any page regardless of complexity
- Returns a compact, focused snapshot of what you actually see
- Scales naturally — scroll down and the next snapshot shows the next screen

For reading content, Aria extracts `innerText` from the main content element (`<main>`, `<article>`, `[role="main"]`) rather than downloading the full HTML — fast and bounded.

### Setup

```bash
# 1. Start your browser with the debug port enabled
# Snap Chromium (Ubuntu default — works out of the box)
chromium --remote-debugging-port=9222 --remote-allow-origins=*

# Google Chrome
google-chrome --remote-debugging-port=9222 --remote-allow-origins=*
```

```ini
# ~/.aria/.env
CHROME_PROFILE_DIR=~/snap/chromium/current/.config/chromium  # snap Chromium
# CHROME_PROFILE_DIR=~/.config/google-chrome                 # Google Chrome deb
CHROME_DEBUG_PORT=9222
ARIA_BROWSER_MAX_LOOPS=50   # browser tasks need more steps than regular tasks
```

During `aria-install`, answer **yes** to "Browser automation?" and it will ask for these values.

### Three Chrome states handled automatically

| State | What happens |
|-------|-------------|
| Browser running with `--remote-debugging-port` | Aria attaches to the active tab silently |
| Browser running **without** the debug flag | Aria notifies you to close it; relaunches automatically with the flag |
| Browser not running | Aria launches it with your profile |

### Available actions

| Action | Description |
|--------|-------------|
| `open` | Navigate to a URL |
| `snapshot` | See what is currently visible in the viewport (buttons, links, inputs, text) |
| `read` | Extract readable text content from the current page |
| `click` | Click an element by role + name, or by visible text |
| `type` | Type text into a focused input field |
| `scroll` | Scroll the page; next snapshot shows the new viewport |
| `back` | Go back in browser history |
| `query` | Run a JavaScript snippet for targeted data extraction |
| `resume` | Continue a paused task (after hitting the loop limit) |
| `close_tab` | Close the current tab |

### Long tasks and continuation

Browser tasks often require many steps. The loop limit is automatically raised to `ARIA_BROWSER_MAX_LOOPS=50` (vs the normal 20) when browser actions are detected. If a task is still paused at the limit:

```
You: archive all newsletters in Gmail
Aria: ⚠ Reached step limit. Progress: archived 18/31, on page 2.

You: continue the browser task
Aria: Resuming — last URL: mail.google.com, progress: archived 18/31...
```

### Snap Chromium note

Snap Chromium (the Ubuntu default) works with CDP — the snap sandbox does not block the debug port. No need to install Google Chrome as a deb package.

### Work PC / CLI-only

If `websockets` is not installed or the browser is not reachable, the tool returns a clear error and all other Aria features work normally. The `browser` tool has no impact on the CLI-only experience.

---

## Running as a background service

### Install wizard (recommended)

```bash
aria-install
```

Detects binaries, writes systemd service files, enables lingering (auto-start
on reboot), starts all services, and verifies they are running.

```bash
aria-install --services    # reinstall after git pull + pip install .
aria-install --uninstall   # remove all services
```

### Day-to-day management

```bash
# Status
systemctl --user status aria-telegram
systemctl --user status aria-supervisor

# Live logs
journalctl --user -fu aria-telegram
journalctl --user -fu aria-supervisor

# Restart after update
git pull && pip install . && systemctl --user restart aria-telegram aria-supervisor
```

### nohup (alternative, no systemd required)

```bash
nohup aria-telegram    >> ~/.aria/telegram.log    2>&1 &
nohup aria-supervisor  >> ~/.aria/supervisor.log  2>&1 &
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
│   ├── failed/
│   └── cancelled/
└── workspace/
    ├── memory/                               ← chmod 700; files 600
    │   ├── core.md                           ← explicit facts (REMEMBER: lines)
    │   ├── last_session.md                   ← rolling session summary
    │   ├── patterns.md                       ← behavioural patterns (aria-reflect)
    │   └── reflect_watermark                 ← tracks last analysed session
    ├── soul/
    │   └── identity.md                       ← agent persona (edit freely)
    ├── sessions/                             ← chmod 700; files 600
    │   └── session_YYYYMMDD_HHMMSS.md        ← per-session logs
    └── tools_registry/
        └── available_tools.md                ← auto-generated tool reference
```

---

## Project structure

```
aria-agent/
├── pyproject.toml
├── README.md
├── CLAUDE.md                          ← context for Claude Code
├── whatsapp/                          ← copy to ~/.aria/whatsapp/
│   ├── package.json
│   └── bridge.js
└── src/
    └── aria/
        ├── __init__.py                ← version via importlib.metadata
        ├── agent.py                   ← ReAct loop, streaming, Markdown render, session continuity
        ├── channel.py                 ← multi-channel registry, idle timer
        ├── config.py                  ← path resolution, .env loading
        ├── install.py                 ← setup wizard (aria-install)
        ├── main.py                    ← CLI entry point with rich + readline
        ├── reflect.py                 ← memory reflection engine (aria-reflect)
        ├── setup.py                   ← first-run wizard, env template
        ├── supervisor.py              ← task supervisor with periodic reflection (aria-supervisor)
        ├── task.py                    ← task model (JSON), queue ops, recurrence
        ├── telegram_bot.py            ← Telegram bot
        ├── telegram_notify.py         ← push-only Telegram sender
        ├── whatsapp_bridge.py         ← HTTP bridge for whatsapp-web.js
        ├── workspace.py               ← markdown persistence, secret redaction, permissions
        └── tools/
            ├── __init__.py            ← auto-loader and dispatcher
            ├── _env.py                ← subprocess environment builder
            ├── calendar.py            ← Google Calendar via gog
            ├── file_access.py         ← read/write/patch with path security
            ├── gmail.py               ← Gmail via gog
            ├── drive.py               ← Google Drive via gog
            ├── imap.py                ← IMAP email for any provider
            ├── jira.py                ← Jira REST API via httpx
            ├── notify.py              ← Telegram push notification
            ├── reflect.py             ← on-demand memory reflection
            ├── schedule.py            ← create/list/cancel supervisor tasks
            ├── shell_run.py           ← shell commands, script mode, interpreter whitelist
            └── web_fetch.py           ← web page fetcher
```
