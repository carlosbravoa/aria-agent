"""Remote control (mode B): an attached channel drives the terminal's own
session — handoff between channel thread and REPL, host routing, the
interruptible prompt, remote turns in the REPL loop, mirroring of local turns,
/remote control|release, and cleanup when the REPL exits."""

from __future__ import annotations

import io
import sys
import textwrap
import threading
import time

import pytest

FAKE = textwrap.dedent('''
    from aria.channels import ChannelPlugin, ConfigField
    SENT = []
    class Fake(ChannelPlugin):
        name = "fake"
        config_fields = (ConfigField("FAKE_TOKEN", required=True),)
        supports_attached = True
        def start(self, stop): stop.wait()
        def send(self, text, to=None): SENT.append((text, to))
    PLUGIN = Fake()
''')


@pytest.fixture
def env(minimal_env, tmp_path, monkeypatch):
    d = tmp_path / "channels"
    d.mkdir()
    (d / "fake.py").write_text(FAKE)
    monkeypatch.setenv("ARIA_CHANNELS_DIR", str(d))
    monkeypatch.setenv("FAKE_TOKEN", "t")
    for k in ("ARIA_CHANNELS", "TELEGRAM_TOKEN", "WHATSAPP_ALLOWED"):
        monkeypatch.delenv(k, raising=False)
    import aria.channels as ch
    from aria.channels import attached, control
    ch.reset_cache()
    yield ch
    control.detach_repl()
    attached.stop_all(timeout=2)
    ch.reset_cache()


@pytest.fixture
def out(monkeypatch):
    from rich.console import Console
    from aria import main
    buf = io.StringIO()
    monkeypatch.setattr(main, "console", Console(file=buf, width=200))
    return buf


def _wait(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


class FakeAgent:
    """Records the delivery context each turn ran under."""
    name = "Aria"

    def __init__(self):
        self.turns = []

    def chat_mirrored(self, text, response_cb=None, activity_cb=None):
        from aria import context
        self.turns.append((text, context.current()))
        if response_cb:
            response_cb(f"reply to {text}")
        return [f"reply to {text}"]


def _sent():
    return sys.modules["_aria_user_channel_fake"].SENT


# ── control handoff ───────────────────────────────────────────────────────────

def test_submit_blocks_until_the_repl_runs_it(env):
    from aria.channels import control
    wakes = []
    control.attach_repl(object(), lambda: wakes.append(1))
    control.enable("fake")
    got = []
    t = threading.Thread(target=lambda: got.append(control.submit("fake", "u1", "hi")))
    t.start()
    assert _wait(control.pending) and wakes
    turn = control.take()
    assert (turn.channel, turn.user_id, turn.text) == ("fake", "u1", "hi")
    assert not got                                   # still waiting for the REPL
    turn.finish(["done"])
    t.join(2)
    assert got == [["done"]]
    assert control.controlled() == {"fake": "u1"}    # remembers who wrote


def test_detach_answers_pending_turns(env):
    from aria.channels import control
    control.attach_repl(object(), None)
    control.enable("fake")
    got = []
    t = threading.Thread(target=lambda: got.append(control.submit("fake", "u1", "hi")))
    t.start()
    assert _wait(control.pending)
    control.detach_repl()
    t.join(2)
    assert "closed" in got[0][0]
    assert not control.is_controlled("fake")


def test_enable_needs_a_listening_repl(env):
    from aria.channels import control
    with pytest.raises(RuntimeError):
        control.enable("fake")


# ── host routing ──────────────────────────────────────────────────────────────

def test_host_routes_controlled_channels_to_the_repl(env, monkeypatch):
    from aria import channel as sessions
    from aria.channels import control, host
    monkeypatch.setattr(sessions, "handle", lambda *a, **k: ["own session"])
    repl_agent = object()
    assert host.handle_message("fake", "u1", "x") == ["own session"]
    control.attach_repl(repl_agent, None)
    control.enable("fake")
    result = []
    worker = threading.Thread(target=lambda: result.append(host.handle_message("fake", "u1", "x")))
    worker.start()
    assert _wait(control.pending)
    control.take().finish(["from the terminal"])
    worker.join(2)
    assert result == [["from the terminal"]]
    assert host.get_agent("fake", "u1") is repl_agent
    assert host.handle_message("other", "u1", "x") == ["own session"]


# ── Agent.chat_mirrored ───────────────────────────────────────────────────────

def test_chat_mirrored_renders_and_streams(minimal_env, native_client):
    from aria.agent import Agent
    a = Agent(window_key="repl", terminal=True)
    a.client = native_client("Hello from the model")
    rendered = []
    a._render_answer = lambda text: rendered.append(text)
    streamed = []
    assert a.chat_mirrored("hi", response_cb=streamed.append) == ["Hello from the model"]
    assert rendered == ["Hello from the model"] and streamed == ["Hello from the model"]
    assert a._is_terminal                               # terminal mode untouched
    assert a._response_cb is None


def test_chat_mirrored_reports_an_interrupt(minimal_env, monkeypatch):
    from aria.agent import Agent
    a = Agent(window_key="repl", terminal=True)

    def boom():
        raise KeyboardInterrupt
    monkeypatch.setattr(a, "_run_loop", boom)
    assert a.chat_mirrored("hi") == ["(interrupted at the terminal)"]


# ── the interruptible prompt ──────────────────────────────────────────────────

class FakeApp:
    def __init__(self):
        self.is_running = True
        self.result = None
        self.loop = type("L", (), {"call_soon_threadsafe": lambda s, fn: fn()})()

    def exit(self, result=None):
        self.result = result
        self.is_running = False


def test_waker_keeps_typed_text(minimal_env):
    from aria import main
    session = type("S", (), {})()
    session.app = FakeApp()
    session.default_buffer = type("B", (), {"text": "half-typed thought"})()
    w = main._Waker(session)
    w.wake()
    assert session.app.result == main._WAKE
    assert w.take_saved() == "half-typed thought" and w.take_saved() == ""
    session.app = FakeApp()
    session.app.is_running = False
    w.wake()                                         # not prompting → no-op
    assert session.app.result is None


def test_waker_pre_run_catches_a_queued_turn(env):
    from aria import main
    from aria.channels import control
    session = type("S", (), {})()
    session.app = FakeApp()
    session.default_buffer = type("B", (), {"text": ""})()
    w = main._Waker(session)
    w.pre_run()
    assert session.app.result is None                # nothing queued
    control.attach_repl(object(), None)
    control.enable("fake")
    threading.Thread(target=lambda: control.submit("fake", "u", "x"), daemon=True).start()
    assert _wait(control.pending)
    w.pre_run()
    assert session.app.result == main._WAKE


# ── the REPL loop ─────────────────────────────────────────────────────────────

def test_repl_runs_a_remote_turn_in_its_own_session(env, out, monkeypatch):
    from aria import context, main
    from aria.channels import control
    agent = FakeAgent()
    control.attach_repl(agent, None)
    control.enable("fake")
    prompts = iter([main._WAKE, "/quit"])

    def fake_prompt(session, waker=None):
        if not getattr(fake_prompt, "fired", False):
            fake_prompt.fired = True              # a phone message arrives while prompting
            got.append(None)
            threading.Thread(target=lambda: got.__setitem__(
                0, control.submit("fake", "u1", "hello", response_cb=streamed.append)),
                daemon=True).start()
            assert _wait(control.pending)
        return next(prompts)

    got: list = []
    streamed: list = []
    monkeypatch.setattr(main, "_prompt", fake_prompt)
    main._repl_loop(agent, session=None)
    assert _wait(lambda: got and got[0] is not None)
    assert got[0] == ["reply to hello"] and streamed == ["reply to hello"]
    text, ctx = agent.turns[0]
    assert text == "hello" and (ctx.channel, ctx.user_id) == ("fake", "u1")
    assert context.current() is None              # reset after the turn
    assert "📱 fake › hello" in out.getvalue()


def test_local_turns_are_mirrored_to_the_phone(env, out):
    from aria import main
    from aria.channels import control
    env.get("fake")                               # load the plugin module
    agent = FakeAgent()
    control.attach_repl(agent, None)
    control.enable("fake")
    main._chat_local(agent, "status?")
    assert _wait(lambda: len(_sent()) >= 2)
    assert ("💻 status?", None) in _sent() and ("reply to status?", None) in _sent()
    assert agent.turns[0][1] is None              # local turn: no channel context


# ── /remote control|release and startup ───────────────────────────────────────

def test_remote_control_and_release(env, out):
    from aria import main
    from aria.channels import control
    control.attach_repl(FakeAgent(), None)
    main._remote_command("control")
    assert control.is_controlled("fake")
    main._remote_command("")
    assert "controls this session" in out.getvalue()
    main._remote_command("release")
    assert not control.is_controlled("fake")
    main._remote_command("control fake")
    main._remote_command("off")
    assert not control.is_controlled("fake")


def test_control_mode_takes_control_at_startup(env, out, monkeypatch):
    from aria import main
    from aria.channels import control
    monkeypatch.setenv("ARIA_CHANNEL_MODE_FAKE", "control")
    control.attach_repl(FakeAgent(), None)
    main._start_attached_channels()
    assert control.is_controlled("fake")
    assert [p.name for p in env.attached_channels()] == ["fake"]
    assert "fake" not in [p.name for p in env.service_channels()]


def test_installer_keeps_control_mode(env, monkeypatch):
    from aria import install
    monkeypatch.setenv("ARIA_CHANNEL_MODE_FAKE", "control")
    monkeypatch.setattr(install, "_ask", lambda *a, **k: "v")
    monkeypatch.setattr(install, "_ask_bool", lambda *a, **k: True)
    out_ = install._configure_channel(env.get("fake"), lambda k: "", dry_run=True)
    assert out_["ARIA_CHANNEL_MODE_FAKE"] == "control"


def test_real_prompt_toolkit_prompt_is_interrupted_and_text_kept(env, out):
    """End to end with a real PromptSession: half a line is typed, a phone
    message arrives from another thread, the prompt yields, the remote turn
    runs, and the half-typed text is back in the next prompt."""
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit import PromptSession
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from aria import main
    from aria.channels import control

    agent = FakeAgent()
    with create_pipe_input() as inp:
        session = PromptSession(input=inp, output=DummyOutput())
        waker = main._Waker(session)
        control.attach_repl(agent, waker.wake)
        control.enable("fake")
        got: list = []

        def phone() -> None:
            inp.send_text("half-typed")               # the user is mid-sentence…
            time.sleep(0.3)
            got.append(control.submit("fake", "u1", "from the phone"))   # …phone message
            time.sleep(0.2)
            inp.send_text(" and finished\r")          # user completes the line
            time.sleep(0.2)
            inp.send_text("/quit\r")

        threading.Thread(target=phone, daemon=True).start()
        lines: list[str] = []
        real_prompt = main._prompt

        def recording_prompt(sess, w=None):
            r = real_prompt(sess, w)
            lines.append(r)
            return r

        import unittest.mock as um
        with um.patch.object(main, "_prompt", recording_prompt):
            main._repl_loop(agent, session, waker)

    assert got == [["reply to from the phone"]]
    assert agent.turns[0][0] == "from the phone"
    assert lines[0] == main._WAKE
    assert "half-typed and finished" in lines        # typed text survived the interruption
