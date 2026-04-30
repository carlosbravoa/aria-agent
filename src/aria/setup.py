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

# Model name — must be available at your endpoint
LLM_MODEL=llama3.2

# Display name used in the terminal
AGENT_NAME=Aria

# ── Optional overrides ────────────────────────────────────────────────
# Uncomment to move the workspace or custom tools directory elsewhere
# ARIA_WORKSPACE=~/.aria/workspace
# ARIA_TOOLS_DIR=~/.aria/tools

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
# ARIA_MAX_LOOPS=20
# Max conversation history turns kept in context
# ARIA_MAX_HISTORY=60
# Minutes of inactivity before a Telegram/WhatsApp session is summarised
# ARIA_CHANNEL_IDLE_MINUTES=60

# ── Memory reflection ────────────────────────────────────────────────
# Sessions to analyse per reflection batch
# ARIA_REFLECT_BATCH=10
# Max chars read per session log during reflection
# ARIA_REFLECT_SESSION_CHARS=3000
# Max bullet points kept in patterns.md after consolidation
# ARIA_REFLECT_MAX_LINES=40

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


def is_first_run() -> bool:
    """Return True if no .env exists in any of the expected locations."""
    import os
    if os.environ.get("ARIA_ENV"):
        return False
    if (Path.home() / ".aria" / ".env").exists():
        return False
    if Path(".env").exists():
        return False
    return True


def run() -> None:
    """Create ~/.aria/, write .env template, print instructions, exit."""
    aria_dir   = Path.home() / ".aria"
    env_path   = aria_dir / ".env"
    tools_dir  = aria_dir / "tools"
    ws_dir     = aria_dir / "workspace"

    # Create directories
    for d in (aria_dir, tools_dir, ws_dir):
        d.mkdir(parents=True, exist_ok=True)

    # Write .env only if it doesn't exist (safety check)
    if not env_path.exists():
        env_path.write_text(_ENV_TEMPLATE, encoding="utf-8")

    print(_BANNER)
    print(_INSTRUCTIONS.format(
        aria_dir=aria_dir,
        env_path=env_path,
        workspace_dir=ws_dir,
        tools_dir=tools_dir,
    ))
    sys.exit(0)
