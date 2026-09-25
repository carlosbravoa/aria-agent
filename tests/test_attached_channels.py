"""Attached mode: a channel online only while the `aria` CLI is open.

Covers the mode setting, the run lock (CLI vs service never both receive),
the in-process runner (start/stop/status, errors never reach the REPL, log
routing, per-channel session cleanup), Telegram's threaded runner, the
/remote REPL command, and the installer (no unit for an attached channel)."""

from __future__ import annotations

import io
import logging
import subprocess
import sys
import textwrap
import threading
import time

import pytest

FAKE = textwrap.dedent('''
    import threading
    from aria.channels import ChannelPlugin, ConfigField
    EVENTS = []
    class Fake(ChannelPlugin):
        name = "fake"
        config_fields = (ConfigField("FAKE_TOKEN", required=True),)
        supports_attached = True
        def run(self): EVENTS.append("run")
        def send(self, text, to=None): EVENTS.append(("send", text))
        def start(self, stop):
            EVENTS.append("start")
            if __import__("os").environ.get("FAKE_BOOM"):
                raise RuntimeError("boom")
            stop.wait()
            EVENTS.append("stopped")
    PLUGIN = Fake()
''')


@pytest.fixture
def env(minimal_env, tmp_path, monkeypatch):
    d = tmp_path / "channels"
    d.mkdir()
    (d / "fake.py").write_text(FAKE)
    monkeypatch.setenv("ARIA_CHANNELS_DIR", str(d))
    monkeypatch.setenv("FAKE_TOKEN", "t")
    for k in ("ARIA_CHANNELS", "TELEGRAM_TOKEN", "WHATSAPP_ALLOWED", "FAKE_BOOM"):
        monkeypatch.delenv(k, raising=False)
    import aria.channels as ch
    ch.reset_cache()
    yield ch
    from aria.channels import attached
    attached.stop_all(timeout=2)
    ch.reset_cache()


def _events():
    return sys.modules["_aria_user_channel_fake"].EVENTS


def _wait(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


# ── mode & selection ──────────────────────────────────────────────────────────

def test_mode_defaults_to_service(env, monkeypatch):
    from aria.channels.base import mode_key
    p = env.get("fake")
    assert p.mode == "service"
    monkeypatch.setenv(mode_key("fake"), "Attached")
    assert p.mode == "attached"
    assert mode_key("my-chan") == "ARIA_CHANNEL_MODE_MY_CHAN"


def test_attached_and_service_channel_lists(env, monkeypatch):
    monkeypatch.setenv("ARIA_CHANNEL_MODE_FAKE", "attached")
    assert [p.name for p in env.attached_channels()] == ["fake"]
    assert "fake" not in [p.name for p in env.service_channels()]
    # a channel that can't run attached stays a service even if asked
    monkeypatch.setenv("WHATSAPP_ALLOWED", "1")
    monkeypatch.setenv("ARIA_CHANNEL_MODE_WHATSAPP", "attached")
    assert "whatsapp" not in [p.name for p in env.attached_channels()]
    assert "whatsapp" in [p.name for p in env.service_channels()]


# ── run lock ──────────────────────────────────────────────────────────────────

def test_run_lock_is_exclusive(minimal_env):
    from aria.channels.runlock import RunLock
    a, b = RunLock("x"), RunLock("x")
    assert a.acquire() and not b.acquire()
    a.release()
    assert b.acquire()
    b.release()


def test_service_waits_for_attached_session(minimal_env):
    from aria.channels.runlock import RunLock, hold_for_service
    cli = RunLock("x")
    assert cli.acquire()
    got = []
    t = threading.Thread(target=lambda: got.append(hold_for_service("x")), daemon=True)
    t.start()
    time.sleep(0.2)
    assert not got                      # blocked, not crash-looping
    cli.release()
    assert _wait(lambda: bool(got))
    got[0].release()


def test_attach_refused_while_service_holds_lock(env, tmp_path):
    """Another PROCESS (the service) holds the lock → attaching is refused."""
    import os
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import sys, time; sys.path.insert(0, sys.argv[1]);"
         "from aria.channels.runlock import RunLock; l=RunLock('fake');"
         "assert l.acquire(); print('held', flush=True); time.sleep(30)",
         str(__import__("pathlib").Path(__file__).parents[1] / "src")],
        stdout=subprocess.PIPE, text=True, env=dict(os.environ))
    try:
        assert holder.stdout.readline().strip() == "held"
        from aria.channels import attached
        ok, msg = attached.start(env.get("fake"))
        assert not ok and "already online" in msg
    finally:
        holder.kill()
        holder.wait()


# ── in-process runner ─────────────────────────────────────────────────────────

def test_start_status_stop(env):
    from aria.channels import attached
    ok, msg = attached.start(env.get("fake"))
    assert ok and "attached" in msg
    assert _wait(lambda: "start" in _events())
    assert attached.status() == [("fake", "online")]
    assert attached.start(env.get("fake"))[1].endswith("already attached")
    assert "offline" in attached.stop("fake")
    assert _events()[-1] == "stopped"
    assert attached.status() == []
    from aria.channels.runlock import RunLock
    lock = RunLock("fake")
    assert lock.acquire()               # released on stop
    lock.release()


def test_plugin_error_never_reaches_the_repl(env, monkeypatch):
    from aria.channels import attached
    monkeypatch.setenv("FAKE_BOOM", "1")
    ok, _ = attached.start(env.get("fake"))
    assert ok
    assert _wait(lambda: attached.status() and attached.status()[0][1].startswith("stopped"))
    assert "boom" in attached.status()[0][1]


def test_logs_routed_to_file_while_attached(env):
    from aria.channels import attached
    lg = logging.getLogger("telegram")
    before = (lg.level, lg.propagate)
    attached.start(env.get("fake"))
    assert lg.propagate is False
    logging.getLogger("aria.channels.x").warning("hello from a channel")
    attached.stop("fake")
    assert (lg.level, lg.propagate) == before
    assert "hello from a channel" in attached.log_path().read_text()


def test_log_file_redacts_secrets(env, monkeypatch):
    from aria.channels import attached
    monkeypatch.setenv("TELEGRAM_TOKEN", "123456:super-secret-token")
    attached.start(env.get("fake"))
    try:
        raise ValueError("token `123456:super-secret-token` rejected")
    except ValueError:
        logging.getLogger("telegram.ext").exception("bootstrap failed")
    attached.stop("fake")
    text = attached.log_path().read_text()
    assert "super-secret-token" not in text and "token `***` rejected" in text


def test_shutdown_only_closes_that_channels_sessions(minimal_env, monkeypatch):
    from aria import channel
    closed = []

    class S:
        def __init__(self, key):
            self.key, self.closed = key, False
            self._lock = threading.Lock()
            self.agent = type("A", (), {"close": lambda s, k=key: closed.append(k)})()
        def cancel(self): pass

    monkeypatch.setattr(channel, "_sessions",
                        {("tg", "1"): S(("tg", "1")), ("wa", "2"): S(("wa", "2"))})
    channel.shutdown("tg")
    assert closed == [("tg", "1")] and list(channel._sessions) == [("wa", "2")]


# ── Telegram's threaded runner ────────────────────────────────────────────────

def test_telegram_run_attached_stops_on_event(minimal_env, monkeypatch):
    import asyncio
    from aria.channels.telegram import bot
    seen = {}

    class FakeApp:
        def run_polling(self, **kw):
            seen.update(kw)
            loop = asyncio.get_event_loop()
            seen["loop"] = loop
            loop.run_forever()          # until stop_running() on this loop
        def stop_running(self):
            asyncio.get_event_loop().stop()

    monkeypatch.setenv("TELEGRAM_TOKEN", "123:abc")
    monkeypatch.setattr(bot, "build_app", lambda token, watchdog=None: FakeApp())
    stop = threading.Event()
    t = threading.Thread(target=bot.run_attached, args=(stop,), daemon=True)
    t.start()
    assert _wait(lambda: "loop" in seen)
    assert seen["stop_signals"] is None          # no signal handlers off the main thread
    stop.set()
    t.join(3)
    assert not t.is_alive()


def test_telegram_plugin_supports_attached(minimal_env):
    from aria.channels.telegram import PLUGIN
    assert PLUGIN.supports_attached


# ── /remote in the REPL ───────────────────────────────────────────────────────

@pytest.fixture
def repl_out(monkeypatch):
    from rich.console import Console
    from aria import main
    buf = io.StringIO()
    monkeypatch.setattr(main, "console", Console(file=buf, width=200))
    return buf


def test_remote_command(env, repl_out):
    from aria import main
    main._remote_command("")
    assert "fake" in repl_out.getvalue() and "offline" in repl_out.getvalue()
    main._remote_command("on")                     # only one capable → no name needed
    assert "attached" in repl_out.getvalue()
    assert _wait(lambda: "start" in _events())
    main._remote_command("")
    assert "online" in repl_out.getvalue()
    main._remote_command("off fake")
    assert "offline" in repl_out.getvalue().splitlines()[-1]
    main._remote_command("sideways")
    assert "Usage" in repl_out.getvalue()


def test_repl_starts_and_stops_attached_channels(env, repl_out, monkeypatch):
    from aria import main
    monkeypatch.setenv("ARIA_CHANNEL_MODE_FAKE", "attached")
    main._start_attached_channels()
    assert _wait(lambda: "start" in _events())
    main._stop_attached_channels()
    assert _events()[-1] == "stopped"
    assert "📱" in repl_out.getvalue()


# ── installer ─────────────────────────────────────────────────────────────────

def test_installer_skips_and_retires_units_for_attached(env, monkeypatch, tmp_path):
    from pathlib import Path
    from aria import install
    monkeypatch.setenv("ARIA_CHANNEL_MODE_FAKE", "attached")
    unit = Path.home() / ".config" / "systemd" / "user" / "aria-channel-fake.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Unit]\n")                   # left over from service mode
    calls = []
    monkeypatch.setattr(install.subprocess, "run", lambda *a, **k: calls.append(a[0]))
    monkeypatch.setattr(install, "_aria_bin", lambda n: f"/opt/bin/{n}")
    services, _ = install._collect_services({"fake"}, dry_run=False)
    assert "aria-channel-fake" not in services
    assert not unit.exists()
    assert ["systemctl", "--user", "disable", "--now", "aria-channel-fake"] in calls


def test_installer_asks_for_mode_when_supported(env, monkeypatch):
    from aria import install
    monkeypatch.setattr(install, "_ask", lambda *a, **k: "v")
    monkeypatch.setattr(install, "_ask_bool", lambda *a, **k: True)
    out = install._configure_channel(env.get("fake"), lambda k: "", dry_run=True)
    assert out["ARIA_CHANNEL_MODE_FAKE"] == "attached"
