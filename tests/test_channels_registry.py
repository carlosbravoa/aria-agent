"""Channel plugin registry: discovery, overrides, enablement (incl. legacy
fallback), push routing, the aria-channel CLI, and the example plugin."""

from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path

import pytest

EXAMPLE = Path(__file__).parents[1] / "docs" / "examples" / "channels" / "webhook.py"

FAKE = '''
from aria.channels import ChannelPlugin, ConfigField
SENT = []
class Fake(ChannelPlugin):
    name = "{name}"
    description = "fake"
    config_fields = (ConfigField("FAKE_TOKEN", required=True),)
    def run(self): SENT.append(("run",))
    def send(self, text, to=None): SENT.append((text, to))
PLUGIN = Fake()
'''


@pytest.fixture
def chdir_(minimal_env, tmp_path, monkeypatch):
    d = tmp_path / "channels"
    d.mkdir()
    monkeypatch.setenv("ARIA_CHANNELS_DIR", str(d))
    monkeypatch.delenv("ARIA_CHANNELS", raising=False)
    monkeypatch.delenv("ARIA_NOTIFY_CHANNEL", raising=False)
    for k in ("TELEGRAM_TOKEN", "WHATSAPP_ALLOWED"):
        monkeypatch.delenv(k, raising=False)
    import aria.channels
    aria.channels.reset_cache()
    yield d
    aria.channels.reset_cache()


def test_user_plugin_discovered(chdir_):
    import aria.channels as ch
    (chdir_ / "fake.py").write_text(FAKE.format(name="fake"))
    (chdir_ / "_helper.py").write_text("raise RuntimeError('never imported')")
    p = ch.get("fake")
    assert p is not None and not p.builtin and p.source.endswith("fake.py")


def test_broken_plugin_is_skipped_not_fatal(chdir_):
    import aria.channels as ch
    (chdir_ / "broken.py").write_text("raise ImportError('boom')")
    (chdir_ / "noplugin.py").write_text("X = 1")
    (chdir_ / "badname.py").write_text(FAKE.format(name="Bad Name"))
    (chdir_ / "fake.py").write_text(FAKE.format(name="fake"))
    assert "fake" in ch.discover()
    assert not {"broken", "noplugin", "Bad Name"} & set(ch.discover())


def test_enablement_legacy_and_explicit(chdir_, monkeypatch):
    import aria.channels as ch
    (chdir_ / "fake.py").write_text(FAKE.format(name="fake"))
    assert "fake" not in [p.name for p in ch.enabled()]         # not configured
    monkeypatch.setenv("FAKE_TOKEN", "x")
    assert "fake" in [p.name for p in ch.enabled()]             # legacy: configured → on
    monkeypatch.setenv("ARIA_CHANNELS", "")
    assert ch.enabled() == []                                   # explicit empty list
    monkeypatch.setenv("ARIA_CHANNELS", " Fake , nope ")
    assert [p.name for p in ch.enabled()] == ["fake"]           # unknown names ignored


def test_push_routing(chdir_, monkeypatch):
    import aria.channels as ch
    (chdir_ / "fake.py").write_text(FAKE.format(name="fake"))
    monkeypatch.setenv("ARIA_CHANNELS", "fake")
    assert ch.push_channel().name == "fake"                      # only push-capable one
    assert ch.push("hi", to="u1") == "fake"
    import sys
    sent = sys.modules["_aria_user_channel_fake"].SENT
    assert sent[-1] == ("hi", "u1")
    monkeypatch.setenv("ARIA_NOTIFY_CHANNEL", "nope")
    with pytest.raises(RuntimeError):
        ch.push("x")


def test_cli_runs_and_lists(chdir_, monkeypatch, capsys):
    import sys
    import aria.channels as ch
    from aria.channels import cli
    (chdir_ / "fake.py").write_text(FAKE.format(name="fake"))
    monkeypatch.setenv("ARIA_CHANNELS", "fake")
    assert cli.main(["--list"]) == 0
    assert "fake" in capsys.readouterr().out
    assert cli.main(["fake"]) == 0
    assert sys.modules["_aria_user_channel_fake"].SENT[-1] == ("run",)
    assert cli.main(["missing"]) == 2
    assert ch.get("fake").services()[0].exec_start == ("aria-channel", "fake")


def test_example_webhook_plugin_end_to_end(chdir_, monkeypatch):
    """The documented example works as a drop-in: discovered, runs an HTTP
    server, and hands messages to the agent host."""
    import httpx
    import aria.channels as ch
    from aria.channels import host
    shutil.copy(EXAMPLE, chdir_ / "webhook.py")
    monkeypatch.setenv("ARIA_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setenv("ARIA_WEBHOOK_PORT", "0")
    plugin = ch.get("webhook")
    assert plugin is not None and plugin.is_configured()

    seen = {}

    def fake_handle(channel, user, text, **kw):
        seen["args"] = (channel, user, text)
        return ["pong"]

    monkeypatch.setattr(host, "handle_message", fake_handle)
    import http.server
    servers = []
    real = http.server.ThreadingHTTPServer

    class Capture(real):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            servers.append(self)

    monkeypatch.setattr(http.server, "ThreadingHTTPServer", Capture)
    monkeypatch.setattr(host, "shutdown", lambda: None)
    t = threading.Thread(target=plugin.run, daemon=True)
    t.start()
    for _ in range(100):
        if servers:
            break
        time.sleep(0.02)
    port = servers[0].server_address[1]
    url = f"http://127.0.0.1:{port}/message"
    try:
        r = httpx.post(url, json={"user": "alice", "text": "ping"},
                       headers={"X-Aria-Secret": "s3cret"})
        assert r.json() == {"replies": ["pong"]}
        assert seen["args"] == ("webhook", "alice", "ping")
        assert httpx.post(url, json={}, headers={"X-Aria-Secret": "nope"}).status_code == 403
    finally:
        servers[0].shutdown()
    with pytest.raises(RuntimeError):
        plugin.send("x")                     # no ARIA_WEBHOOK_OUT_URL


OVERRIDE = '''
from aria.channels import ChannelPlugin
class MyTelegram(ChannelPlugin):
    name = "telegram"
    description = "my telegram"
    {flag}
    def send(self, text, to=None): pass
PLUGIN = MyTelegram()
'''


def test_overriding_a_builtin_needs_opt_in(chdir_):
    import aria.channels as ch
    (chdir_ / "telegram.py").write_text(OVERRIDE.format(flag=""))
    assert ch.get("telegram").builtin                     # silently shadowing refused
    (chdir_ / "telegram.py").write_text(OVERRIDE.format(flag="override = True"))
    ch.reset_cache()
    p = ch.get("telegram")
    assert not p.builtin and p.description == "my telegram"
    # reuses the legacy unit name → the installer replaces aria-telegram
    # instead of adding a second poller on the same token
    [spec] = p.services()
    assert spec.unit == "aria-telegram" and spec.exec_start == ("aria-channel", "telegram")


def test_aria_channels_none_means_no_channels(chdir_, monkeypatch):
    import aria.channels as ch
    monkeypatch.setenv("TELEGRAM_TOKEN", "x")
    monkeypatch.setenv("ARIA_CHANNELS", "none")
    assert ch.enabled() == []


def test_update_refresh_uses_new_code_in_subprocess(chdir_, monkeypatch):
    """The refresh runs in a fresh interpreter (the updating process may hold
    stale modules) and returns the plugin's ok/warn notes."""
    from aria.tools import update
    (chdir_ / "inst.py").write_text(
        "from aria.channels import ChannelPlugin\n"
        "class P(ChannelPlugin):\n"
        "    name = 'inst'\n"
        "    def install(self, dry_run=False):\n"
        "        return [('warn', 'Run: npm ci'), ('info', 'fine')]\n"
        "PLUGIN = P()\n")
    monkeypatch.setenv("ARIA_CHANNELS", "inst")
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).parents[1] / "src"))
    monkeypatch.setattr(update, "_refresh_channel_files_inproc",
                        lambda lines: lines.append("IN-PROCESS FALLBACK"))
    lines: list[str] = []
    update._refresh_channel_files(lines)
    assert lines == ["📲 Run: npm ci"]
