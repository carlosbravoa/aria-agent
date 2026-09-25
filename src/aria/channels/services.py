"""
aria/channels/services.py — Start and stop channel services from inside Aria.

Backs the REPL's `/channel` command, so running a channel in the background
doesn't need a second terminal or a trip through aria-install:

  status()        every channel with its service state
  start(name)     deploy its helper files, write its unit(s) if needed — the
                  same unit text aria-install writes — then enable + start
  stop(name)      disable + stop (it stays off, also after a reboot)
  restart(name)
  logs(name)

Backends: systemd --user when available; otherwise the channel runs as a
detached process (`aria-channel <name>` etc.) with a pidfile in ~/.aria/run
and a log in ~/.aria/logs. Every function returns human-readable lines and
raises ChannelServiceError for anything the user must fix.
"""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

from aria.channels.base import ChannelPlugin, ServiceSpec


class ChannelServiceError(Exception):
    pass


def _systemd() -> bool:
    from aria import install
    return install._systemd_available()


def _unit_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def _run_dir() -> Path:
    d = Path.home() / ".aria" / "run"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def _log_dir() -> Path:
    d = Path.home() / ".aria" / "logs"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def _env_file() -> Path:
    raw = os.environ.get("ARIA_ENV", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".aria" / ".env"


def _plugin(name: str) -> ChannelPlugin:
    from aria import channels
    p = channels.get(name)
    if p is None:
        known = ", ".join(sorted(channels.discover()))
        raise ChannelServiceError(f"no channel '{name}' (available: {known})")
    return p


def _missing_settings(p: ChannelPlugin) -> list[str]:
    keys = list(p.legacy_keys) or [f.key for f in p.config_fields if f.required]
    return [k for k in keys if not os.environ.get(k, "").strip()]


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)


# ── Status ────────────────────────────────────────────────────────────────────

def _unit_state(unit: str) -> str:
    if not (_unit_dir() / f"{unit}.service").exists():
        return "not installed"
    state = _systemctl("is-active", unit).stdout.strip() or "unknown"
    return state


def _pid(unit: str) -> int | None:
    try:
        pid = int((_run_dir() / f"{unit}.pid").read_text().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def service_state(p: ChannelPlugin) -> str:
    """The channel's main unit state: active / inactive / failed / not installed,
    or running (pid N) / stopped without systemd."""
    unit = p.services()[0].unit
    if _systemd():
        return _unit_state(unit)
    pid = _pid(unit)
    return f"running (pid {pid})" if pid else "stopped"


def status() -> list[tuple[ChannelPlugin, str]]:
    from aria import channels
    return [(p, service_state(p)) for _, p in sorted(channels.discover().items())]


# ── Start / stop ──────────────────────────────────────────────────────────────

def _resolved(spec: ServiceSpec) -> tuple[str | None, list[str]]:
    from aria import install
    exe = install._resolve_exe(spec.exec_start[0])
    return exe, list(spec.exec_start[1:])


def _ensure_listed(name: str) -> str | None:
    """With an explicit ARIA_CHANNELS, add `name` to it (in .env and here) so
    the rest of Aria — the updater, push routing — treats it as enabled."""
    raw = os.environ.get("ARIA_CHANNELS")
    if raw is None:
        return None                                   # legacy auto-enable covers it
    names = [n.strip().lower() for n in raw.split(",") if n.strip() and n.strip() != "none"]
    if name in names:
        return None
    names.append(name)
    value = ",".join(names)
    env = _env_file()
    try:
        lines = env.read_text(encoding="utf-8").splitlines()
    except OSError:
        return f"Couldn't update ARIA_CHANNELS in {env} — add {name} yourself."
    out, done = [], False
    for line in lines:
        if line.strip().startswith("ARIA_CHANNELS="):
            out.append(f"ARIA_CHANNELS={value}")
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f"ARIA_CHANNELS={value}")
    from aria.setup import write_private
    write_private(env, "\n".join(out) + "\n")
    os.environ["ARIA_CHANNELS"] = value
    from aria import channels
    channels.reset_cache()
    return f"Added {name} to ARIA_CHANNELS in {env}."


def start(name: str) -> list[str]:
    p = _plugin(name)
    missing = _missing_settings(p)
    if missing:
        raise ChannelServiceError(
            f"{name} isn't configured — set {', '.join(missing)} in {_env_file()} "
            f"(or run aria-install)")
    out: list[str] = []
    for note in p.install(dry_run=False) or []:
        level, text = note if isinstance(note, tuple) else ("info", note)
        if level in ("ok", "warn"):
            out.append(("⚠ " if level == "warn" else "") + text)
    listed = _ensure_listed(p.name)
    if listed:
        out.append(listed)
    if p.runs_attached:
        out.append(f"Note: {name} is configured to run attached (ARIA_CHANNEL_MODE); "
                   f"the service takes over whenever no `aria` session holds it.")

    units: list[tuple[ServiceSpec, str, list[str]]] = []
    for spec in p.services():
        exe, args = _resolved(spec)
        if exe is None:
            if spec.optional:
                out.append(f"⚠ {Path(spec.exec_start[0]).name} not found — {spec.unit} skipped")
                continue
            hint = " (pip install . to get it)" if spec.exec_start[0].startswith("aria-") else ""
            raise ChannelServiceError(f"{spec.exec_start[0]} not found{hint}")
        missing_files = [a for a in args if os.path.isabs(a) and not Path(a).exists()]
        if missing_files:
            if spec.optional:
                out.append(f"⚠ {missing_files[0]} not found — {spec.unit} skipped")
                continue
            raise ChannelServiceError(f"{missing_files[0]} not found")
        units.append((spec, exe, args))

    if _systemd():
        return out + _start_systemd(units)
    return out + _start_process(units)


def _start_systemd(units) -> list[str]:
    from aria import install
    d = _unit_dir()
    d.mkdir(parents=True, exist_ok=True)
    out = []
    names = []
    for spec, exe, args in units:
        text = install._service(
            description=spec.description, exec_start=" ".join([exe, *args]),
            env_file=str(_env_file()), after=spec.requires, requires=spec.requires)
        path = d / f"{spec.unit}.service"
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
            out.append(f"Wrote {path.name}")
        names.append(spec.unit)
    _systemctl("daemon-reload")
    r = _systemctl("enable", "--now", *names)
    if r.returncode != 0:
        raise ChannelServiceError(f"systemctl enable --now failed: {r.stderr.strip()}")
    for unit in names:
        out.append(f"{unit}: {_unit_state(unit)}")
    if not install._linger_enabled():
        out.append("⚠ User lingering is off: services stop when you log out "
                   "(loginctl enable-linger to keep them running).")
    return out


def _start_process(units) -> list[str]:
    out = []
    for spec, exe, args in units:
        if _pid(spec.unit):
            out.append(f"{spec.unit}: already running (pid {_pid(spec.unit)})")
            continue
        log = open(_log_dir() / f"{spec.unit}.log", "ab")
        proc = subprocess.Popen([exe, *args], stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True)
        (_run_dir() / f"{spec.unit}.pid").write_text(str(proc.pid))
        out.append(f"{spec.unit}: running (pid {proc.pid}; log {_log_dir() / (spec.unit + '.log')})")
    out.append("⚠ No systemd here: it won't restart by itself or start at boot.")
    return out


def stop(name: str) -> list[str]:
    p = _plugin(name)
    units = [s.unit for s in p.services()]
    if _systemd():
        installed = [u for u in units if (_unit_dir() / f"{u}.service").exists()]
        if not installed:
            return [f"{name} has no service installed — nothing to stop."]
        r = _systemctl("disable", "--now", *reversed(installed))
        if r.returncode != 0:
            raise ChannelServiceError(f"systemctl disable --now failed: {r.stderr.strip()}")
        return [f"{u}: stopped and disabled" for u in installed]
    out = []
    for unit in reversed(units):
        pid = _pid(unit)
        if pid:
            os.kill(pid, signal.SIGTERM)
            out.append(f"{unit}: stopped (pid {pid})")
        (_run_dir() / f"{unit}.pid").unlink(missing_ok=True)
    return out or [f"{name} isn't running."]


def restart(name: str) -> list[str]:
    p = _plugin(name)
    if _systemd():
        units = [s.unit for s in p.services() if (_unit_dir() / f"{s.unit}.service").exists()]
        if not units:
            return start(name)
        r = _systemctl("restart", *units)
        if r.returncode != 0:
            raise ChannelServiceError(f"systemctl restart failed: {r.stderr.strip()}")
        return [f"{u}: {_unit_state(u)}" for u in units]
    return stop(name) + start(name)


def logs(name: str, lines: int = 30) -> str:
    p = _plugin(name)
    unit = p.services()[0].unit
    if _systemd():
        r = subprocess.run(["journalctl", "--user", "-u", unit, "-n", str(lines),
                            "--no-pager", "-o", "cat"], capture_output=True, text=True)
        return r.stdout.strip() or r.stderr.strip() or f"No log lines for {unit}."
    path = _log_dir() / f"{unit}.log"
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:]) or "(empty)"
    except OSError:
        return f"No log for {unit} yet."
