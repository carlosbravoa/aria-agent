"""
aria/channels/base.py — The channel plugin contract.

A channel connects Aria to a messaging surface (Telegram, WhatsApp, a webhook,
Matrix, …). Each one is a `ChannelPlugin` subclass exposed by a module as
`PLUGIN` (an instance) or `Plugin` (the class). Built-ins live in
`aria/channels/<name>/`; user plugins are single files dropped into
`~/.aria/channels/` (see docs/channel-plugins.md).

The plugin owns the transport — receiving messages and sending text — and
nothing else. Sessions, per-conversation history, memory, tools, delivery
context and idle timeouts are provided by the host (`aria.channels.host`).

Import rule: a plugin module is imported whenever the registry is consulted
(the notify tool, the supervisor, the installer). Keep its top level cheap and
dependency-free; import SDKs (python-telegram-bot, httpx, …) inside `run()` /
`send()`.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


# install() notes: plain text (shown as a hint) or (level, text) with level
# "ok" | "warn" | "info". The updater shows only ok/warn notes (real changes).
Note = str | tuple[str, str]


@dataclass(frozen=True)
class ConfigField:
    """One .env setting the installer asks for when the channel is enabled.
    `help` is shown as the prompt's hint."""
    key: str
    prompt: str = ""
    secret: bool = False
    required: bool = False
    default: str = ""
    help: str = ""


@dataclass(frozen=True)
class ServiceSpec:
    """A systemd --user unit the installer writes for this channel.

    `exec_start` is an argv list; a bare "aria-…" first element is resolved
    to the installed console script. `requires` names another unit this one
    depends on (e.g. WhatsApp's Node process requires the Python bridge)."""
    unit: str
    description: str
    exec_start: tuple[str, ...]
    requires: str = ""
    optional: bool = False       # skipped (with a warning) if its binary is missing


class ChannelPlugin:
    """Base class for channel plugins. Override what your channel supports.

    Required:
      name          unique id ([a-z][a-z0-9_-]*). Also the channel name the
                    agent sees in its delivery context and the prefix of each
                    conversation's window key ("<name>:<user_id>").
      run()         the long-running receive loop (blocks). Call
                    host.handle_message() for every inbound message.
      send()        push text to a user (or broadcast when `to` is None) — used
                    by the notify tool, supervisor results and `aria --notify`.

    Optional:
      send_file()   set supports_files = True and implement it.
      title         section/menu name (default: name capitalised).
      setup_help    intro line(s) shown before the channel's settings.
      config_fields settings the installer prompts for.
      legacy_keys   env keys that auto-enable the channel when ARIA_CHANNELS is
                    unset (backwards compatibility for pre-plugin installs).
      services()    systemd units; default: one `aria-channel <name>` unit.
      install()     pre-install hook (deploy helper files, check binaries).
    """

    # Plain class attributes (not a dataclass: a generated __init__ would
    # overwrite a subclass's `name = "…"` with the base default).
    name: str = ""
    title: str = ""
    description: str = ""
    setup_help: str = ""
    config_fields: tuple[ConfigField, ...] = ()
    legacy_keys: tuple[str, ...] = ()
    supports_files: bool = False
    override: bool = False       # a user plugin must set this to replace a built-in
    overrides: ChannelPlugin | None = None   # the built-in it replaced (set by the registry)
    builtin: bool = False        # set by the registry
    source: str = ""             # module/file it was loaded from (set by the registry)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name!r}>"

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def run(self) -> None:
        raise NotImplementedError(f"channel '{self.name}' has no run()")

    def is_configured(self) -> bool:
        """True when the settings this channel needs are present. Used for the
        legacy auto-enable path and by the installer to preselect it."""
        keys = self.legacy_keys or tuple(f.key for f in self.config_fields if f.required)
        return bool(keys) and all(os.environ.get(k, "").strip() for k in keys)

    # ── Outbound ──────────────────────────────────────────────────────────────

    def send(self, text: str, to: str | None = None) -> None:
        """Deliver `text` to user `to`, or to every allowed recipient when
        `to` is None. Raise RuntimeError with a human-readable reason on
        failure (it is shown to the model/user)."""
        raise NotImplementedError(f"channel '{self.name}' cannot push messages")

    def send_file(self, path: Path, caption: str = "", to: str | None = None) -> str:
        """Deliver a file; return the name it was sent as."""
        raise NotImplementedError(f"channel '{self.name}' cannot send files")

    @property
    def display_name(self) -> str:
        return self.title or self.name.capitalize()

    @property
    def supports_push(self) -> bool:
        return type(self).send is not ChannelPlugin.send

    # ── Installation ──────────────────────────────────────────────────────────

    def services(self) -> list[ServiceSpec]:
        if self.overrides is not None:
            # Replacing a built-in: reuse its unit names so the installer
            # rewrites the existing unit instead of adding a second one (two
            # pollers on one Telegram token conflict), running this plugin.
            specs = self.overrides.services()
            first = specs[0]
            return [ServiceSpec(first.unit, first.description,
                                ("aria-channel", self.name), first.requires,
                                first.optional)] + specs[1:]
        return [ServiceSpec(unit=f"aria-channel-{self.name}",
                            description=f"Aria {self.name} channel",
                            exec_start=("aria-channel", self.name))]

    def install(self, dry_run: bool = False) -> list[Note]:
        """Pre-install hook (also run after a self-update). Return notes for
        the installer — see `Note`."""
        return []


def validate_name(name: str) -> bool:
    return bool(_NAME_RE.match(name or ""))
