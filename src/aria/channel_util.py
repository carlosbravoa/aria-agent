"""
aria/channel_util.py — small helpers shared by the Telegram/WhatsApp modules.

Stdlib-only at import time so push-only senders (telegram_notify,
whatsapp_notify) stay cheap to import.
"""

from __future__ import annotations

import os


def parse_allowed(var: str) -> list[str]:
    """Entries of the comma-separated env allow-list `var`.

    Each entry is whitespace-stripped; empty entries are dropped. Order and
    duplicates are preserved — callers convert to a set / filter as needed.
    """
    raw = os.environ.get(var, "")
    return [x.strip() for x in raw.split(",") if x.strip()]


def record_feed(text: str) -> None:
    """Record an outbound push so the agent has context when the user replies."""
    try:
        from aria import config
        from aria.workspace import Workspace
        ws = Workspace(config.workspace_dir())
        ws.append_notify_feed(text)
    except Exception:
        pass  # best-effort — never block on feed write
