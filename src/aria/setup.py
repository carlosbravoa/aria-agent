"""
aria/setup.py — First-run setup wizard.

Called automatically when ~/.aria/.env does not exist.
Creates ~/.aria/, writes a .env template, prints instructions, and exits.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ENV_TEMPLATE = """\
# ── Aria configuration ────────────────────────────────────────────────
# LLM endpoint (any OpenAI-compatible API)
LLM_BASE_URL=http://localhost:11434/v1

# API key — use any string for local models (Ollama, LM Studio, etc.)
LLM_API_KEY=ollama

# Model name — must be available at your endpoint.
# Aria 2.0 requires a model that supports native tool/function calling
# (e.g. Claude, GPT-4o). Models without tool support → use Aria 1.x.
LLM_MODEL=llama3.2

# Display name used in the terminal
AGENT_NAME=Aria

# ── Optional overrides ────────────────────────────────────────────────
# Uncomment to move the workspace or custom tools directory elsewhere
# ── Model profiles (optional) ────────────────────────────────────────
# Switch with /model <name> in REPL or Telegram
# Unset fields inherit from LLM_BASE_URL / LLM_API_KEY above
# LLM_PROFILE1_NAME=fast
# LLM_PROFILE1_MODEL=claude-haiku-4-5-20251001
# LLM_PROFILE1_BASE_URL=   # optional
# LLM_PROFILE1_API_KEY=    # optional
#
# LLM_PROFILE2_NAME=local
# LLM_PROFILE2_MODEL=llama3.2
# LLM_PROFILE2_BASE_URL=http://localhost:11434/v1
# LLM_PROFILE2_API_KEY=ollama
#
# LLM_PROFILE3_NAME=strong
# LLM_PROFILE3_MODEL=claude-opus-4-6

# ── Path overrides ───────────────────────────────────────────────────
# ARIA_WORKSPACE=~/.aria/workspace
# ARIA_TOOLS_DIR=~/.aria/tools
# Path to the source directory — used by the update tool
# ARIA_SOURCE_DIR=~/aria-agent
# ARIA_UPDATE_BRANCH=main

# ── File access security ─────────────────────────────────────────────
# Directories the agent can READ (colon-separated, workspace always included)
# ARIA_FILE_READ_DIRS=~/Documents:~/Downloads:~/projects
# Directories the agent can WRITE (colon-separated, workspace always included)
# ARIA_FILE_WRITE_DIRS=~/projects
# Delete is always restricted to workspace only.

# ── Gmail (optional) ─────────────────────────────────────────────────
# CLI binary used for Gmail access. Run `gog auth login` to authenticate.
# ── IMAP (optional — any non-Gmail provider) ─────────────────────────
# IMAP_DEFAULT_HOST=imap.example.com
# IMAP_DEFAULT_USER=you@example.com
# IMAP_DEFAULT_PASSWORD=app-password
# IMAP_DEFAULT_PORT=993
# Additional accounts: IMAP_WORK_HOST, IMAP_WORK_USER, etc.

# ── Gmail / gog ──────────────────────────────────────────────────────
# GMAIL_CLI=gog                  # also used for Drive, Calendar
# GOG_ACCOUNT=you@gmail.com
# GOG_KEYRING_BACKEND=file
# GOG_KEYRING_PASSWORD=your-passphrase
# Keyring config for headless/background operation (required after re-auth below)
# GOG_KEYRING_BACKEND=file
# GOG_KEYRING_PASSWORD=pick-a-strong-passphrase

# ── Agent behaviour ──────────────────────────────────────────────────
# Max tool-call loops per turn (raise if agent hits limit on complex tasks)
# Conversation window
# ── Browser automation (optional) ───────────────────────────────────
# CHROME_PROFILE_DIR=~/.config/google-chrome
# CHROME_DEBUG_PORT=9222
# ARIA_BROWSER_MAX_LOOPS=50

# Conversation window
# ARIA_WINDOW_MESSAGES=15   # how many messages to keep across sessions
# ARIA_WINDOW_MSG_CHARS=300 # max chars per message before truncation

# ARIA_MAX_LOOPS=20
# Max conversation history turns kept in context
# ARIA_MAX_HISTORY=60
# Every Nth call to the SAME tool in one turn nudges the model to step back
# instead of probing with small variations (0 disables)
# ARIA_TOOL_NUDGE_EVERY=8
# After N consecutive failures of the same tool, tell the model to consider
# the TOOL broken and report it instead of working around it (0 disables)
# ARIA_TOOL_BROKEN_AFTER=3
# Flag a high-friction turn to the user (>=N tool calls with >40% errors,
# repeated calls, or a hard stop) and log it for reflection (0 disables)
# ARIA_FRICTION_MIN_CALLS=6
# shell_run policy in non-interactive contexts (Telegram/WhatsApp/supervisor):
#   safe (default) = block destructive + secret-path commands, allow the rest
#   off            = no shell outside the interactive REPL
#   full           = legacy (destructive still blocked, secret-path allowed)
# ARIA_SHELL_UNATTENDED=safe
# Optional real isolation: a command prefix that every shell_run invocation is
# wrapped in (needs the binary installed). Empty = no sandbox.
#   ARIA_SHELL_SANDBOX=firejail --quiet --private-tmp
# In the REPL, approving a risky command with "always" remembers its prefix in
# ~/.aria/shell_allowlist.json (manage with /trust); it then runs without asking.
# Minutes of inactivity before a Telegram/WhatsApp session is trimmed + dropped
# ARIA_CHANNEL_IDLE_MINUTES=60
# Telegram: show a live tool-progress trail message while a turn runs (on/off)
# ARIA_TELEGRAM_PROGRESS=on
# Telegram: restart the bot if no getUpdates poll succeeds for N minutes
# (self-heals a wedged polling loop after a network drop; 0 disables)
# ARIA_TELEGRAM_STALL_MIN=10

# ── Channels (messaging plugins) ─────────────────────────────────────
# Enabled channels, comma-separated (aria-install writes this). Unset → every
# channel whose settings are present is enabled (TELEGRAM_TOKEN → telegram,
# WHATSAPP_ALLOWED → whatsapp), as before plugins existed. List them with
# `aria-channel --list`.
# ARIA_CHANNELS=telegram,whatsapp
# Where pushes outside a conversation go (supervisor results, reflection
# notices, `aria --notify`, the notify tool in the REPL). Unset → telegram when
# enabled, else the first enabled channel that can push.
# ARIA_NOTIFY_CHANNEL=telegram
# Directory of user channel plugins (*.py drop-ins, see docs/channel-plugins.md)
# ARIA_CHANNELS_DIR=~/.aria/channels
# Run mode per channel: service (default — a background systemd unit, always
# online), attached (online only while the `aria` CLI is open; toggle with
# /remote) or control (attached, and your phone drives the terminal's own
# session — /remote control|release). Telegram supports attached/control;
# WhatsApp is service-only.
# ARIA_CHANNEL_MODE_TELEGRAM=attached

# ── Inbound attachments ──────────────────────────────────────────────
# Files sent over a channel are saved under <workspace>/inbox/ and pruned:
# first anything older than KEEP_DAYS, then oldest-first past MAX_MB.
# ARIA_INBOX_KEEP_DAYS=14
# ARIA_INBOX_MAX_MB=200

# ── Memory reflection ────────────────────────────────────────────────
# Sessions to analyse per reflection batch
# ARIA_REFLECT_BATCH=10
# Max chars read per session log during reflection
# ARIA_REFLECT_SESSION_CHARS=3000
# Max bullet points kept in patterns.md after consolidation
# ARIA_REFLECT_MAX_LINES=40
# Diagnose accumulated high-friction turns once one tool has N events
# (suspected systemic issues → ops memory + notify; 0 disables)
# ARIA_FRICTION_REFLECT_MIN=3

# ── Supervisor ───────────────────────────────────────────────────────
# Seconds between task queue polls
# ── Jira (optional) ──────────────────────────────────────────────────
# JIRA_BASE_URL=https://yourcompany.atlassian.net
# JIRA_EMAIL=you@yourcompany.com
# JIRA_API_TOKEN=your-api-token
# JIRA_DEFAULT_PROJECT=PROJ

# ── Supervisor ───────────────────────────────────────────────────────
# ARIA_SUPERVISOR_INTERVAL=30
# Seconds between reflection runs (0 = disabled, default = 86400 = 24h)
# ARIA_REFLECT_EVERY=86400
# Send Telegram notification after each reflection run
# ARIA_REFLECT_NOTIFY=true
# Wall-clock ceiling per task; also the running/ reaper lease (0 = disabled)
# ARIA_TASK_TIMEOUT=900
# Retry backoff for failed tasks: delay = base * 2^(attempt-1), capped at max
# ARIA_TASK_RETRY_BASE=60
# ARIA_TASK_RETRY_MAX=3600
# Timezone for scheduling/recurrence (IANA name; default = system local)
# ARIA_TZ=Europe/Madrid

# ── LLM resilience & context (new in 2.5) ────────────────────────────
# Automatic retry with backoff + request timeout on every model call
# ARIA_LLM_RETRIES=4
# ARIA_LLM_TIMEOUT=120           # overall seconds
# ARIA_LLM_CONNECT_TIMEOUT=10    # connect seconds
# Token-aware context management (heuristic ~4 chars/token):
#   hard cap — trim oldest turns to fit; 0 disables
# ARIA_CONTEXT_TOKENS=32000
#   soft trigger — auto-summarize older turns above this; 0 disables
# ARIA_COMPACT_AT=24000
# ARIA_COMPACT_CHUNK_CHARS=12000
# Does your LLM endpoint accept the `system` role? (yes = default). Set to no for
# the few endpoints without a system role — the prompt is then sent as a leading
# user turn. When yes, the per-turn timestamp rides in a trailing system message
# so the prompt + history stay a cacheable prefix.
# LLM_SYSTEM_MESSAGES=yes
# Persist per-call token usage to ~/.aria/usage.jsonl (see `aria --usage`)
# ARIA_USAGE_LOG=on
# Soft cap on core-memory facts kept after reflection consolidation
# ARIA_CORE_MAX_LINES=80
# Extra leading commands allowed for shell_run in non-interactive 'safe' mode
# ARIA_SHELL_SAFE_EXTRA=make,pytest

# ── WhatsApp outbound push (new in 2.5) ──────────────────────────────
# Port the Node bridge listens on for Python→WhatsApp pushes (notify tool)
# ARIA_WA_PUSH_PORT=7533
# Seconds the bridge waits for a reply before telling the user "still working"
# (a later reply is pushed instead of lost)
# ARIA_WA_TIMEOUT=600
# Extra env var names passed to shell_run despite looking like secrets
# ARIA_SHELL_ENV_ALLOW=
# Minutes a session must be idle before reflection analyses it
# ARIA_REFLECT_SETTLE_MIN=10
# Non-public network ranges web_fetch/browser may reach (comma-separated CIDRs),
# e.g. Tailscale or a fake-IP proxy: 100.64.0.0/10,198.18.0.0/15
# ARIA_NET_ALLOW=
"""

_BANNER = """
╭─────────────────────────────────────────────╮
│           Welcome to Aria  ✦                │
╰─────────────────────────────────────────────╯
"""

_INSTRUCTIONS = """
{aria_dir} has been created with a default .env file.

  Next steps
  ──────────
  1. Edit the config:
       {env_path}

  2. Set your LLM endpoint and model, e.g. for Ollama:
       LLM_BASE_URL=http://localhost:11434/v1
       LLM_MODEL=llama3.2

     Or for OpenAI:
       LLM_BASE_URL=https://api.openai.com/v1
       LLM_API_KEY=sk-...
       LLM_MODEL=gpt-4o-mini

  3. Start Aria:
       aria

  Workspace     {workspace_dir}
  Custom tools  {tools_dir}
    (drop .py tool files here — auto-loaded on next start)

  Run `aria --help` at any time to see available commands.
"""


def write_private(path: Path, content: str) -> None:
    """Atomically write `content` to `path` with 0600 perms from creation.

    The temp file is created by mkstemp (0600) in the same directory, so there
    is no window where secrets sit in a world-readable file, and a crash can't
    leave a truncated .env behind. Shared by setup and aria-install."""
    import os
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def is_first_run() -> bool:
    """Return True if no .env exists in any of the expected locations."""
    import os
    if os.environ.get("ARIA_ENV"):
        return False
    if (Path.home() / ".aria" / ".env").exists():
        return False
    # No ./.env (cwd) check: config.load() no longer reads it (see config.py).
    # But say so, rather than silently ignoring a config that used to work.
    if Path(".env").exists() and "LLM_BASE_URL" in Path(".env").read_text(
            encoding="utf-8", errors="ignore"):
        print("Note: ./.env in the current directory is no longer read "
              "automatically. Move it to ~/.aria/.env, or run with "
              "ARIA_ENV=./.env.")
    return True


def run() -> None:
    """Create ~/.aria/, write .env template, print instructions, exit."""
    aria_dir   = Path.home() / ".aria"
    env_path   = aria_dir / ".env"
    tools_dir  = aria_dir / "tools"
    ws_dir     = aria_dir / "workspace"

    # Create directories. ~/.aria holds .env (API keys) and memory — owner-only.
    for d in (aria_dir, tools_dir, ws_dir):
        d.mkdir(parents=True, exist_ok=True)
    try:
        aria_dir.chmod(0o700)
    except OSError:
        pass

    # Write .env only if it doesn't exist (safety check). Created 0600 so the
    # keys it will hold are never world-readable, not even briefly.
    if not env_path.exists():
        write_private(env_path, _ENV_TEMPLATE)

    print(_BANNER)
    print(_INSTRUCTIONS.format(
        aria_dir=aria_dir,
        env_path=env_path,
        workspace_dir=ws_dir,
        tools_dir=tools_dir,
    ))
    sys.exit(0)
