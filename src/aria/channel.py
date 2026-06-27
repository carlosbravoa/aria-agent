"""
aria/channel.py — Channel abstraction for multi-platform messaging.

Session key: (channel_name, user_id)
  - Separate history per channel per user
  - Memory (workspace) is shared across all channels

Session continuity for long-lived channel processes (Telegram, WhatsApp):
  After ARIA_CHANNEL_IDLE_MINUTES of inactivity, the session's conversation
  window is trimmed (agent.close(), no LLM summary) and the session is dropped
  so it resumes cleanly when the user returns. Default: 60 minutes.
  Registry access is guarded by a lock — channels handle messages on multiple
  threads.
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
        self.key       = (channel, user_id)
        # terminal=False: channels render nothing and have no meaningful cwd, so
        # the cwd/project-context system-prompt blocks must not be injected.
        self.agent     = Agent(window_key=f"{channel}:{user_id}", terminal=False)
        self.channel   = channel
        self.user_id   = user_id
        self._timer: threading.Timer | None = None
        self._gen      = 0          # bumped on every activity; stale timers bail
        self._lock     = threading.Lock()
        self._reset_timer()

    def handle(self, text: str, response_cb=None, activity_cb=None) -> list[str]:
        with self._lock:
            self._reset_timer()
            return self.agent.chat_yield(text, response_cb=response_cb,
                                         activity_cb=activity_cb)

    def _reset_timer(self) -> None:
        """Restart the inactivity countdown. Called under self._lock."""
        if self._timer is not None:
            self._timer.cancel()
        self._gen += 1
        gen = self._gen
        self._timer = threading.Timer(_IDLE_SECONDS, self._on_idle, args=(gen,))
        self._timer.daemon = True
        self._timer.start()

    def _on_idle(self, gen: int) -> None:
        """Called after inactivity — trim the window and drop from the registry.
        Bails if newer activity reset the timer (gen mismatch), and only evicts
        itself if the registry still maps the key to this exact session (so an
        orphaned duplicate can never evict a live one)."""
        with self._lock:
            if gen != self._gen:
                return                       # a newer message reset the timer
            log.info("Session idle for %d min (%s/%s)",
                     _IDLE_SECONDS // 60, self.channel, self.user_id)
            with _registry_lock:
                if _sessions.get(self.key) is self:
                    _sessions.pop(self.key, None)
            self.agent.close()

    def cancel(self) -> None:
        """Cancel the timer (e.g. on clean shutdown)."""
        if self._timer is not None:
            self._timer.cancel()


# Registry: (channel_name, user_id) → _Session. All access guarded by
# _registry_lock — message-handler threads (Telegram/WhatsApp can run
# concurrently) and idle-timer threads both mutate it.
_sessions: dict[tuple[str, str], _Session] = {}
_registry_lock = threading.Lock()


def _get_or_create(channel: str, user_id: str) -> _Session:
    """Atomically fetch or create the session for this (channel, user_id)."""
    key = (channel, user_id)
    with _registry_lock:
        sess = _sessions.get(key)
        if sess is None:
            log.info("New session: channel=%s user=%s", channel, user_id)
            sess = _Session(channel, user_id)
            _sessions[key] = sess
        return sess


def get_session(channel: str, user_id: str) -> Agent:
    """Return the Agent for this (channel, user_id) pair, creating one if needed."""
    return _get_or_create(channel, user_id).agent


def handle(channel: str, user_id: str, text: str,
           response_cb=None, activity_cb=None) -> list[str]:
    """
    Process one user message and return a list of response strings.
    Each string should be sent as a separate message for natural timing.
    The slow chat runs outside the registry lock — only get-or-create is guarded.

    Optional `response_cb`/`activity_cb` let a channel stream responses and tool
    progress mid-turn (used by Telegram); omitting them keeps the batched return
    (used by WhatsApp and the rest).
    """
    return _get_or_create(channel, user_id).handle(text, response_cb, activity_cb)


def shutdown() -> None:
    """Summarise all active sessions on clean process exit."""
    with _registry_lock:
        sessions = list(_sessions.values())
        _sessions.clear()
    for session in sessions:
        session.cancel()
        with session._lock:
            session.agent.close()


@runtime_checkable
class Channel(Protocol):
    """Protocol every channel adapter must satisfy."""
    channel_name: str

    def send(self, user_id: str, text: str) -> None:
        """Send text back to user_id on this channel."""
        ...
