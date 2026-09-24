"""
Regression tests for channel/delivery fixes: @-mentions of paths containing
'@', the --notify stderr error path, Telegram Markdown→HTML code protection,
_split hard-splitting, telegram_notify.send chunking/fallback/broadcast, the
channel registry (Agent built outside the global lock, idle-eviction race),
Telegram _Progress RetryAfter + partial delivery, /clear → clear_session, and
the WhatsApp turn timeout. No network.
"""

import asyncio
import sys
import urllib.error
from types import SimpleNamespace

import pytest


# ── main.py ───────────────────────────────────────────────────────────────────

def test_mention_path_containing_at(tmp_path):
    from aria import main as M
    d = tmp_path / "user@corp.com"
    d.mkdir()
    f = d / "x.txt"
    f.write_text("payload-here")
    out = M._expand_mentions(f"look at @{f} now")
    assert "payload-here" in out
    # still ends at whitespace, and email addresses are not mentions
    assert [m.group(1) for m in M._MENTION_RE.finditer("@a/b c@d.com @e")] == ["a/b", "e"]


def test_notify_error_path_exits_cleanly(minimal_env, monkeypatch, capsys):
    """console.print(file=...) used to raise TypeError and mask the error."""
    from aria import main as M
    import aria.telegram_notify as tn

    class _Boom:
        name = "Aria"
        def __init__(self, *a, **k): pass
        def chat_collect(self, q): raise RuntimeError("llm down")
        def close(self): pass

    monkeypatch.setattr(M, "Agent", _Boom)
    monkeypatch.setattr(tn, "send", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["aria", "--notify", "hi"])
    with pytest.raises(SystemExit) as ei:
        M.main()
    assert ei.value.code == 1
    assert "llm down" in capsys.readouterr().err


# ── telegram_notify ───────────────────────────────────────────────────────────

def test_md_to_html_protects_code():
    from aria.telegram_notify import _md_to_html
    out = _md_to_html("call `__init__` and\n```py\nf(**kw, **x)\n```\n**bold**")
    assert "<code>__init__</code>" in out
    assert "f(**kw, **x)" in out
    assert "<b>bold</b>" in out
    assert out.count("<b>") == 1


def test_split_hard_splits_long_line():
    from aria.telegram_notify import _split
    chunks = _split("short\n" + "x" * 9000 + "\ntail", max_len=4000)
    assert all(len(c) <= 4000 for c in chunks)
    assert "".join(chunks) == "short\n" + "x" * 9000 + "\ntail"


def test_telegram_bot_uses_shared_split():
    pytest.importorskip("telegram")
    from aria import telegram_bot, telegram_notify
    assert telegram_bot._split is telegram_notify._split


@pytest.fixture
def tg_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_ALLOWED", "1,2")
    import aria.telegram_notify as tn
    monkeypatch.setattr(tn, "_record_feed", lambda text: None)
    return tn


def _http_400():
    import io
    return urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO(b"can't parse entities"))


def test_send_falls_back_to_plain_and_splits_markdown_first(tg_env, monkeypatch):
    tn = tg_env
    calls = []

    def fake_post(url, cid, text, html_mode):
        calls.append((cid, text, html_mode))
        if html_mode:
            raise _http_400()

    monkeypatch.setattr(tn, "_post_message", fake_post)
    tn.send("**" + "a" * 5000 + "**", chat_id=7)
    plain = [c for c in calls if not c[2]]
    assert len(plain) == 2                       # two markdown chunks, each resent plain
    assert "".join(t for _, t, _ in plain) == "**" + "a" * 5000 + "**"


def test_send_continues_after_failing_chat(tg_env, monkeypatch):
    tn = tg_env
    got = []

    def fake_post(url, cid, text, html_mode):
        if cid == 1:
            raise urllib.error.URLError("down")
        got.append(cid)

    monkeypatch.setattr(tn, "_post_message", fake_post)
    tn.send("hello")                             # broadcast: chat 1 fails, 2 still gets it
    assert got == [2]


def test_send_raises_when_nobody_received(tg_env, monkeypatch):
    tn = tg_env

    def fake_post(url, cid, text, html_mode):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(tn, "_post_message", fake_post)
    with pytest.raises(RuntimeError):
        tn.send("hello")


# ── channel.py ────────────────────────────────────────────────────────────────

class _Agent:
    count = 0
    lock_held_during_init = False

    def __init__(self, *a, **k):
        from aria import channel
        _Agent.count += 1
        if channel._registry_lock.locked():
            _Agent.lock_held_during_init = True
        self.closed = False

    def chat_yield(self, text, response_cb=None, activity_cb=None):
        assert not self.closed, "turn ran on a closed agent"
        return [f"reply:{text}"]

    def close(self):
        self.closed = True


@pytest.fixture
def chan(minimal_env, monkeypatch):
    from aria import channel
    _Agent.count = 0
    _Agent.lock_held_during_init = False
    monkeypatch.setattr(channel, "Agent", _Agent)
    monkeypatch.setattr(channel, "_IDLE_SECONDS", 10_000)
    with channel._registry_lock:
        channel._sessions.clear()
    yield channel
    channel.shutdown()


def test_agent_built_outside_registry_lock(chan):
    chan.handle("telegram", "u1", "hi")
    assert _Agent.count == 1
    assert not _Agent.lock_held_during_init


def test_evicted_session_is_replaced_not_reused(chan):
    sess = chan._get_or_create("telegram", "u1")
    sess._on_idle(sess._gen)                      # idle eviction wins the race
    assert sess.closed and sess.agent.closed
    assert sess.handle("late") is None            # a stale reference refuses the turn
    assert chan.handle("telegram", "u1", "hi") == ["reply:hi"]   # fresh session
    assert chan._sessions[("telegram", "u1")] is not sess


# ── telegram_bot ──────────────────────────────────────────────────────────────

class _FlakyBot:
    """Raises the scripted exception for the Nth send_message call."""
    def __init__(self, fail_on=None):
        self.fail_on = fail_on or {}
        self.calls = 0
        self.sent = []

    async def send_message(self, chat_id, text, parse_mode=None):
        self.calls += 1
        exc = self.fail_on.get(self.calls)
        if exc is not None:
            raise exc
        self.sent.append(text)
        return SimpleNamespace(message_id=self.calls)


def test_progress_retry_after(minimal_env, monkeypatch):
    pytest.importorskip("telegram")
    from telegram.error import RetryAfter
    from aria import telegram_bot as tb
    monkeypatch.setattr(tb, "_MAX_RETRY_AFTER", 0.0)
    bot = _FlakyBot({1: RetryAfter(1)})
    prog = tb._Progress(bot, "1", loop=None)
    assert asyncio.run(prog._send_response("hello")) is True
    assert bot.sent == ["hello"]


def test_progress_partial_delivery_retries_missing_chunks(minimal_env):
    pytest.importorskip("telegram")
    from aria import telegram_bot as tb
    text = "a" * 4000 + "\n" + "b" * 100
    # chunk 2: HTML (call 2) and plain (call 3) both fail; the retry succeeds.
    bot = _FlakyBot({2: RuntimeError("x"), 3: RuntimeError("y")})
    prog = tb._Progress(bot, "1", loop=None)
    assert asyncio.run(prog._send_response(text)) is True
    assert len(bot.sent) == 2 and bot.sent[1].strip() == "b" * 100
    assert prog.undelivered == []


def test_telegram_clear_uses_clear_session(minimal_env, monkeypatch):
    pytest.importorskip("telegram")
    from aria import telegram_bot as tb
    agent = SimpleNamespace(cleared=False)
    agent.clear_session = lambda: setattr(agent, "cleared", True)
    replies = []

    async def fake_reply(update, text, parse_html=True):
        replies.append(text)

    monkeypatch.setattr(tb, "_is_allowed", lambda u: True)
    monkeypatch.setattr(tb, "get_session", lambda c, u: agent)
    monkeypatch.setattr(tb, "_reply", fake_reply)
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=1))
    asyncio.run(tb.cmd_clear(update, None))
    assert agent.cleared and replies == ["History cleared."]


# ── whatsapp ──────────────────────────────────────────────────────────────────

def test_whatsapp_timeout_env(monkeypatch):
    from aria import whatsapp_bridge as wb
    monkeypatch.delenv("ARIA_WA_TIMEOUT", raising=False)
    assert wb._turn_timeout() == 600
    monkeypatch.setenv("ARIA_WA_TIMEOUT", "900")
    assert wb._turn_timeout() == 900
