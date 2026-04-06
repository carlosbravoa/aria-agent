"""
aria/channel.py — Channel abstraction for multi-platform messaging.

Every transport (Telegram, WhatsApp, …) implements the Channel protocol
and registers incoming messages through the session registry. The agent
always replies back through the same channel that delivered the request.

Session key: (channel_name, user_id)
  - Separate history per channel per user by default
  - Memory (workspace) is shared across all channels for the same installation
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from aria.agent import Agent

log = logging.getLogger(__name__)

# Registry: (channel_name, user_id) → Agent
_sessions: dict[tuple[str, str], Agent] = {}


def get_session(channel: str, user_id: str) -> Agent:
    """
    Return the Agent for this (channel, user_id) pair.
    Creates a new one on first contact.
    """
    key = (channel, user_id)
    if key not in _sessions:
        log.info("New session: channel=%s user=%s", channel, user_id)
        _sessions[key] = Agent()
    return _sessions[key]


def handle(channel: str, user_id: str, text: str) -> str:
    """
    Process one user message and return the agent's response as a string.
    The caller is responsible for routing the response back to the right channel.
    """
    agent = get_session(channel, user_id)
    return agent.chat_collect(text)


@runtime_checkable
class Channel(Protocol):
    """
    Protocol every channel adapter must satisfy.
    Not strictly required for operation but useful for type checking.
    """
    channel_name: str

    def send(self, user_id: str, text: str) -> None:
        """Send text back to user_id on this channel."""
        ...
