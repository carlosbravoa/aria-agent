"""
aria/channels/host.py — What Aria provides to channel plugins.

Everything a plugin needs to turn "a message arrived" into "here are the
replies" without knowing about agents, sessions or memory:

    from aria.channels import host

    replies = host.handle_message("mychannel", user_id, text)
    for r in replies:
        my_transport.send(user_id, r)

Sessions are keyed by (channel, user_id); each has its own conversation window
and plan, shares long-term memory with every other channel, and is closed after
ARIA_CHANNEL_IDLE_MINUTES of inactivity. During a turn the delivery context is
set, so the agent's notify/send_file tools reply on this channel.
"""

from __future__ import annotations

from collections.abc import Callable

from aria.channel_util import parse_allowed, record_feed

__all__ = ["handle_message", "get_agent", "shutdown", "parse_allowed", "record_feed"]


def handle_message(channel: str, user_id: str, text: str,
                   response_cb: Callable[[str], None] | None = None,
                   activity_cb: Callable[[str], None] | None = None) -> list[str]:
    """Run one agent turn for `user_id` on `channel`; return the replies (send
    each as a separate message). `response_cb` receives each reply as soon as
    it is produced and `activity_cb` per-tool progress lines, for channels that
    stream. Blocking — call it from a worker thread in async transports."""
    from aria import channel as _sessions
    return _sessions.handle(channel, str(user_id), text,
                            response_cb=response_cb, activity_cb=activity_cb)


def get_agent(channel: str, user_id: str):
    """The live Agent for this conversation (for channel-specific commands
    such as /clear or /model)."""
    from aria import channel as _sessions
    return _sessions.get_session(channel, str(user_id))


def shutdown(channel: str | None = None) -> None:
    """Close open sessions cleanly (call when run() exits). With `channel`,
    only that channel's sessions — other channels in the same process
    (attached mode) keep theirs."""
    from aria import channel as _sessions
    _sessions.shutdown(channel)
