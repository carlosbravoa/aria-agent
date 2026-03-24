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
TTY_COMMANDS = frozenset({
    "top", "htop", "btop", "vim", "vi", "nano", "emacs", "less", "more",
    "man", "ssh", "telnet", "ftp", "sftp", "mysql", "psql", "sqlite3",
    "python", "python3", "ipython", "irb", "node", "bash", "sh", "zsh",
    "fish", "screen", "tmux", "watch",
})


def is_tty_command(command: str) -> bool:
    """Return True if the command is likely to require an interactive TTY."""
    first_word = command.strip().split()[0] if command.strip() else ""
    # Strip path prefix (e.g. /usr/bin/vim → vim)
    binary = Path(first_word).name
    return binary in TTY_COMMANDS


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
