"""The unified /channel command: plain-words status, commands that cooperate
(moving a channel between the background and this window, pausing and
resuming the service), setup prompts, and /remote as an alias."""

from __future__ import annotations

import io
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

ATTACHABLE = textwrap.dedent('''
    from aria.channels import ChannelPlugin, ConfigField
    class Fake(ChannelPlugin):
        name = "fake"
        description = "fake"
        config_fields = (ConfigField("FAKE_TOKEN", required=True, prompt="FAKE_TOKEN"),
                         ConfigField("FAKE_OPT", default="d1"))
        supports_attached = True
        def start(self, stop): stop.wait()
        def send(self, text, to=None): pass
    PLUGIN = Fake()
''')
SERVICE_ONLY = textwrap.dedent('''
    from aria.channels import ChannelPlugin, ConfigField
    class Svc(ChannelPlugin):
        name = "svc"
        config_fields = (ConfigField("SVC_TOKEN", required=True),)
        def send(self, text, to=None): pass
    PLUGIN = Svc()
''')


@pytest.fixture
def ux(minimal_env, tmp_path, monkeypatch):
    from rich.console import Console
    from aria import install, main
    from aria.channels import attached, control, services
    d = tmp_path / "channels"
    d.mkdir()
    (d / "fake.py").write_text(ATTACHABLE)
    (d / "svc.py").write_text(SERVICE_ONLY)
    monkeypatch.setenv("ARIA_CHANNELS_DIR", str(d))
    for k in ("TELEGRAM_TOKEN", "WHATSAPP_ALLOWED", "VICUS_SITE", "VICUS_EMAIL",
              "VICUS_PASSWORD", "VICUS_ALLOWED", "VICUS_SOURCE_DIR", "ARIA_CHANNELS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("FAKE_TOKEN", "t")
    monkeypatch.setenv("SVC_TOKEN", "t")
    envf = Path.home() / ".aria" / ".env"
    envf.parent.mkdir(parents=True, exist_ok=True)
    envf.write_text("LLM_MODEL=x\n")
    monkeypatch.setenv("ARIA_ENV", str(envf))

    # a fake systemd: unit files are real (in the tmp HOME), state is ours
    active: set[str] = set()
    calls: list[tuple] = []

    def systemctl(*args):
        calls.append(args)
        verb, units = args[0], [a for a in args[1:] if not a.startswith("-")]
        if verb in ("enable", "start", "restart"):
            active.update(units)
        elif verb in ("disable", "stop"):
            active.difference_update(units)
        out = ("active" if units and units[0] in active else "inactive") if verb == "is-active" else ""
        return type("R", (), {"returncode": 0, "stdout": out + "\n", "stderr": ""})()

    monkeypatch.setattr(services, "_systemd", lambda: True)
    monkeypatch.setattr(services, "_systemctl", systemctl)
    monkeypatch.setattr(install, "_resolve_exe", lambda exe: f"/opt/bin/{exe}")
    monkeypatch.setattr(install, "_linger_enabled", lambda: True)
    buf = io.StringIO()
    monkeypatch.setattr(main, "console", Console(file=buf, width=250))
    main._paused.clear()

    class Agent:
        name = "Aria"
    control.attach_repl(Agent(), None)
    import aria.channels as ch
    ch.reset_cache()

    def install_service(name):
        unit = Path.home() / ".config/systemd/user" / f"aria-channel-{name}.service"
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text("[Unit]\n")
        active.add(f"aria-channel-{name}")

    def out():
        return buf.getvalue()

    yield type("UX", (), {"main": main, "out": staticmethod(out), "active": active,
                          "calls": calls, "install_service": staticmethod(install_service),
                          "env": envf, "buf": buf})
    control.detach_repl()
    attached.stop_all(timeout=2)
    main._paused.clear()
    ch.reset_cache()


def _line(ux, name):
    ux.buf.truncate(0)
    ux.buf.seek(0)
    ux.main._channel_command("")
    return next(line for line in ux.out().splitlines() if line.strip().startswith(name))


def test_status_in_plain_words(ux, monkeypatch):
    assert "offline" in _line(ux, "fake")
    monkeypatch.delenv("FAKE_TOKEN")
    assert "not set up (missing FAKE_TOKEN)  → /channel setup fake" in _line(ux, "fake")
    monkeypatch.setenv("FAKE_TOKEN", "t")
    ux.install_service("fake")
    assert "online in the background" in _line(ux, "fake")


def test_on_brings_it_into_this_window(ux):
    ux.main._channel_command("on fake")
    assert "fake is online in this window" in ux.out()
    assert "online in this window" in _line(ux, "fake")


def test_on_moves_a_running_service_here_and_gives_it_back_on_quit(ux):
    ux.install_service("fake")
    ux.main._channel_command("on fake")
    assert ("stop", "aria-channel-fake") in ux.calls                # paused, not disabled
    assert not any(c[0] == "disable" for c in ux.calls)
    assert "moved from the background" in ux.out()
    assert "background service resumes when you quit" in _line(ux, "fake")
    ux.main._stop_attached_channels()                             # the REPL quits
    assert ("start", "aria-channel-fake") in ux.calls
    assert "fake is back online in the background" in ux.out()
    assert "aria-channel-fake" in ux.active


def test_on_for_a_service_only_channel_starts_it_in_the_background(ux):
    ux.main._channel_command("on svc")
    assert "can't run inside this window — starting it in the background" in ux.out()
    assert ("enable", "--now", "aria-channel-svc") in ux.calls
    assert "online in the background" in _line(ux, "svc")


def test_always_hands_this_window_over_to_the_service(ux):
    from aria.channels import attached
    ux.main._channel_command("on fake")
    ux.main._channel_command("on fake --always")
    assert "fake" not in dict(attached.status())
    assert ("enable", "--now", "aria-channel-fake") in ux.calls
    assert "online in the background" in _line(ux, "fake")


def test_off_means_offline_everywhere(ux):
    from aria.channels import attached
    ux.install_service("fake")
    ux.main._channel_command("on fake")                            # here, service paused
    ux.main._channel_command("off fake")
    assert "fake" not in dict(attached.status())
    assert ("disable", "--now", "aria-channel-fake") in ux.calls
    assert "fake" not in ux.main._paused                          # won't come back on quit
    assert "offline" in _line(ux, "fake")


def test_control_and_release(ux):
    from aria.channels import control
    ux.main._channel_command("control fake")
    assert control.is_controlled("fake")
    assert "controls this session" in _line(ux, "fake")
    ux.main._channel_command("release")                           # only one → no name needed
    assert not control.is_controlled("fake")
    assert "online in this window" in _line(ux, "fake")


def test_on_when_not_set_up_points_to_setup(ux, monkeypatch):
    monkeypatch.delenv("FAKE_TOKEN")
    ux.main._channel_command("on fake")
    assert "isn't set up yet (missing FAKE_TOKEN). Run /channel setup fake." in ux.out()


def test_setup_asks_and_saves(ux, monkeypatch):
    monkeypatch.delenv("FAKE_TOKEN")
    answers = iter(["new-token", ""])                              # token; keep default opt
    monkeypatch.setattr(ux.main.console, "input", lambda *a, **k: next(answers))
    ux.main._channel_command("setup fake")
    text = ux.env.read_text()
    assert "FAKE_TOKEN=new-token" in text and "FAKE_OPT=d1" in text
    assert ux.env.stat().st_mode & 0o777 == 0o600
    assert "Next: /channel on fake" in ux.out()
    assert "offline" in _line(ux, "fake")                         # now set up


def test_setup_cancels_without_a_required_value(ux, monkeypatch):
    monkeypatch.delenv("FAKE_TOKEN")
    monkeypatch.setattr(ux.main.console, "input", lambda *a, **k: "")
    ux.main._channel_command("setup fake")
    assert "FAKE_TOKEN is required — setup cancelled" in ux.out()
    assert "FAKE_TOKEN" not in ux.env.read_text()


def test_ambiguity_asks_which(ux):
    ux.main._channel_command("on")
    assert "Which channel? /channel on <name> — fake, svc" in ux.out()
    ux.main._channel_command("on nope")
    assert "No channel 'nope'" in ux.out()


def test_another_window_holding_the_channel(ux):
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import sys, time; sys.path.insert(0, sys.argv[1]);"
         "from aria.channels.runlock import RunLock; l=RunLock('fake');"
         "assert l.acquire(); print('held', flush=True); time.sleep(30)",
         str(Path(__file__).parents[1] / "src")],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        assert "online in another aria window" in _line(ux, "fake")
        ux.main._channel_command("on fake")
        assert "online in another aria window — quit that one first" in ux.out()
    finally:
        holder.kill()
        holder.wait()


def test_remote_is_an_alias_and_its_off_only_leaves_this_window(ux):
    from aria.channels import attached
    ux.install_service("fake")
    ux.main._remote_command("on fake")
    assert "fake" in dict(attached.status())
    ux.main._remote_command("off")
    # it had paused the service to come here; leaving hands it straight back
    assert "fake left this window and is back online in the background" in ux.out()
    assert not any(c[0] == "disable" for c in ux.calls)
    assert ("start", "aria-channel-fake") in ux.calls
    assert "fake" not in ux.main._paused
    ux.main._remote_command("on fake")
    ux.main._stop_attached_channels()
    time.sleep(0.1)


def test_remote_off_without_a_paused_service(ux):
    ux.main._remote_command("on fake")
    ux.main._remote_command("off fake")
    assert "fake is offline in this window" in ux.out()


def test_help_lines_survive_rich_markup(ux):
    """The usage text contains [..] — it must print literally, not as markup."""
    ux.main._channel_command("")
    assert "/channel [on|off|control|release|setup|restart|logs] <name>" in ux.out()
    ux.main._channel_command("bogus")
    assert "Usage: /channel [on|off|control|release|setup|restart|logs] <name>" in ux.out()
