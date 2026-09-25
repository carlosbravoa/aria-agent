"""
aria/channels/control.py — Remote control of the terminal session (mode B).

Normally every channel conversation gets its own agent session. When an
attached channel CONTROLS the REPL (`/remote control`, or
ARIA_CHANNEL_MODE_<NAME>=control), its messages run in the terminal's own
session instead — same history, plan, working directory and project context —
so you can pick up from your phone exactly where the terminal left off.

Flow: the channel's thread calls host.handle_message() as usual. For a
controlled channel the host submits the message here and BLOCKS until the REPL
has run it; the REPL wakes its prompt (keeping whatever was typed), renders the
turn in the terminal, streams replies back through the channel's callbacks,
and returns the replies. Plugins need no changes.

Turns from the phone run with the channel's delivery context set, so the
agent's notify/send_file tools reply on the phone and shell_run takes the
unattended policy — nothing ever waits on a terminal prompt nobody is at.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

_CLOSED_MSG = "Aria was closed on the terminal — this message wasn't run."


@dataclass
class RemoteTurn:
    channel: str
    user_id: str
    text: str
    response_cb: Callable[[str], None] | None = None
    activity_cb: Callable[[str], None] | None = None
    replies: list[str] = field(default_factory=list)
    done: threading.Event = field(default_factory=threading.Event)

    def finish(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.done.set()


_lock = threading.Lock()
_queue: queue.Queue[RemoteTurn] = queue.Queue()
_controlled: dict[str, str | None] = {}      # channel → last user who wrote from it
_agent: Any = None                            # the REPL's Agent while it listens
_wake: Callable[[], None] = lambda: None


def attach_repl(agent: Any, wake: Callable[[], None] | None) -> None:
    """The REPL starts listening: `wake()` interrupts its prompt (any thread)."""
    global _agent, _wake
    with _lock:
        _agent = agent
        _wake = wake or (lambda: None)


def detach_repl(message: str = _CLOSED_MSG) -> None:
    """The REPL is going away: release control and answer anything pending."""
    global _agent, _wake
    with _lock:
        _agent = None
        _wake = lambda: None
        _controlled.clear()
    while True:
        try:
            _queue.get_nowait().finish([message])
        except queue.Empty:
            return


def enable(channel: str) -> None:
    with _lock:
        if _agent is None:
            raise RuntimeError("no terminal session is listening")
        _controlled.setdefault(channel, None)


def disable(channel: str) -> None:
    with _lock:
        _controlled.pop(channel, None)


def is_controlled(channel: str) -> bool:
    with _lock:
        return channel in _controlled and _agent is not None


def controlled() -> dict[str, str | None]:
    """{channel: last user id (None until someone wrote)} under control."""
    with _lock:
        return dict(_controlled)


def agent() -> Any:
    return _agent


def submit(channel: str, user_id: str, text: str,
           response_cb: Callable[[str], None] | None = None,
           activity_cb: Callable[[str], None] | None = None) -> list[str]:
    """Run `text` in the terminal session; blocks until done. Called on the
    channel's thread by host.handle_message()."""
    turn = RemoteTurn(channel, str(user_id), text, response_cb, activity_cb)
    with _lock:
        if _agent is None or channel not in _controlled:
            return [_CLOSED_MSG]
        _controlled[channel] = str(user_id)
        _queue.put(turn)
        wake = _wake
    wake()
    turn.done.wait()
    return turn.replies


def take() -> RemoteTurn | None:
    """The next queued remote turn, if any (REPL thread, non-blocking)."""
    try:
        return _queue.get_nowait()
    except queue.Empty:
        return None


def pending() -> bool:
    return not _queue.empty()
