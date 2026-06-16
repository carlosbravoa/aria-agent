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
from pathlib import Path


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


def build_env() -> dict[str, str]:
    """
    Return an environment dict suitable for subprocess calls from a
    background process.

    Priority (highest to lowest):
      1. Variables in ~/.aria/.env  ← put GMAIL_ACCOUNT etc. here
      2. Current process environment
      3. Constructed PATH and XDG defaults
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

    return env
