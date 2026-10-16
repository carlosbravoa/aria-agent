"""
aria/channels/autostart.py — Bring a channel online when a push finds it down.

"Message me on Vicus" from the terminal shouldn't fail just because the
channel isn't running. Before a push, a plugin whose delivery needs a live
process (Vicus: the device behind its socket) calls ensure_online(); when the
channel is down it is started the way the user would start it:

  - its background service, when a systemd unit is installed — started, not
    enabled, so the user's on/off-at-login choice is left alone;
  - else, inside the open `aria` window (attached, exactly like `/channel on`;
    it goes offline when the window closes — `/channel off` stops it sooner);
  - else (a scheduled task, `aria --notify` with nothing to host it) it
    explains how to start it.

ARIA_CHANNEL_AUTOSTART=off disables this. What was done is kept per thread
(take_note) so the notify/send_file tools can tell the user.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable

from aria.channels.base import ChannelPlugin

_lock = threading.Lock()
_notes = threading.local()


def enabled() -> bool:
    return os.environ.get("ARIA_CHANNEL_AUTOSTART", "on").strip().lower() not in (
        "off", "0", "no", "false")


def take_note() -> str:
    """What the last ensure_online() on this thread did ("" if nothing)."""
    note = getattr(_notes, "text", "")
    _notes.text = ""
    return note


def ensure_online(plugin: ChannelPlugin, online: Callable[[], bool],
                  timeout: float = 45.0) -> None:
    """Return once `online()` holds, starting the channel if needed. Raises
    RuntimeError (shown to the model/user) when it can't be brought up."""
    if online():
        return
    name = plugin.name
    title = getattr(plugin, "title", "") or name
    how_to = f"start it with `/channel on {name}` in aria, or `aria-channel {name}`"
    if not enabled():
        raise RuntimeError(f"the {title} channel isn't running — {how_to}")
    from aria.channels import attached, services
    missing = services.missing_settings(name)
    if missing:
        raise RuntimeError(f"the {title} channel isn't set up (missing "
                           f"{', '.join(missing)}) — run /channel setup {name}")
    with _lock:                       # two pushes at once start it once
        if online():
            return
        here = False
        if services.installed(name):
            services.resume(name)
            done = "started its background service"
        elif attached.hosting() and plugin.supports_attached:
            ok, msg = attached.start(plugin)
            if not ok:
                raise RuntimeError(msg)
            here = True
            done = "brought it online in this aria window (/channel off to stop it)"
        else:
            raise RuntimeError(f"the {title} channel isn't running — {how_to}")
        deadline = time.monotonic() + timeout
        while not online():
            state = dict(attached.status()).get(name, "") if here else ""
            if state.startswith("stopped"):          # e.g. bad credentials
                raise RuntimeError(f"{title} couldn't start ({state}) — see "
                                   f"/channel logs {name}")
            if time.monotonic() > deadline:
                raise RuntimeError(f"{title} was offline and didn't come up within "
                                   f"{timeout:.0f} s — see /channel logs {name}")
            time.sleep(0.2)
    _notes.text = f"{title} was offline — {done}."
