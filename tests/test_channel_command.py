"""`/channel`: start/stop/restart/logs channel services from inside Aria —
systemd backend (same unit text as aria-install) and the detached-process
fallback, plus the REPL command."""

from __future__ import annotations

import io
import sys
import textwrap
import time
from pathlib import Path

import pytest

FAKE = textwrap.dedent('''
    from aria.channels import ChannelPlugin, ConfigField
    class Fake(ChannelPlugin):
        name = "fake"
        description = "fake channel"
        config_fields = (ConfigField("FAKE_TOKEN", required=True),)
        def send(self, text, to=None): pass
    PLUGIN = Fake()
''')


@pytest.fixture
def env(minimal_env, tmp_path, monkeypatch):
    d = tmp_path / "channels"
    d.mkdir()
    (d / "fake.py").write_text(FAKE)
    monkeypatch.setenv("ARIA_CHANNELS_DIR", str(d))
    monkeypatch.setenv("FAKE_TOKEN", "t")
    envf = Path.home() / ".aria" / ".env"
    envf.parent.mkdir(parents=True, exist_ok=True)
    envf.write_text("LLM_MODEL=x\nARIA_CHANNELS=telegram\n")
    monkeypatch.setenv("ARIA_ENV", str(envf))
    monkeypatch.setenv("ARIA_CHANNELS", "telegram")
    import aria.channels as ch
    ch.reset_cache()
    yield envf
    ch.reset_cache()


@pytest.fixture
def systemd(env, monkeypatch):
    from aria import install
    from aria.channels import services
    calls = []

    def systemctl(*args):
        calls.append(args)
        out = "active\n" if args[0] == "is-active" else ""
        return type("R", (), {"returncode": 0, "stdout": out, "stderr": ""})()
    monkeypatch.setattr(services, "_systemd", lambda: True)
    monkeypatch.setattr(services, "_systemctl", systemctl)
    monkeypatch.setattr(install, "_resolve_exe", lambda exe: f"/opt/bin/{exe}")
    monkeypatch.setattr(install, "_linger_enabled", lambda: True)
    return calls


def test_start_writes_the_installer_unit_and_enables_it(systemd, env):
    from aria import install
    from aria.channels import services
    out = services.start("fake")
    unit = Path.home() / ".config/systemd/user/aria-channel-fake.service"
    assert unit.read_text() == install._service(
        description="Aria fake channel", exec_start="/opt/bin/aria-channel fake",
        env_file=str(env))
    assert ("enable", "--now", "aria-channel-fake") in systemd
    assert "aria-channel-fake: active" in out
    # an explicit ARIA_CHANNELS gains the channel, in .env too
    assert "ARIA_CHANNELS=telegram,fake" in env.read_text()
    assert services.service_state(services._plugin("fake")) == "active"


def test_start_refuses_an_unconfigured_channel(systemd, monkeypatch):
    from aria.channels import services
    monkeypatch.delenv("FAKE_TOKEN")
    with pytest.raises(services.ChannelServiceError, match="FAKE_TOKEN"):
        services.start("fake")


def test_start_explains_a_missing_binary(systemd, monkeypatch):
    from aria import install
    from aria.channels import services
    monkeypatch.setattr(install, "_resolve_exe", lambda exe: None)
    with pytest.raises(services.ChannelServiceError, match="pip install"):
        services.start("fake")


def test_stop_disables_and_restart_starts_when_not_installed(systemd):
    from aria.channels import services
    assert services.stop("fake") == ["fake has no service installed — nothing to stop."]
    services.restart("fake")                       # nothing installed → a start
    assert ("enable", "--now", "aria-channel-fake") in systemd
    assert services.stop("fake") == ["aria-channel-fake: stopped and disabled"]
    assert ("disable", "--now", "aria-channel-fake") in systemd


def test_unknown_channel(systemd):
    from aria.channels import services
    with pytest.raises(services.ChannelServiceError, match="no channel 'nope'"):
        services.start("nope")


def test_process_backend_without_systemd(env, monkeypatch, tmp_path):
    """No systemd: the channel runs detached with a pidfile and a log."""
    from aria import install
    from aria.channels import services
    script = tmp_path / "fake-channel"
    script.write_text(f"#!{sys.executable}\nimport time\nprint('fake up', flush=True)\ntime.sleep(60)\n")
    script.chmod(0o755)
    monkeypatch.setattr(services, "_systemd", lambda: False)
    monkeypatch.setattr(install, "_resolve_exe", lambda exe: str(script))
    out = services.start("fake")
    assert any("running (pid" in line for line in out)
    assert services.service_state(services._plugin("fake")).startswith("running")
    time.sleep(0.5)
    assert "fake up" in services.logs("fake")
    assert services.stop("fake")[0].startswith("aria-channel-fake: stopped")
    time.sleep(0.3)
    assert services.service_state(services._plugin("fake")) == "stopped"


def test_repl_channel_command(systemd, monkeypatch):
    from rich.console import Console
    from aria import main
    buf = io.StringIO()
    monkeypatch.setattr(main, "console", Console(file=buf, width=200))
    main._channel_command("")
    assert "fake" in buf.getvalue() and "service:" in buf.getvalue()
    main._channel_command("start fake")
    assert "aria-channel-fake: active" in buf.getvalue()
    main._channel_command("start")
    assert "Usage" in buf.getvalue()
    main._channel_command("start nope")
    assert "no channel 'nope'" in buf.getvalue()
