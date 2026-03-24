"""
aria/tools/_env.py — Shared subprocess environment helper.

When Aria runs as a background service (systemd, Telegram bot, etc.) it may
not inherit the user's full shell environment. This module builds an env dict
that includes the user's PATH, HOME, config dirs, and any extra vars needed
for CLI tools like gog to find their auth tokens.
"""

from __future__ import annotations

import os
from pathlib import Path


def build_env() -> dict[str, str]:
    """
    Return an environment dict suitable for subprocess calls.

    Merges (in priority order):
      1. Current process environment  (already-set vars win)
      2. Sensible PATH that includes common user binary locations
      3. HOME, XDG dirs, DBUS_SESSION_BUS_ADDRESS (needed by some CLIs)
    """
    home = str(Path.home())

    # Extend PATH with locations that are often missing in service environments
    extra_paths = [
        f"{home}/.local/bin",
        f"{home}/bin",
        f"{home}/.cargo/bin",   # Rust tools
        f"{home}/go/bin",       # Go tools (gog lives here)
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

    # Prepend extra paths that aren't already present
    merged_parts = []
    seen = set(current_parts)
    for p in extra_paths:
        if p not in seen:
            merged_parts.append(p)
            seen.add(p)
    merged_parts.extend(current_parts)

    env = os.environ.copy()
    env["PATH"] = ":".join(merged_parts)
    env.setdefault("HOME", home)
    env.setdefault("USER", os.environ.get("USER", Path.home().name))

    # XDG dirs — where many CLIs store config/tokens
    env.setdefault("XDG_CONFIG_HOME", f"{home}/.config")
    env.setdefault("XDG_DATA_HOME", f"{home}/.local/share")
    env.setdefault("XDG_CACHE_HOME", f"{home}/.cache")

    return env
