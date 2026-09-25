"""Roadmap 4.3/4.5/4.7/4.8/4.9 (core + Telegram side): /stop, shared slash
commands, host interception of commands and approval answers, Telegram's
concurrent updates with per-chat ordering, approval buttons, and approvals
reaching the phone from a remote-control turn."""

from __future__ import annotations

import asyncio
import sys
import textwrap
import threading
import time
from types import SimpleNamespace

import pytest

FAKE = textwrap.dedent('''
    from aria.channels import ChannelPlugin
    ASKED = []
    SENT = []
    class Fake(ChannelPlugin):
        name = "fake"
        supports_attached = True
        def start(self, stop): stop.wait()
        def send(self, text, to=None): SENT.append((text, to))
        def send_approval(self, code, summary, to=None, expires_min=5):
            ASKED.append((code, summary, to))
    PLUGIN = Fake()
''')


@pytest.fixture
def env(minimal_env, tmp_path, monkeypatch):
    d = tmp_path / "channels"
    d.mkdir()
    (d / "fake.py").write_text(FAKE)
    monkeypatch.setenv("ARIA_CHANNELS_DIR", str(d))
    monkeypatch.setenv("ARIA_CHANNELS", "fake")
    import aria.channels as ch
    from aria.channels import control
    ch.reset_cache()
    ch.get("fake")
    yield ch
    control.detach_repl()
    ch.reset_cache()


def _fake():
    return sys.modules["_aria_user_channel_fake"]


def _wait(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


# ── 4.8 /stop ─────────────────────────────────────────────────────────────────

def test_stop_ends_the_turn_after_the_current_step(minimal_env, native_client, monkeypatch):
    from aria.agent import Agent
    a = Agent(window_key="t", terminal=False)
    # the model keeps calling tools; the first tool call triggers /stop
    a.client = native_client(
        {"content": None, "tool_calls": [("shell_run", {"command": "a"}),
                                         ("shell_run", {"command": "b"})]},
        {"content": None, "tool_calls": [("shell_run", {"command": "c"})]},
    )
    ran = []

    def tool(name, args):
        ran.append(args["command"])
        assert a.request_stop()                 # user sends /stop mid-turn
        return "ok"
    monkeypatch.setattr(a, "_execute_tool", tool)
    out = a.chat_yield("go")
    assert ran == ["a"]                          # b skipped, c never requested
    assert out[-1] == "⏹ Stopped."
    tool_msgs = [m for m in a.history if m.get("role") == "tool"]
    assert len(tool_msgs) == 2 and "stopped" in tool_msgs[1]["content"]
    assert not a.request_stop()                  # idle again


# ── 4.5 shared commands ───────────────────────────────────────────────────────

class _Agent:
    name = "Aria"

    def __init__(self):
        self.stopped = self.cleared = False
        self.busy = True
        self.tool_schemas = [{"function": {"name": "notify", "description": "Send a push"}}]
        self.ws = SimpleNamespace(load_memory=lambda: "", append_memory=lambda n: None)

    def request_stop(self):
        self.stopped = True
        return self.busy

    def clear_session(self):
        self.cleared = True

    def list_profiles(self):
        return [{"name": "default", "model": "m", "active": True}]

    def switch_profile(self, name):
        return f"Switched to {name}"


def test_commands_parse_and_run():
    from aria.channels import commands
    assert commands.parse("/stop@MyBot now") == ("stop", "now")
    assert commands.parse("hello") is None and commands.parse("/") is None
    a = _Agent()
    assert "Stopping" in commands.run(a, "/stop") and a.stopped
    a.busy = False
    assert commands.run(a, "/stop") == "Nothing is running."
    assert commands.run(a, "/clear") == "History cleared." and a.cleared
    assert commands.run(a, "/memory") == "_Nothing stored yet._"
    assert "**notify**" in commands.run(a, "/tools")
    assert commands.run(a, "/models") == "`default` m ✓"
    assert commands.run(a, "/model fast") == "Switched to fast"
    assert commands.run(a, "/save") == "Usage: /save <note>"
    assert "/stop" in commands.run(a, "/help")
    assert commands.run(a, "/unknowncmd") is None


def test_host_intercepts_commands_and_approval_answers(env, monkeypatch):
    from aria import channel as sessions
    from aria.channels import host
    turns = []
    monkeypatch.setattr(sessions, "handle", lambda *a, **k: turns.append(a) or ["turn"])
    a = _Agent()
    monkeypatch.setattr(host, "get_agent", lambda c, u: a)
    assert host.handle_message("fake", "u", "/stop") == ["⏹ Stopping after the current step…"]
    # "yes 1234" with nothing pending is an ordinary message for the agent
    assert host.handle_message("fake", "u", "yes 1234") == ["turn"]
    assert host.handle_message("fake", "u", "/notacommand x") == ["turn"]
    assert host.handle_message("fake", "u", "hello") == ["turn"]
    assert len(turns) == 3


# ── 4.9 approval core ─────────────────────────────────────────────────────────

def test_approval_round_trip_on_the_active_channel(env):
    from aria import approval, context
    result = []

    def turn():                       # the tool runs on the turn's thread,
        context.set_active("fake", "u1")   # where the channel context is set
        result.append(approval.request("delete X"))

    t = threading.Thread(target=turn, daemon=True)
    t.start()
    assert _wait(lambda: _fake().ASKED)
    code, summary, to = _fake().ASKED[-1]
    assert (summary, to) == ("delete X", "u1")
    assert approval.try_answer_text("fake", "someone-else", f"yes {code}") is None
    assert approval.try_answer_text("fake", "u1", f"yes {code}") == "✅ Approved."
    t.join(3)
    assert result == [(True, "approved")]


def test_scheduled_task_asks_the_push_channel_and_can_be_denied(env, monkeypatch):
    from aria import approval
    monkeypatch.setenv("ARIA_TASK_ID", "t1")
    monkeypatch.setenv("ARIA_APPROVAL_TASKS", "on")
    monkeypatch.setenv("ARIA_NOTIFY_CHANNEL", "fake")
    result = []
    t = threading.Thread(target=lambda: result.append(approval.check("delete", "delete X")), daemon=True)
    t.start()
    assert _wait(lambda: _fake().ASKED)
    code, _, to = _fake().ASKED[-1]
    assert to is None                                   # broadcast to the push channel
    approval.answer(code, False, "fake", "anyone")
    t.join(3)
    assert result[0].startswith("[approval] Not done") and "denied" in result[0]


def test_approval_expires_and_can_be_disabled(env, monkeypatch):
    from aria import approval, context
    monkeypatch.setattr(approval, "_timeout", lambda: 0.3)
    token = context.set_active("fake", "u1")
    try:
        ok, why = approval.request("x")
        assert not ok and "not approved within" in why
        monkeypatch.setenv("ARIA_APPROVALS", "off")
        assert approval.request("x") == (False, "approvals are disabled (ARIA_APPROVALS=off)")
    finally:
        context.reset(token)
    # terminal (no channel, no task): check() never asks
    monkeypatch.delenv("ARIA_APPROVALS")
    assert approval.check("delete", "x") is None
    # not in the required list → never asks, even unattended
    monkeypatch.setenv("ARIA_TASK_ID", "t")
    assert approval.check("gmail_send", "x") is None


def test_remote_control_turn_asks_on_the_phone(env, monkeypatch):
    """4.3: a phone-driven REPL turn that hits a risky action asks the phone
    (the turn runs with the channel context set), and the phone's answer —
    which bypasses the queue — unblocks it."""
    import io
    from rich.console import Console
    from aria import approval, main
    from aria.channels import control, host
    monkeypatch.setattr(main, "console", Console(file=io.StringIO()))

    class Agent:
        name = "Aria"
        def chat_mirrored(self, text, response_cb=None, activity_cb=None):
            refusal = approval.check("delete", "delete build/")
            return ["deleted" if refusal is None else refusal]

    agent = Agent()
    control.attach_repl(agent, None)
    control.enable("fake")
    replies = []
    phone = threading.Thread(target=lambda: replies.append(
        host.handle_message("fake", "u9", "clean up")), daemon=True)
    phone.start()
    assert _wait(control.pending)
    repl = threading.Thread(target=lambda: main._run_remote_turn(agent, control.take()),
                            daemon=True)
    repl.start()
    assert _wait(lambda: _fake().ASKED)
    code, _, to = _fake().ASKED[-1]
    assert to == "u9"
    assert host.handle_message("fake", "u9", f"yes {code}") == ["✅ Approved."]
    repl.join(3)
    phone.join(3)
    assert replies == [["deleted"]]


# ── 4.7 / 4.9 Telegram ────────────────────────────────────────────────────────

def _tg_update(text, chat_id=42, sent=None):
    sent = [] if sent is None else sent

    async def reply_text(t, parse_mode=None):
        sent.append(t)
    return SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id),
                           message=SimpleNamespace(text=text, reply_text=reply_text,
                                                   reply_to_message=None)), sent


def test_telegram_stop_is_not_blocked_by_a_running_turn(minimal_env, monkeypatch):
    """/stop must answer while the chat lock is held by a running turn."""
    pytest.importorskip("telegram")
    from aria.channels.telegram import bot
    monkeypatch.setenv("TELEGRAM_ALLOWED", "42")
    replies = []

    async def fake_reply(update, text, parse_html=True):
        replies.append(text)
    monkeypatch.setattr(bot, "_reply", fake_reply)
    monkeypatch.setattr(bot, "run_command", lambda ch, cid, text: f"ran {text}")

    async def scenario():
        lock = bot._chat_lock("42")
        await lock.acquire()                        # a turn is running
        try:
            u, _ = _tg_update("/stop")
            await asyncio.wait_for(bot.on_command(u, None), 2)
            assert replies == ["ran /stop"]
            u, _ = _tg_update("/clear")             # history-changing: waits
            pending = asyncio.ensure_future(bot.on_command(u, None))
            await asyncio.sleep(0.1)
            assert not pending.done()
        finally:
            lock.release()
        await asyncio.wait_for(pending, 2)
        assert replies[-1] == "ran /clear"
        bot._chat_locks.clear()
    asyncio.run(scenario())


def test_telegram_turns_in_one_chat_keep_order(minimal_env, monkeypatch):
    pytest.importorskip("telegram")
    from aria.channels.telegram import bot
    monkeypatch.setenv("TELEGRAM_ALLOWED", "42")
    order = []

    def fake_handle(ch, chat_id, text, **kw):
        time.sleep(0.2 if text == "first" else 0.0)
        order.append(text)
        return [text]

    class P:
        sent, undelivered = 1, []
        def __init__(self, *a): pass
        def start(self): pass
        async def stop(self): pass
        response = activity = None
    monkeypatch.setattr(bot, "handle", fake_handle)
    monkeypatch.setattr(bot, "_Progress", P)

    async def scenario():
        u1, _ = _tg_update("first")
        u2, _ = _tg_update("second")
        await asyncio.gather(bot.on_message(u1, SimpleNamespace(bot=None)),
                             bot.on_message(u2, SimpleNamespace(bot=None)))
        bot._chat_locks.clear()
    asyncio.run(scenario())
    assert order == ["first", "second"]


def test_telegram_typed_approval_answer_skips_the_lock(minimal_env, monkeypatch):
    pytest.importorskip("telegram")
    from aria.channels.telegram import bot
    monkeypatch.setenv("TELEGRAM_ALLOWED", "42")
    monkeypatch.setattr(bot, "answer_approval", lambda ch, cid, t: "✅ Approved.")
    replies = []

    async def fake_reply(update, text, parse_html=True):
        replies.append(text)
    monkeypatch.setattr(bot, "_reply", fake_reply)

    async def scenario():
        await bot._chat_lock("42").acquire()
        u, _ = _tg_update("yes 1234")
        await asyncio.wait_for(bot.on_message(u, None), 2)
        bot._chat_locks.clear()
    asyncio.run(scenario())
    assert replies == ["✅ Approved."]


def test_telegram_approval_buttons(minimal_env, monkeypatch):
    pytest.importorskip("telegram")
    from aria.channels.telegram import bot, notify
    monkeypatch.setenv("TELEGRAM_ALLOWED", "42")
    monkeypatch.setenv("TELEGRAM_TOKEN", "t")
    posted = []
    monkeypatch.setattr(notify, "_post_message",
                        lambda url, cid, text, html_mode, reply_markup=None:
                        posted.append((cid, text, reply_markup)))
    notify.send_approval("1234", "delete <b>X</b>", chat_id=42)
    cid, text, markup = posted[0]
    buttons = markup["inline_keyboard"][0]
    assert cid == 42 and "delete &lt;b&gt;X&lt;/b&gt;" in text and "yes 1234" in text
    assert [b["callback_data"] for b in buttons] == ["aria-approve:1234:y", "aria-approve:1234:n"]

    answered = []
    from aria import approval
    monkeypatch.setattr(approval, "answer",
                        lambda code, ok, ch, uid: answered.append((code, ok, ch, uid)) or "✅ Approved.")
    toasts, edits = [], []

    async def q_answer(t=None):
        toasts.append(t)

    async def q_edit(t, parse_mode=None):
        edits.append(t)
    query = SimpleNamespace(data="aria-approve:1234:y", answer=q_answer,
                            edit_message_text=q_edit,
                            message=SimpleNamespace(text_html="🔐 Approval needed"))
    update = SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=42))
    asyncio.run(bot.on_approval_button(update, None))
    assert answered == [("1234", True, "telegram", "42")]
    assert toasts == ["✅ Approved."] and "✅ Approved." in edits[0]


def test_telegram_app_is_concurrent(minimal_env):
    pytest.importorskip("telegram")
    from aria.channels.telegram import bot
    app = bot.build_app("123:abc")
    assert app.concurrent_updates > 1


def test_stop_cancels_a_pending_approval(env):
    """/stop during an approval wait ends it at once (no 5-minute hang)."""
    from aria import approval, context
    stop = threading.Event()
    result = []

    def turn():
        context.set_active("fake", "u1")
        approval.bind_cancel(stop)
        result.append(approval.request("delete X"))
    t = threading.Thread(target=turn, daemon=True)
    t.start()
    assert _wait(lambda: _fake().ASKED)
    stop.set()
    t.join(3)
    assert result == [(False, "stopped by the user")]


def test_answer_for_a_dead_requester_is_refused(env, monkeypatch):
    from aria import approval
    approval._write(approval._dir() / "4321.json",
                    {"code": "4321", "channel": "fake", "to": None, "summary": "x",
                     "status": "pending", "pid": 2 ** 22 + 12345})
    monkeypatch.setattr(approval, "_alive", lambda pid: False)
    assert "no longer waiting" in approval.answer("4321", True, "fake", "u")


def test_single_message_channels_fail_fast(env, monkeypatch):
    from aria import approval, context
    monkeypatch.setattr(type(env.get("fake")), "answers_approvals", False)
    token = context.set_active("fake", "u1")
    try:
        ok, why = approval.request("x")
    finally:
        context.reset(token)
    assert not ok and "can't receive an answer" in why and not _fake().ASKED


def test_clear_refused_while_a_reply_runs():
    from aria.channels import commands
    a = _Agent()
    a._busy = True
    assert "still running" in commands.run(a, "/clear") and not a.cleared
    assert "still running" in commands.run(a, "/model fast")
    assert commands.run(a, "/models") == "`default` m ✓"      # read-only: fine


def test_parallel_tool_calls_see_the_channel_context(minimal_env, native_client, monkeypatch):
    """Batched PARALLEL_SAFE calls run in pool threads; they must still see
    the turn's channel (approvals + delivery routing)."""
    from aria import context
    from aria.agent import Agent
    a = Agent(window_key="t", terminal=False)
    a._parallel_safe = {"drive"}
    a.client = native_client(
        {"content": None, "tool_calls": [("drive", {"action": "list"}),
                                         ("drive", {"action": "delete", "file_id": "1"})]},
        "done")
    seen = []
    monkeypatch.setattr(a, "_execute_tool",
                        lambda n, args: seen.append(context.current()) or "ok")
    token = context.set_active("telegram", "42")
    try:
        a.chat_yield("go")
    finally:
        context.reset(token)
    assert len(seen) == 2 and all(c is not None and c.channel == "telegram" for c in seen)
