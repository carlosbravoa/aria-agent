"""
aria/context.py — Which channel and user the current turn belongs to.

Tools are plain module-level `execute(args)` functions with no reference to the
Agent that called them, so a tool that wants to reply on the channel the user
is actually talking on has nowhere to look. This module holds that for the
duration of one turn: a channel sets it before handing the message to the
agent and clears it afterwards, and delivery tools (`notify`, `send_file`) read
it to target the right chat instead of broadcasting to every allowed one.

Unset means "no active channel" — the REPL, single-shot runs, supervisor tasks
and cron. Those fall back to their configured broadcast target, which is the
correct behaviour there.

Implementation note: a ContextVar is per-thread, and the agent loop runs in a
pooled worker thread, so the value must be reset when the turn ends or a later
turn reusing that thread could observe a stale channel. `set_active` returns a
token for exactly that; always reset it in a `finally`. Tools marked
PARALLEL_SAFE execute in a separate pool that does not inherit this context —
they are read-only remote tools that never deliver, so it does not apply.
"""

from __future__ import annotations

import contextvars
from typing import NamedTuple


class ChannelContext(NamedTuple):
    channel: str
    user_id: str


_active: contextvars.ContextVar["ChannelContext | None"] = contextvars.ContextVar(
    "aria_active_channel", default=None
)


def set_active(channel: str, user_id: str):
    """Mark the channel/user this turn belongs to. Returns a reset token."""
    return _active.set(ChannelContext(str(channel), str(user_id)))


def reset(token) -> None:
    """Restore the previous value. Safe to call with a token from another
    context — falls back to clearing rather than raising."""
    try:
        _active.reset(token)
    except (ValueError, LookupError):
        _active.set(None)


def current() -> "ChannelContext | None":
    """The active (channel, user_id), or None outside a channel turn."""
    return _active.get()


def clear() -> None:
    _active.set(None)
