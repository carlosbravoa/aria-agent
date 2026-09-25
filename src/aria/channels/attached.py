"""
aria/channels/attached.py — Run channels inside the `aria` CLI process.

Attached mode: a channel is online only while Aria is open — no background
service, nothing left running. Each attached channel runs its plugin's
`start(stop_event)` in a daemon thread; the per-channel run lock keeps it from
fighting a service that polls the same account.

Conversations arriving on an attached channel get their own sessions (same as
the service), share long-term memory with the REPL, and take the unattended
tool policy — shell_run never prompts the terminal for a remote user.

Logs from attached channels go to ~/.aria/logs/attached.log, never to the
terminal: a library warning printed from a background thread would garble the
REPL's prompt line.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path

from aria.channels.base import ChannelPlugin
from aria.channels.runlock import RunLock

log = logging.getLogger(__name__)

# Loggers the attached channels write through; redirected to a file while any
# channel is attached. httpx logs every request at INFO — keep only warnings.
_ROUTED_LOGGERS = {"aria.channels": logging.INFO, "aria.channel": logging.INFO,
                   "telegram": logging.WARNING, "httpx": logging.WARNING}


@dataclass
class _Running:
    plugin: ChannelPlugin
    lock: RunLock
    stop: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    error: str = ""


_running: dict[str, _Running] = {}
_state_lock = threading.Lock()
_log_handler: logging.Handler | None = None
_saved_logger_state: dict[str, tuple[int, bool]] = {}


class _RedactingFormatter(logging.Formatter):
    """Mask secret values (tokens, keys, passwords from the environment) in
    the final text — tracebacks included. Libraries embed them in messages,
    e.g. python-telegram-bot's InvalidToken quotes the token."""

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for value in _secret_values():
            text = text.replace(value, "***")
        return text


def _secret_values() -> list[str]:
    import os
    from aria.tools._env import _is_secret_name
    return sorted({v for k, v in os.environ.items()
                   if _is_secret_name(k) and len(v) >= 8}, key=len, reverse=True)


def log_path() -> Path:
    return Path.home() / ".aria" / "logs" / "attached.log"


def _route_logs() -> None:
    global _log_handler
    if _log_handler is not None:
        return
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handler = logging.FileHandler(path, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    handler.setFormatter(_RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    for name, level in _ROUTED_LOGGERS.items():
        lg = logging.getLogger(name)
        _saved_logger_state[name] = (lg.level, lg.propagate)
        lg.addHandler(handler)
        lg.setLevel(level)
        lg.propagate = False
    _log_handler = handler


def _unroute_logs() -> None:
    global _log_handler
    if _log_handler is None:
        return
    for name, (level, propagate) in _saved_logger_state.items():
        lg = logging.getLogger(name)
        lg.removeHandler(_log_handler)
        lg.setLevel(level)
        lg.propagate = propagate
    _saved_logger_state.clear()
    _log_handler.close()
    _log_handler = None


def _run(state: _Running) -> None:
    name = state.plugin.name
    try:
        state.plugin.start(state.stop)
    except Exception as exc:                      # never let a channel kill the REPL
        state.error = f"{type(exc).__name__}: {exc}"
        log.exception("Attached channel %s stopped with an error", name)
    finally:
        try:
            from aria.channels import host
            host.shutdown(name)                   # close only this channel's sessions
        except Exception:
            log.exception("Closing %s sessions failed", name)
        state.lock.release()


def start(plugin: ChannelPlugin) -> tuple[bool, str]:
    """Attach `plugin` to this process. Returns (ok, message for the user)."""
    name = plugin.name
    if not plugin.supports_attached:
        return False, f"{name} can't run attached — it only runs as a service"
    with _state_lock:
        cur = _running.get(name)
        if cur is not None and cur.thread is not None and cur.thread.is_alive():
            return True, f"{name} is already attached"
        lock = RunLock(name)
        if not lock.acquire(blocking=False):
            return False, (f"{name} is already online as a background service "
                           f"(or another aria session) — not attaching")
        _route_logs()
        state = _Running(plugin=plugin, lock=lock)
        state.thread = threading.Thread(target=_run, args=(state,), daemon=True,
                                        name=f"aria-attached-{name}")
        _running[name] = state
        state.thread.start()
    return True, f"{name} attached — messages reach Aria while this window is open"


def stop(name: str, timeout: float = 10.0) -> str:
    with _state_lock:
        state = _running.pop(name, None)
    if state is None:
        return f"{name} is not attached"
    state.stop.set()
    if state.thread is not None:
        state.thread.join(timeout)
        if state.thread.is_alive():
            return (f"{name} is stopping (still finishing a network call; it "
                    f"goes offline when Aria exits at the latest)")
    with _state_lock:
        if not _running:
            _unroute_logs()
    return f"{name} detached — offline"


def stop_all(timeout: float = 10.0) -> None:
    for name in list(_running):
        stop(name, timeout=timeout)


def status() -> list[tuple[str, str]]:
    """[(name, "online" | "stopped: <error>")] for channels attached this session."""
    out = []
    with _state_lock:
        items = list(_running.items())
    for name, st in items:
        alive = st.thread is not None and st.thread.is_alive()
        out.append((name, "online" if alive else f"stopped{': ' + st.error if st.error else ''}"))
    return out
