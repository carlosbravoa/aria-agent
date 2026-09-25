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
- **Lean token usage** — careful context management, native tool calling and a cache-friendly prompt layout mean you get impressive capability at a fraction of the cost of comparable agents
- **Browser automation** *(experimental)* — control your real Chrome/Chromium with existing sessions via CDP; navigate, click, read content from any logged-in site

## What is on the roadmap

- **Knowledge base integration** — consuming content from document repositories, wikis, or vector stores for RAG-style retrieval
- **Your suggestions** — open an issue or ask the agent itself

---

## Table of contents

1. [Requirements](#requirements)
2. [Quickstart — CLI only](#quickstart--cli-only)
3. [Quickstart — with services](#quickstart--with-services)
   - [Upgrading to this version](#upgrading-to-this-version)
4. [Configure](#configure)
5. [Model profiles](#model-profiles)
6. [CLI commands](#cli-commands)
7. [Interactive REPL](#interactive-repl)
8. [Channels — Telegram](#telegram)
9. [Channels — WhatsApp](#whatsapp)
   - [Custom channels](#custom-channels)
10. [Scheduled tasks](#scheduled-tasks)
11. [Autonomous supervisor](#autonomous-supervisor)
12. [Memory](#memory)
13. [Memory reflection](#memory-reflection)
14. [Session continuity](#session-continuity)
15. [Tool calling](#tool-calling)
16. [Built-in tools](#built-in-tools)
17. [Web fetching](#web-fetching)
18. [Adding custom tools](#adding-custom-tools)
19. [Gmail & Calendar setup](#gmail--calendar-setup)
20. [Jira setup](#jira-setup)
21. [IMAP setup](#imap-setup)
22. [Browser automation (experimental)](#browser-automation-experimental)
23. [Running as a background service](#running-as-a-background-service)
24. [Development](#development)
25. [Workspace layout](#workspace-layout)
26. [Project structure](#project-structure)

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

### Upgrading to this version

After `git pull && pip install .`:

- **Re-run `aria-install --services`** — services now use a recovery template
  unit (`aria-rollback@.service`, via `OnFailure=`) instead of ending in a
  permanent failed state.
- **`/model` in a channel is now per conversation.** A switch on Telegram or
  WhatsApp is saved for that chat only (`~/.aria/.last_profile__<chat>`); the
  REPL keeps `~/.aria/.last_profile`, which background tasks also follow.
- **`./.env` in the current directory is no longer read.** Keep config in
  `~/.aria/.env`, or point at another file with `ARIA_ENV=./.env`.
- **Shell subprocesses no longer see Aria's own secrets** (API keys, tokens,
  passwords from `.env`). If a script you run through `shell_run` needs one,
  list its name in `ARIA_SHELL_ENV_ALLOW`.
- **`web_fetch`/`browser` block every non-public address range.** On Tailscale
  (100.64.0.0/10) or a fake-IP proxy such as Clash/sing-box (198.18.0.0/15),
  allow the range with `ARIA_NET_ALLOW`.
- **Channels are plugins now; nothing to change.** An existing `.env` with
  `TELEGRAM_TOKEN` and/or `WHATSAPP_ALLOWED` keeps both channels enabled.
  `aria-telegram` / `aria-whatsapp` and their systemd units are unchanged. The
  next `aria-install` run writes `ARIA_CHANNELS` explicitly. See
  [Custom channels](#custom-channels).

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
# ARIA_TELEGRAM_PROGRESS=on         # live tool-trail message while the agent works
# ARIA_TELEGRAM_STALL_MIN=10        # self-restart if polling is wedged for N min (0 = off)
# ARIA_INBOX_KEEP_DAYS=14           # retention for files sent to Aria
# ARIA_INBOX_MAX_MB=200             # inbox size cap, oldest pruned first

# ── WhatsApp ──────────────────────────────────────────────────────────────────
# Required for: aria-whatsapp (skip entirely if not using)
# ARIA_WA_PORT=7532
# ARIA_WA_SECRET=pick-any-random-string
# WHATSAPP_ALLOWED=34612345678      # international format, no +
# ARIA_WA_PUSH_PORT=7533            # Node listener for outbound push (notify tool)
# ARIA_WA_TIMEOUT=600               # seconds to wait for a reply; later replies are pushed

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
# ARIA_FILE_MAX_LINES=500          # max lines returned per file read
# Directories outside these can be granted on the fly — Aria asks, you approve
# in natural language, and the grant is saved to ~/.aria/authorized_dirs.json

# ── REPL ──────────────────────────────────────────────────────────────────────
# ARIA_REPL_MARKDOWN=on            # render Markdown in REPL; toggle live with /markdown

# ── Agent behaviour ───────────────────────────────────────────────────────────
# ARIA_MAX_LOOPS=20                # max tool-call loops per turn
# ARIA_MAX_HISTORY=60              # conversation turns kept in context
# ARIA_TOOL_NUDGE_EVERY=8          # every Nth same-tool call in a turn → step-back nudge (0 off)
# ARIA_TOOL_BROKEN_AFTER=3         # N consecutive same-tool failures → "tool may be broken" escalation (0 off)
# ARIA_FRICTION_MIN_CALLS=6        # flag high-friction turns to the user + friction log (0 off)
# ARIA_FRICTION_REFLECT_MIN=3      # reflection diagnoses the friction log at N events per tool (0 off)
# ARIA_CHANNEL_IDLE_MINUTES=60     # idle minutes before a channel session's window is trimmed
# ARIA_OPSMEM_MAX_LINES=40         # operational memory cap (learn tool entries)
# ARIA_WINDOW_MESSAGES=15          # exchanges kept in the rolling conversation window
# ARIA_WINDOW_MSG_CHARS=300        # chars per message before truncation

# ── Shell security ────────────────────────────────────────────────────────────
# ARIA_SHELL_UNATTENDED=safe       # channels/supervisor: safe | off | full
# ARIA_SHELL_SANDBOX=              # optional wrapper, e.g. "firejail --quiet --private-tmp"
# ARIA_SHELL_SAFE_EXTRA=make,pytest # extra read-only commands allowed in safe mode
# ARIA_SHELL_ENV_ALLOW=             # env var names passed to shell_run despite looking secret
# "always" approvals are saved to ~/.aria/shell_allowlist.json — audit with /trust

# ── Network ───────────────────────────────────────────────────────────────────
# web_fetch/browser refuse non-public addresses; allow trusted ranges explicitly
# ARIA_NET_ALLOW=100.64.0.0/10,198.18.0.0/15   # e.g. Tailscale, fake-IP proxies

# ── Browser automation (experimental) ────────────────────────────────────────
# Needs: pip install websockets, and chromium/chrome started with the debug port
# CHROME_PROFILE_DIR=~/snap/chromium/current/.config/chromium
# CHROME_DEBUG_PORT=9222
# ARIA_BROWSER_MAX_LOOPS=50        # higher loop budget for multi-step browser tasks
# ARIA_BROWSER_HUMANIZE=on         # human-like mouse paths/typing/scroll (off to disable)

# ── Supervisor ────────────────────────────────────────────────────────────────
# ARIA_SUPERVISOR_INTERVAL=30      # seconds between task queue polls
# ARIA_REFLECT_EVERY=86400         # seconds between reflection runs (0 = off)
# ARIA_REFLECT_NOTIFY=true         # Telegram notification after reflection

# ── Memory reflection ─────────────────────────────────────────────────────────
# ARIA_REFLECT_BATCH=10            # sessions analysed per batch
# ARIA_REFLECT_SESSION_CHARS=3000  # max chars read per session log
# ARIA_REFLECT_MAX_LINES=40        # max bullet points in patterns.md
# ARIA_REFLECT_SETTLE_MIN=10       # skip sessions active in the last N minutes

# ── Self-update ───────────────────────────────────────────────────────────────
# ARIA_SOURCE_DIR=~/aria-agent     # git checkout the update tool pulls from
# ARIA_UPDATE_BRANCH=main
# ARIA_UPDATE_CONFIRM_SEC=600      # rollback watchdog window after a service update

# ── Path overrides ────────────────────────────────────────────────────────────
# ARIA_ENV=~/.aria/.env
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

> **Note:** Profile switches are saved per conversation: the REPL (and single-shot/`--notify`) use `~/.aria/.last_profile`, each Telegram/WhatsApp chat its own `~/.aria/.last_profile__<chat>`. Scheduled tasks follow the REPL's saved profile and never change it.

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
Responses are rendered as Markdown — headings, bold, code blocks, lists — but only when actual Markdown is present, so plain prose stays clean.

| Command          | Description                                      |
|------------------|--------------------------------------------------|
| `/memory`        | Print current memory                             |
| `/tools`         | List all loaded tools                            |
| `/clear`         | Clear conversation history                       |
| `/compact`       | Summarize the conversation to reclaim context tokens |
| `/retry`         | Re-run your last message                         |
| `/copy`          | Copy the last answer to the clipboard            |
| `/save <note>`   | Append a note directly to memory                 |
| `/models`        | List available model profiles                    |
| `/model <name>`  | Switch model profile (persists across sessions)  |
| `/markdown`      | Toggle Markdown rendering on/off                 |
| `/cost`          | Show session token usage                         |
| `/trust [clear]` | Show/clear auto-approved shell commands          |
| `/version`       | Show version                                     |
| `/help`          | Show command list                                |
| `/quit`          | Exit (saves conversation window)                 |

At the prompt: `!cmd` runs a shell command directly, `@path/to/file` attaches a
file's contents, `Esc`/`Ctrl+C` interrupts a reply while keeping context, and
`Alt+Enter` inserts a newline.

On exit, the rolling conversation window is trimmed and saved to
`memory/conversation_window__repl.md`, and resumed as history in the next
session — no LLM summarisation step.

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

Bot commands: `/start` `/memory` `/tools` `/clear` `/save <note>` `/version` `/model [name]` `/models`

Replies **stream as the agent works**: each response arrives as its own message
as soon as it's ready, a typing indicator stays alive during long turns, and a
single live-edited "tool trail" message shows each tool call as it runs
(`ARIA_TELEGRAM_PROGRESS=on`, the default — set `off` to disable the trail).

After `ARIA_CHANNEL_IDLE_MINUTES` of inactivity the chat's conversation window
is trimmed and the session closed; it resumes with that context when you return.

### Sending and receiving files

**Send Aria a file** — documents, photos, videos and voice notes are saved to
`<workspace>/inbox/telegram/<chat id>/` and the agent is told where they are, so
it can work with them straight away. Send a PDF with "summarise this" and it
reads it; send a CSV and it can process it. Any caption you add travels with the
file as your message.

Two honest limits: Telegram's Bot API caps downloads at **20 MB**, and Aria
**cannot see image content or transcribe audio** — those files are stored and
acknowledged, not interpreted. The inbox prunes itself (`ARIA_INBOX_KEEP_DAYS`,
then oldest-first past `ARIA_INBOX_MAX_MB`), so it won't grow without bound.

**Ask Aria for a file** — "send me that report as a file" uses the `send_file`
tool, which uploads it as a document attachment (Telegram's limit is 50 MB).
It resolves paths through the *same* read allow-list as `file_access`, so
anything permanently blocked — `~/.ssh`, `~/.aria/.env`, cloud credentials —
can never be sent, and a path outside your allowed directories triggers the
usual "may I access this?" approval flow first.

> **Multi-user note:** replies, notifications and files now go to the chat you
> are actually talking in, rather than to every ID in `TELEGRAM_ALLOWED`.
> Identical behaviour for single-user setups.

---

## Custom channels

Telegram and WhatsApp are built-in **channel plugins**. You can add your own
(Discord, Matrix, Signal, a webhook for Home Assistant or n8n, …) by putting one
Python file in `~/.aria/channels/`. The plugin only moves text in and out; Aria
provides sessions, memory, tools and delivery routing.

```bash
cp docs/examples/channels/webhook.py ~/.aria/channels/   # a working example
aria-channel --list                                      # discovered + enabled channels
aria-install                                             # select it, configure, get a unit
```

| Setting | Meaning |
|---|---|
| `ARIA_CHANNELS=telegram,webhook` | Enabled channels. Unset → every configured channel (legacy behaviour). |
| `ARIA_NOTIFY_CHANNEL=webhook` | Where pushes go outside a conversation. Default: Telegram, when enabled. |
| `ARIA_CHANNELS_DIR` | Plugin directory. Default `~/.aria/channels`. |
| `ARIA_CHANNEL_MODE_TELEGRAM=attached` | Telegram is online only while the `aria` CLI is open, with no background service (`/remote` in the REPL). |

Full guide: [`docs/channel-plugins.md`](docs/channel-plugins.md).

---

## WhatsApp

Requires Node.js 18+.

`aria-install` (answer yes to WhatsApp) now **copies `bridge.js`, `package.json`
and `package-lock.json` into `~/.aria/whatsapp/` for you** and refreshes them on every reinstall; the
self-update tool refreshes them too, so the Node side always tracks the Python
side. It only touches those files — your `node_modules/` and login state
(`.wwebjs_auth/`) are left alone. You still run `npm ci` once (and again
if the package files changed — the installer/updater tells you when). The
lockfile pins the exact `whatsapp-web.js` build, which breaks often upstream.

```bash
# 1. Configure — run aria-install and answer yes to WhatsApp, or add to ~/.aria/.env:
# ARIA_WA_PORT=7532          # Python↔Node inbound bridge
# ARIA_WA_PUSH_PORT=7533     # Node listener for outbound push (notify tool)
# ARIA_WA_SECRET=your-secret
# WHATSAPP_ALLOWED=34612345678

# 2. Install Node deps (bridge files are already deployed by aria-install)
cd ~/.aria/whatsapp && npm ci

# 3. Start both processes
nohup aria-whatsapp >> ~/.aria/whatsapp.log 2>&1 &
nohup node ~/.aria/whatsapp/bridge.js >> ~/.aria/whatsapp-node.log 2>&1 &
```

Manual deploy (only if you're not using `aria-install`):
`mkdir -p ~/.aria/whatsapp && cp whatsapp/bridge.js whatsapp/package.json ~/.aria/whatsapp/`

On first run `bridge.js` shows a QR code — scan with WhatsApp once.
Auth persists in `~/.aria/whatsapp/.wwebjs_auth/`. Outbound push (the `notify`
tool replying on WhatsApp) uses `ARIA_WA_PUSH_PORT`.

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

Recurring occurrences also carry `series_id` (shared by every run of the
series) and `scheduled_for` (the slot the run belongs to, so retries don't
shift the schedule). These are filled in automatically.

### Recurring tasks

Set `recur` and the supervisor automatically re-enqueues the task after each
run — no need to reschedule manually. Creating the same task twice (same
prompt, recurrence and slot) returns the existing one; a running task can't
create recurring tasks; cancelling any occurrence stops the whole series.
Ask the agent to create one:

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

## Memory

Aria has two kinds of long-term memory that it writes to itself as you interact:

**Core memory** (`memory/core.md`) — permanent facts about you: your name, role,
timezone, language, preferences, recurring contacts. Aria writes here when it
learns something always true.

**Operational memory** (`memory/operational_memory.md`) — how to be useful in
*your* context: which accounts and tools to use for specific tasks, project keys,
calendar IDs, recurring task patterns, shortcuts it discovered while working.
This is what tailors Aria to your day-to-day over time. Capped at
`ARIA_OPSMEM_MAX_LINES` (default 40); reflection consolidates and prunes it.

Operational memory is injected into each session as *suggestions* — if an entry
turns out to be wrong or outdated, Aria verifies and replaces it rather than
blindly following it. The memory heals itself over time.

You don't manage these manually — Aria decides what's worth keeping. Both are
plain markdown you can inspect or edit directly if you want.

---

## Memory reflection

Scans session logs, extracts behavioural patterns, and consolidates both the
pattern file and operational memory. Loaded into every session automatically.

The supervisor runs reflection automatically every 24 hours
(`ARIA_REFLECT_EVERY=86400`). REPL users without the supervisor get the same
thing via a background thread on startup (once per day, non-blocking). Run
manually at any time:

```bash
aria-reflect
aria-reflect --notify    # Telegram notification when done
aria-reflect --verbose   # debug output
```

The agent can also trigger it mid-conversation:

```
You: analyse our past conversations and update your memory
Aria: 🔧 calling reflect...
      Reflection complete: 8 sessions analysed, patterns consolidated to 23 lines,
      operational memory consolidated to 12 entries.
```

**Three-phase process:**
1. **Extraction** — analyses only new sessions (watermark prevents re-processing)
2. **Pattern consolidation** — merges behavioural patterns, prunes weak signals,
   caps at `ARIA_REFLECT_MAX_LINES`
3. **Operational memory consolidation** — deduplicates entries on the same topic,
   corrects ones contradicted by recent sessions, prunes vague ones

---

## Session continuity

A rolling window of the last `ARIA_WINDOW_MESSAGES` messages is kept per
conversation in `memory/conversation_window__<key>.md` (`repl` for the terminal,
`telegram_<chat>` / `whatsapp_<number>` for channels) and replayed as real
conversation history in the next session. No LLM summarisation on exit — the
window is just trimmed, so it works instantly and offline. `/clear` deletes it.

Works across all interfaces: REPL, single-shot, Telegram, WhatsApp.

---

## Tool calling

Aria 2.x uses the provider's native tool/function-calling API: tool schemas are
sent with every request and the model returns structured `tool_calls`, so no
JSON has to be parsed out of free text. This requires a tool-aware model; for
models without tool support, use Aria 1.x (plain-text protocol). Several calls
in one turn are supported, and read-only tools run in parallel.

Memory is written through tools too: `remember` (facts about you) and `learn`
(operational notes).

Tools are auto-discovered from `src/aria/tools/` and `~/.aria/tools/`
at startup — no registration needed.

---

## Built-in tools

| Tool          | Description                                                               |
|---------------|---------------------------------------------------------------------------|
| `file_access` | Read, write, append, patch, **edit (multi)**, **replace_lines**, list, delete, **undo** files. Reads **PDFs** by extracting their text automatically. `base64` encoding + paginated reads (`offset`/`limit`). Read/write restricted to configured directories. |
| `send_file`   | Send a file from disk to the user over Telegram as a downloadable attachment. Goes through the same read allow-list as `file_access`, so blocked paths can never be sent. |
| `code_search` | Fast content/filename search across a tree (ripgrep → git grep → Python fallback; respects `.gitignore`). Locate code/symbols/TODOs before reading whole files. |
| `git`         | Common git ops: status, diff, log, show, branch, checkout, add, commit, push, pull. No shell string assembly. |
| `plan`        | Track a multi-step task as a todo checklist (rendered live in the REPL); update statuses as you go. |
| `shell_run`   | Run shell commands or scripts. Use `script` field for commands with quotes (AWS CLI, jq, SQL) — no JSON escaping needed. Interpreter whitelist enforced. Interactive `[y/N/always]` approval for risky ops; optional `ARIA_SHELL_SANDBOX` isolation. |
| `web_fetch`   | Fetch readable text from a web page using trafilatura for clean content extraction. |
| `gmail`       | Search, read, send, mark-read via `gog` CLI.                              |
| `calendar`    | List, create, update, delete, RSVP Google Calendar events via `gog`.      |
| `notify`      | Push a message to the user on the channel of the current turn (Telegram or WhatsApp); broadcasts via Telegram outside a channel. |
| `remember`    | Save / `list` / `forget` permanent user facts in core memory. |
| `learn`       | Save / `list` / `forget` operational notes (global or project-scoped). |
| `memory_search` | Search across all memory stores (core, operational, patterns, project notes) for a phrase — on-demand recall without loading all memory into context. |
| `schedule`    | Create, list, and cancel scheduled tasks for the supervisor.              |
| `reflect`     | Trigger memory reflection on demand.                                      |
| `jira`        | Create, search, comment, transition Jira issues via REST API.             |
| `browser`     | *(experimental)* Control Chrome/Chromium via CDP — viewport-based snapshots, click, type, read, scroll, with human-like input by default. Uses your real sessions. |
| `imap`        | List, search, read, mark read/unread, move emails, list folders on any IMAP provider. |
| `drive`       | List, search, read, download, upload, organise Google Drive files via gog. |
| `update`      | Self-update from the git source checkout: fetch, diff, dry-run import check, apply. Service updates arm a rollback watchdog (`aria-rollback`) that auto-reverts a crash-looping update within `ARIA_UPDATE_CONFIRM_SEC`. |

### Writing scripts without JSON escaping issues

Use the `script` field for any command containing quotes, backticks, or special characters.
This avoids JSON escaping failures entirely — the script is written to a temp file and executed:

```json
{"script": "aws ec2 describe-instances --query 'Reservations[*].Instances[*].InstanceId'"}
```

```json
{"script": "print('hello world')", "interpreter": "python3"}
```

If `command` is used and contains quotes, Aria automatically redirects to script mode.

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

### Directory authorization flow

When the agent tries to access a path outside the allow-list, instead of failing
it asks you naturally:

```
You: read the config at /home/carlos/projects/config.yaml
Aria: I need read access to /home/carlos/projects/ to do that.
      Would you like me to grant read access to that directory?

You: yes go ahead
Aria: Got it. Access granted. [reads the file and continues]
```

Granted directories are saved to `~/.aria/authorized_dirs.json` and
remembered across sessions. Sensitive system paths (`~/.ssh`, `~/.aria/.env`,
etc.) can never be authorized regardless of what you say.

Works identically in REPL, Telegram, and WhatsApp.

---

## Web fetching

`web_fetch` uses [trafilatura](https://trafilatura.readthedocs.io) for content extraction — the same approach as Firefox Reader Mode. It identifies the main article or documentation body and discards navigation, ads, footers, and boilerplate, dramatically improving signal-to-noise ratio compared to plain HTML stripping.

**SSRF protection:** agent-driven fetches (`web_fetch` and `browser` navigation)
refuse private, loopback, link-local, and reserved addresses — including the
cloud metadata endpoint `169.254.169.254` — and non-http(s) schemes, and the
check is re-applied at every redirect hop. A channel user or a prompt-injected
page can't point Aria at your internal network.

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
chromium --remote-debugging-port=9222 --remote-allow-origins=http://localhost

# Google Chrome
google-chrome --remote-debugging-port=9222 --remote-allow-origins=http://localhost
```

> The allow-origin is a **fixed value Aria's CDP client sends** — not `*`. A web
> page can't forge its `Origin` header, so this keeps random pages from talking
> to your browser's debug port.

```ini
# ~/.aria/.env
CHROME_PROFILE_DIR=~/snap/chromium/current/.config/chromium  # snap Chromium
# CHROME_PROFILE_DIR=~/.config/google-chrome                 # Google Chrome deb
CHROME_DEBUG_PORT=9222
ARIA_BROWSER_MAX_LOOPS=50   # browser tasks need more steps than regular tasks
ARIA_BROWSER_HUMANIZE=on    # human-like input (default on; off = fast direct dispatch)
```

Clicks, typing, and scrolling dispatch through **human-like input** by default:
curved pointer paths, natural typing cadence, wheel-based scrolling, and a
cursor position that stays continuous across actions. Set
`ARIA_BROWSER_HUMANIZE=off` to revert to instant direct dispatch.

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

Each service has `OnFailure=aria-rollback@%n.service`: if a service
crash-loops within `ARIA_UPDATE_CONFIRM_SEC` of a self-update, the
`aria-rollback` template unit reverts to the previous commit and restarts it.

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
├── .last_profile                             ← REPL model profile (.last_profile__<chat> per channel chat)
├── authorized_dirs.json                      ← directories granted on the fly
├── browser_state.json                        ← paused browser task (for resume)
├── shell_allowlist.json                      ← "always" shell approvals (/trust)
├── usage.jsonl                               ← token usage log (aria --usage)
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
    │   ├── core.md                           ← user facts (remember tool)
    │   ├── operational_memory.md             ← procedures/shortcuts (learn tool)
    │   ├── project_notes/                    ← per-repository notes (learn scope=project)
    │   ├── conversation_window__<key>.md     ← rolling last N messages, one per conversation
    │   ├── plan__<key>.json                  ← active task plan per conversation
    │   ├── patterns.md                       ← behavioural patterns (aria-reflect)
    │   ├── notify_feed.md                    ← recent proactive messages
    │   ├── friction_log.md                   ← turns where tools kept failing
    │   └── reflect_watermark                 ← tracks last analysed session
    ├── soul/
    │   └── identity.md                       ← agent persona (edit freely)
    ├── sessions/                             ← chmod 700; files 600
    │   └── session_YYYYMMDD_HHMMSS_ffffff.md ← per-session logs
    ├── inbox/                                ← files sent to Aria over channels
    └── tools_registry/
        └── available_tools.md                ← auto-generated tool reference
```

---

## Development

Aria is built to be taken forward with a coding agent (e.g. Claude Code) or by
hand. Keep any Claude Code context file (`CLAUDE.md`) local — outside the
repo or git-ignored; it is not part of the project.

### Running the tests

```bash
pip install ".[dev]"   # installs pytest + pytest-mock
pytest                 # run the 600+ tests (a few seconds)
pytest tests/test_native_tools.py -v
pytest --cov=aria --cov-report=term-missing   # coverage (needs pytest-cov)
ruff check src tests   # lint
mypy                   # type-check (config in pyproject.toml)
```

Tests cover the native tool loop, workspace persistence and locking, the task
queue and recurring-task dedupe, channels, tool security (path blocking, shell
policy, SSRF guard, secret stripping), reflection, self-update, and import
smoke tests (every module imports, required module-level symbols exist).

### Conventions

- **Tools** are auto-discovered from `src/aria/tools/`. Add a file with a
  `DEFINITION` dict and an `execute(args)` function — no registration needed.
  Files starting with `_` are treated as helpers, not tools.
- **After editing any module**, run an actual import (not just a syntax check) —
  `ast.parse` does not catch a referenced-but-undefined module-level constant.
  Then run `pytest`:
  ```bash
  cd src && ARIA_ENV=/dev/null LLM_BASE_URL=x LLM_API_KEY=x LLM_MODEL=x python3 -c "
  import aria.agent, aria.workspace, aria.channel, aria.supervisor, aria.reflect, aria.task, aria.main
  from aria import tools; print(len(tools.load_all()), 'tools; all modules import OK')"
  ```
  `pre-commit install` (after `pip install ".[dev]"`) runs this plus
  `ruff check src tests` and `mypy` on every commit.
- **New env vars** go in `setup.py`'s template as commented placeholders, and in
  the README's Configure section.

### Roadmap

Open work (modernisation, refactors, features) is tracked in `docs/ROADMAP.md`;
parked tool-specific items in `docs/BACKLOG.md`. Native function calling —
long the most important item — shipped in 2.0; its design notes are kept in
`docs/native-function-calling-spec.md`.

---

## Project structure

```
aria-agent/
├── pyproject.toml                     ← deps + [dev] extra + pytest config
├── README.md
├── docs/
│   ├── ROADMAP.md                     ← open work
│   ├── BACKLOG.md                     ← parked tool items
│   └── native-function-calling-spec.md  ← 2.0 design (implemented)
├── tests/                             ← 600+ tests (pytest), conftest.py + ~33 test files
├── whatsapp/                          ← deployed to ~/.aria/whatsapp/ by aria-install / update
│   ├── package.json
│   └── bridge.js
└── src/
    └── aria/
        ├── __init__.py                ← version via importlib.metadata
        ├── agent.py                   ← native tool-calling ReAct loop, markdown toggle, model profiles, background reflection
        ├── attachments.py             ← inbound file storage: name sanitising, inbox layout, retention
        ├── channel.py                 ← session registry per (channel, user), idle timer
        ├── telegram_*.py, whatsapp_*.py ← legacy aliases of the modules in channels/
        ├── channels/                  ← channel plugins: base.py contract, registry, host API,
        │   │                            cli.py (aria-channel); user plugins in ~/.aria/channels/
        │   ├── telegram/              ← bot.py, notify.py (+ plugin in __init__.py)
        │   └── whatsapp/              ← bridge.py, notify.py, deploy.py
        ├── config.py                  ← path resolution, .env loading
        ├── context.py                 ← active channel/user for the current turn (delivery routing)
        ├── install.py                 ← setup wizard (aria-install)
        ├── main.py                    ← CLI entry point (prompt_toolkit REPL + rich)
        ├── project.py                 ← per-project conventions file + project-scoped notes
        ├── reflect.py                 ← three-phase memory reflection (aria-reflect)
        ├── setup.py                   ← first-run wizard, env template
        ├── supervisor.py              ← task supervisor with periodic reflection (aria-supervisor)
        ├── task.py                    ← task model (JSON), queue ops, recurrence
        ├── usage.py                   ← token usage log summary (aria --usage, /usage)
        ├── workspace.py               ← markdown persistence, secret redaction, permissions, file locks
        └── tools/
            ├── __init__.py            ← auto-loader and dispatcher
            ├── _env.py                ← subprocess environment builder (optionally strips Aria's secrets)
            ├── _net.py                ← shared SSRF guard for outbound fetches
            ├── browser.py             ← Chrome/Chromium via raw CDP (httpx + websockets), humanized input
            ├── calendar.py            ← Google Calendar via gog
            ├── code_search.py         ← ripgrep/git-grep/python code + filename search
            ├── drive.py               ← Google Drive via gog
            ├── file_access.py         ← read/write/patch/edit/undo + PDF extraction + path security
            ├── git.py                 ← git status/diff/log/commit/push/pull without shell strings
            ├── gmail.py               ← Gmail via gog
            ├── imap.py                ← IMAP email for any provider
            ├── jira.py                ← Jira REST API via httpx
            ├── learn.py               ← add/list/forget operational notes (global or project)
            ├── memory_search.py       ← search across all memory stores
            ├── notify.py              ← push notification on the current channel (Telegram/WhatsApp)
            ├── plan.py                ← task-plan/todo checklist (rendered live in the REPL)
            ├── reflect.py             ← on-demand memory reflection
            ├── remember.py            ← add/list/forget permanent user facts
            ├── schedule.py            ← create/list/cancel supervisor tasks
            ├── send_file.py           ← send a file to the user over Telegram
            ├── shell_run.py           ← shell commands, script mode, learnable approval, opt-in sandbox
            ├── update.py              ← self-update from git source + rollback watchdog
            └── web_fetch.py           ← web page fetcher (trafilatura)
```
