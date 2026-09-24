"""
aria/tools/_env.py — Shared subprocess environment helper.

When Aria runs as a background service (nohup, systemd, Telegram bot, etc.)
it may not inherit the user's full shell environment. This module builds an
env dict that includes:
  - A full PATH covering all common user binary locations
  - HOME, XDG dirs so CLI tools can find their config/tokens
  - All vars defined in ~/.aria/.env (highest priority)
    This is where you put tool-specific vars like GMAIL_ACCOUNT, API keys, etc.
"""

from __future__ import annotations

import os
import re
from pathlib import Path


# Env var NAMES that look like credentials. Stripped from the environment handed
# to arbitrary-code subprocesses (shell_run, code_search) so `printenv`/`env`/
# `echo $LLM_API_KEY` can't dump Aria's secrets. Tools that genuinely need a
# secret (gog needs GOG_KEYRING_PASSWORD) keep the full env.
_SECRET_NAME_RE = re.compile(
    r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|_PASS$|_PWD$", re.I)
# ...except these non-secret names the pattern would otherwise catch.
_NOT_SECRET = frozenset({"GOG_KEYRING_BACKEND"})
# Only ARIA'S OWN secrets are stripped: keys defined in ~/.aria/.env, plus
# secret-looking names with these prefixes (config.load() copies .env into the
# process env). The user's own environment — GH_TOKEN, AWS_* credentials,
# GNOME_KEYRING_CONTROL, PASSWORD_STORE_DIR — is theirs and keeps working.
_ARIA_PREFIXES = ("LLM_", "TELEGRAM_", "WHATSAPP_", "ARIA_", "JIRA_", "IMAP_",
                  "GOG_", "GMAIL_", "OPENAI_", "ANTHROPIC_")


def _is_secret_name(name: str) -> bool:
    if name in _NOT_SECRET:
        return False
    return bool(_SECRET_NAME_RE.search(name))


# Commands that require an interactive TTY and will hang or fail in background.
# shell_run uses this to reject them early with a clear message.
# Always interactive — block regardless of arguments.
TTY_COMMANDS = frozenset({
    "top", "htop", "btop", "vim", "vi", "nano", "emacs", "less", "more",
    "man", "ssh", "telnet", "ftp", "sftp", "screen", "tmux", "watch",
})
# Interactive ONLY when run bare (a REPL). With a script/args they're fine —
# `python3 file.py`, `node app.js`, `bash script.sh`, `mysql -e '...'` must NOT
# be rejected (this was the over-broad block that forced everything to 'script').
REPL_COMMANDS = frozenset({
    "python", "python3", "ipython", "irb", "node", "bash", "sh", "zsh",
    "fish", "mysql", "psql", "sqlite3",
})


def is_tty_command(command: str) -> bool:
    """Return True if the command is likely to require an interactive TTY."""
    parts = command.strip().split()
    if not parts:
        return False
    binary = Path(parts[0]).name           # strip path (e.g. /usr/bin/vim → vim)
    if binary in TTY_COMMANDS:
        return True
    return binary in REPL_COMMANDS and len(parts) == 1   # bare REPL only


def gog_keyring_hint(text: str) -> str:
    """Return an actionable setup hint if `text` looks like a gog keyring /
    credential-store failure, else "". Headless/systemd gog needs
    GOG_KEYRING_BACKEND=file + GOG_KEYRING_PASSWORD, and the failure is otherwise
    an opaque non-zero exit."""
    low = (text or "").lower()
    if any(k in low for k in ("keyring", "secretstorage", "no password",
                              "locked", "dbus", "could not be opened",
                              "no such interface")):
        return ("\nHint: for headless/systemd use, set GOG_KEYRING_BACKEND=file "
                "and GOG_KEYRING_PASSWORD in ~/.aria/.env.")
    return ""


def build_env(include_secrets: bool = True) -> dict[str, str]:
    """
    Return an environment dict suitable for subprocess calls from a
    background process.

    Priority (highest to lowest):
      1. Variables in ~/.aria/.env  ← put GMAIL_ACCOUNT etc. here
      2. Current process environment
      3. Constructed PATH and XDG defaults

    include_secrets=False drops Aria's own secret-looking vars (see
    _SECRET_NAME_RE / _ARIA_PREFIXES) — use it for subprocesses that run
    agent-chosen code.
    Names listed in ARIA_SHELL_ENV_ALLOW (comma-separated) are kept anyway.
    """
    home = str(Path.home())

    # ── Base: constructed defaults ────────────────────────────────────────
    extra_paths = [
        f"{home}/.local/bin",
        f"{home}/bin",
        f"{home}/go/bin",        # Go tools (gog typically installs here)
        f"{home}/.cargo/bin",    # Rust tools
        "/usr/local/bin",
        "/usr/local/sbin",
        "/usr/bin",
        "/usr/sbin",
        "/bin",
        "/sbin",
        "/snap/bin",
    ]

    current_path = os.environ.get("PATH", "")
    current_parts = current_path.split(":") if current_path else []
    seen = set(current_parts)
    merged = [p for p in extra_paths if p not in seen] + current_parts

    env = os.environ.copy()
    env["PATH"] = ":".join(merged)
    env.setdefault("HOME", home)
    env.setdefault("USER", os.environ.get("USER", Path.home().name))
    env.setdefault("XDG_CONFIG_HOME", f"{home}/.config")
    env.setdefault("XDG_DATA_HOME",   f"{home}/.local/share")
    env.setdefault("XDG_CACHE_HOME",  f"{home}/.cache")

    # ── Highest priority: vars from ~/.aria/.env ──────────────────────────
    # We parse it manually (no dotenv dep here) so we don't re-trigger
    # config.load() and cause circular imports.
    aria_env = Path(home) / ".aria" / ".env"
    aria_keys: set[str] = set()
    if aria_env.exists():
        for line in aria_env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                env[key] = value   # .env always wins for subprocess env
                aria_keys.add(key)

    if not include_secrets:
        keep = {k.strip() for k in env.get("ARIA_SHELL_ENV_ALLOW", "").split(",")
                if k.strip()}
        env = {k: v for k, v in env.items()
               if k in keep or not _is_secret_name(k)
               or not (k in aria_keys or k.startswith(_ARIA_PREFIXES))}

    return env
