"""
aria/channel.py — Channel abstraction for multi-platform messaging.

Session key: (channel_name, user_id)
  - Separate history per channel per user
  - Memory (workspace) is shared across all channels

Session continuity for long-lived channel processes (Telegram, WhatsApp):
  After ARIA_CHANNEL_IDLE_MINUTES of inactivity, the session is summarised
  and closed so the agent has continuity if the process restarts or the
  user returns later. Default: 60 minutes.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Protocol, runtime_checkable

from aria.agent import Agent

log = logging.getLogger(__name__)

_IDLE_SECONDS = int(os.environ.get("ARIA_CHANNEL_IDLE_MINUTES", "60")) * 60


class _Session:
    """Wraps an Agent with an inactivity timer."""

    def __init__(self, channel: str, user_id: str) -> None:
        self.agent     = Agent()
        self.channel   = channel
        self.user_id   = user_id
        self._timer: threading.Timer | None = None
        self._lock     = threading.Lock()
        self._reset_timer()

    def handle(self, text: str) -> str:
        with self._lock:
            self._reset_timer()
            return self.agent.chat_collect(text)

    def _reset_timer(self) -> None:
        """Restart the inactivity countdown."""
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(_IDLE_SECONDS, self._on_idle)
        self._timer.daemon = True
        self._timer.start()

    def _on_idle(self) -> None:
        """Called after inactivity period — summarise and clean up."""
        log.info(
            "Session idle for %d min — saving summary (%s/%s)",
            _IDLE_SECONDS // 60, self.channel, self.user_id,
        )
        with self._lock:
            self.agent.close()
        # Remove from registry so a fresh session is created next time
        _sessions.pop((self.channel, self.user_id), None)

    def cancel(self) -> None:
        """Cancel the timer (e.g. on clean shutdown)."""
        if self._timer is not None:
            self._timer.cancel()


# Registry: (channel_name, user_id) → _Session
_sessions: dict[tuple[str, str], _Session] = {}


def get_session(channel: str, user_id: str) -> Agent:
    """Return the Agent for this (channel, user_id) pair, creating one if needed."""
    key = (channel, user_id)
    if key not in _sessions:
        log.info("New session: channel=%s user=%s", channel, user_id)
        _sessions[key] = _Session(channel, user_id)
    return _sessions[key].agent


def handle(channel: str, user_id: str, text: str) -> str:
    """
    Process one user message and return the agent's response.
    Resets the inactivity timer on every message.
    """
    key = (channel, user_id)
    if key not in _sessions:
        log.info("New session: channel=%s user=%s", channel, user_id)
        _sessions[key] = _Session(channel, user_id)
    return _sessions[key].handle(text)


def shutdown() -> None:
    """Summarise all active sessions on clean process exit."""
    for session in list(_sessions.values()):
        session.cancel()
        session.agent.close()
    _sessions.clear()


@runtime_checkable
class Channel(Protocol):
    """Protocol every channel adapter must satisfy."""
    channel_name: str

    def send(self, user_id: str, text: str) -> None:
        """Send text back to user_id on this channel."""
        ...
