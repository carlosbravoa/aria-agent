"""
aria/channels — Channel plugin registry.

Discovery (later sources override earlier ones by name, with a warning):
  1. built-ins: subpackages/modules of this package (telegram, whatsapp)
  2. user plugins: *.py files in ~/.aria/channels/ ($ARIA_CHANNELS_DIR);
     files starting with "_" are skipped. A broken file is skipped with a
     warning — it never takes the other channels down.

Enablement:
  ARIA_CHANNELS=telegram,whatsapp,mine   explicit list (written by aria-install)
  unset → legacy fallback: every channel whose settings are present is enabled
          (TELEGRAM_TOKEN → telegram, WHATSAPP_ALLOWED → whatsapp), so
          deployments from before plugins keep working with no change.

Run mode (per channel, ARIA_CHANNEL_MODE_<NAME>):
  service   (default) a background systemd unit, always online
  attached  runs inside the `aria` CLI only while it's open — nothing in the
            background (plugin must set supports_attached). A per-channel run
            lock (runlock.py) guarantees the CLI and a service never both poll.

Push target outside a conversation (supervisor results, reflection notices,
`aria --notify`, the notify tool from the REPL):
  ARIA_NOTIFY_CHANNEL=<name>   explicit
  unset → "telegram" when enabled (the historical behaviour), else the first
          enabled channel that can push.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import pkgutil
import sys
import threading
from pathlib import Path

from aria.channels.base import ChannelPlugin, ConfigField, Note, ServiceSpec, validate_name

__all__ = [
    "ChannelPlugin", "ConfigField", "Note", "ServiceSpec",
    "discover", "enabled", "get", "push_channel", "push", "channels_dir", "reset_cache",
    "attached_channels", "service_channels",
]

log = logging.getLogger(__name__)

_NOT_PLUGINS = {"base", "host", "cli"}
_cache: dict[str, ChannelPlugin] | None = None
_cache_lock = threading.Lock()


def channels_dir() -> Path:
    raw = os.environ.get("ARIA_CHANNELS_DIR")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".aria" / "channels"


def _plugin_from_module(mod, source: str, builtin: bool) -> ChannelPlugin | None:
    obj = getattr(mod, "PLUGIN", None)
    if obj is None:
        cls = getattr(mod, "Plugin", None)
        if isinstance(cls, type) and issubclass(cls, ChannelPlugin):
            obj = cls()
    if not isinstance(obj, ChannelPlugin):
        return None
    if not validate_name(obj.name):
        log.warning("Channel plugin %s has an invalid name %r — skipped", source, obj.name)
        return None
    obj.builtin = builtin
    obj.source = source
    return obj


def _discover() -> dict[str, ChannelPlugin]:
    found: dict[str, ChannelPlugin] = {}
    pkg_dir = Path(__file__).parent
    for _, name, _ in pkgutil.iter_modules([str(pkg_dir)]):
        if name.startswith("_") or name in _NOT_PLUGINS:
            continue
        try:
            mod = importlib.import_module(f"aria.channels.{name}")
        except Exception as exc:              # a broken built-in must not kill the rest
            log.warning("Built-in channel %s failed to load: %s", name, exc)
            continue
        plugin = _plugin_from_module(mod, f"aria.channels.{name}", builtin=True)
        if plugin:
            found[plugin.name] = plugin

    user_dir = channels_dir()
    if user_dir.is_dir():
        for path in sorted(user_dir.glob("*.py")):
            if path.stem.startswith("_"):
                continue
            try:
                spec = importlib.util.spec_from_file_location(
                    f"_aria_user_channel_{path.stem}", path)
                if not (spec and spec.loader):
                    continue
                mod = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = mod
                spec.loader.exec_module(mod)
            except Exception as exc:
                sys.modules.pop(f"_aria_user_channel_{path.stem}", None)
                log.warning("Channel plugin %s failed to load: %s", path, exc)
                continue
            plugin = _plugin_from_module(mod, str(path), builtin=False)
            if plugin is None:
                log.warning("%s defines no ChannelPlugin (PLUGIN or Plugin) — skipped", path)
                continue
            prior = found.get(plugin.name)
            if prior is not None:
                if not (prior.builtin and plugin.override):
                    log.warning("Channel plugin %s: name '%s' is already taken%s — skipped",
                                path, plugin.name,
                                " (set override = True to replace the built-in)"
                                if prior.builtin else "")
                    continue
                plugin.overrides = prior
                log.warning("Channel plugin %s replaces the built-in '%s'", path, plugin.name)
            found[plugin.name] = plugin
    return found


def discover(refresh: bool = False) -> dict[str, ChannelPlugin]:
    """All available channel plugins by name (cached per process)."""
    global _cache
    with _cache_lock:
        if _cache is None or refresh:
            _cache = _discover()
        return dict(_cache)


def reset_cache() -> None:
    global _cache
    with _cache_lock:
        _cache = None


def get(name: str) -> ChannelPlugin | None:
    return discover().get((name or "").strip().lower())


_warned: set[str] = set()


def _explicit() -> list[str] | None:
    """ARIA_CHANNELS as a list; None when unset (legacy mode). "none" (what the
    installer writes when every channel is deselected) means no channels —
    an empty value would be dropped from .env and fall back to legacy mode."""
    raw = os.environ.get("ARIA_CHANNELS")
    if raw is None:
        return None
    names = [n.strip().lower() for n in raw.split(",") if n.strip()]
    return [] if names == ["none"] else names


def enabled() -> list[ChannelPlugin]:
    """Enabled channels, in configured order (see module docstring)."""
    plugins = discover()
    names = _explicit()
    if names is None:
        return [p for p in plugins.values() if p.is_configured()]
    out = []
    for n in names:
        if n in plugins:
            out.append(plugins[n])
        elif n not in _warned:
            _warned.add(n)
            log.warning("ARIA_CHANNELS lists unknown channel %r — ignored", n)
    return out


def attached_channels() -> list[ChannelPlugin]:
    """Enabled channels configured to run inside the `aria` CLI."""
    out = []
    for p in enabled():
        if p.mode != "attached":
            continue
        if not p.supports_attached:
            if p.name not in _warned:
                _warned.add(p.name)
                log.warning("Channel %r can't run attached (ARIA_CHANNEL_MODE) — "
                            "run it as a service instead", p.name)
            continue
        out.append(p)
    return out


def service_channels() -> list[ChannelPlugin]:
    """Enabled channels that run as background services (get systemd units)."""
    return [p for p in enabled()
            if not (p.mode == "attached" and p.supports_attached)]


def push_channel() -> ChannelPlugin | None:
    """Where pushes go when no conversation is active (see module docstring)."""
    name = os.environ.get("ARIA_NOTIFY_CHANNEL", "").strip().lower()
    if name:
        return get(name)
    on = enabled()
    for p in on:
        if p.name == "telegram":
            return p
    for p in on:
        if p.supports_push:
            return p
    # Historical behaviour: outside a channel, pushes went to Telegram even
    # when nothing else was set up — keep that so the error message (e.g.
    # "TELEGRAM_TOKEN not set") stays the actionable one.
    return get("telegram")


def push(text: str, to: str | None = None, channel: str | None = None) -> str:
    """Push `text` via `channel` (default: push_channel()). Returns the channel
    name used. Raises RuntimeError when no channel can deliver."""
    plugin = get(channel) if channel else push_channel()
    if plugin is None:
        raise RuntimeError(
            f"no channel '{channel}'" if channel else
            "no channel is enabled for notifications — set ARIA_NOTIFY_CHANNEL "
            "or enable a channel (ARIA_CHANNELS)")
    if not plugin.supports_push:
        raise RuntimeError(f"channel '{plugin.name}' cannot push messages")
    plugin.send(text, to=to)
    return plugin.name
