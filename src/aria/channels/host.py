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

__all__ = ["handle_message", "run_command", "answer_approval", "get_agent", "shutdown",
           "parse_allowed", "record_feed"]


def handle_message(channel: str, user_id: str, text: str,
                   response_cb: Callable[[str], None] | None = None,
                   activity_cb: Callable[[str], None] | None = None) -> list[str]:
    """Run one agent turn for `user_id` on `channel`; return the replies (send
    each as a separate message). `response_cb` receives each reply as soon as
    it is produced and `activity_cb` per-tool progress lines, for channels that
    stream. Blocking — call it from a worker thread in async transports.

    When this channel controls the terminal session (`/remote control`), the
    message runs there instead of in the channel's own session.

    Approval answers ("yes 1234") and shared slash commands (/stop, /clear,
    /model …) are handled here and never start a turn."""
    reply = answer_approval(channel, user_id, text)
    if reply is not None:
        return [reply]
    reply = run_command(channel, user_id, text)
    if reply is not None:
        return [reply]
    from aria.channels import control
    if control.is_controlled(channel):
        return control.submit(channel, str(user_id), text,
                              response_cb=response_cb, activity_cb=activity_cb)
    from aria import channel as _sessions
    return _sessions.handle(channel, str(user_id), text,
                            response_cb=response_cb, activity_cb=activity_cb)


def answer_approval(channel: str, user_id: str, text: str) -> str | None:
    """Handle a "yes 1234" / "no 1234" reply to a pending approval; None if the
    text isn't one. Must be checked BEFORE any per-chat lock: the turn waiting
    for the answer holds it."""
    from aria import approval
    return approval.try_answer_text(channel, str(user_id), text)


def run_command(channel: str, user_id: str, text: str) -> str | None:
    """Run a shared slash command (/stop /clear /memory /tools /model /models
    /save /version /help) for this conversation; None if `text` isn't one.
    /stop never waits: it only flags the running turn."""
    from aria.channels import commands
    if commands.parse(text) is None:
        return None
    return commands.run(get_agent(channel, user_id), text)


def get_agent(channel: str, user_id: str):
    """The live Agent for this conversation (for channel-specific commands
    such as /clear or /model) — the terminal's own Agent while the channel
    controls it."""
    from aria.channels import control
    if control.is_controlled(channel) and control.agent() is not None:
        return control.agent()
    from aria import channel as _sessions
    return _sessions.get_session(channel, str(user_id))


def shutdown(channel: str | None = None) -> None:
    """Close open sessions cleanly (call when run() exits). With `channel`,
    only that channel's sessions — other channels in the same process
    (attached mode) keep theirs."""
    from aria import channel as _sessions
    _sessions.shutdown(channel)
