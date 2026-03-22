"""
aria/config.py — Centralised configuration and path resolution.

Search order for .env:
  1. $ARIA_ENV  (explicit override)
  2. ~/.aria/.env
  3. ./.env     (cwd, for dev convenience)
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


def _find_env() -> Path | None:
    if explicit := os.environ.get("ARIA_ENV"):
        return Path(explicit)
    home_env = Path.home() / ".aria" / ".env"
    if home_env.exists():
        return home_env
    cwd_env = Path(".env")
    if cwd_env.exists():
        return cwd_env
    return None


def load() -> None:
    """Load environment variables. Safe to call multiple times."""
    env_file = _find_env()
    if env_file:
        load_dotenv(env_file, override=False)


# ── Resolved paths ───────────────────────────────────────────────────────────

def workspace_dir() -> Path:
    """
    Workspace root. Resolution order:
      1. $ARIA_WORKSPACE
      2. ~/.aria/workspace
    """
    raw = os.environ.get("ARIA_WORKSPACE")
    if raw:
        return Path(raw).expanduser().resolve()
    return Path.home() / ".aria" / "workspace"


def tools_dir() -> Path:
    """
    Extra tools directory. Resolution order:
      1. $ARIA_TOOLS_DIR
      2. ~/.aria/tools   (user-installed tools)
    Falls back to built-in package tools if not set.
    """
    raw = os.environ.get("ARIA_TOOLS_DIR")
    if raw:
        return Path(raw).expanduser().resolve()
    return Path.home() / ".aria" / "tools"
