"""
Channel-routed notifications: the notify tool must deliver on the channel the
active turn belongs to. A Telegram turn replies over Telegram (in that chat —
resolved inside telegram_notify), no-channel turns (REPL/supervisor/cron)
broadcast over Telegram, and a WhatsApp turn is delivered over WhatsApp
(whatsapp_notify.send) — never silently rerouted to Telegram. No network:
telegram_notify.send and whatsapp_notify.send are stubbed.
"""

import json

import pytest

from aria import context
from aria.tools import notify


@pytest.fixture
def sent(monkeypatch):
    """Capture telegram_notify.send calls instead of hitting the API."""
    calls = []

    def fake_send(text, chat_id=None):
        calls.append({"text": text, "chat_id": chat_id})

    import aria.telegram_notify as tn
    monkeypatch.setattr(tn, "send", fake_send)
    return calls


@pytest.fixture
def wa_sent(monkeypatch):
    """Capture whatsapp_notify.send calls instead of hitting the bridge."""
    calls = []

    def fake_send(text, to=None):
        calls.append({"text": text, "to": to})

    import aria.whatsapp_notify as wn
    monkeypatch.setattr(wn, "send", fake_send)
    return calls


@pytest.fixture(autouse=True)
def _clear_context():
    context.clear()
    yield
    context.clear()


# ── Routing ───────────────────────────────────────────────────────────────────

def test_no_channel_uses_telegram_broadcast_path(minimal_env, sent):
    """REPL/supervisor/cron: no active channel → the Telegram send path
    (which itself broadcasts to TELEGRAM_ALLOWED)."""
    out = notify.execute({"message": "task done"})
    assert len(sent) == 1
    assert sent[0]["text"] == "task done"
    assert "[notify] Message sent." == out


def test_telegram_turn_uses_telegram(minimal_env, sent):
    token = context.set_active("telegram", "4242")
    try:
        out = notify.execute({"message": "hi"})
    finally:
        context.reset(token)
    assert len(sent) == 1
    assert out == "[notify] Message sent."


def test_telegram_turn_targets_the_active_chat(minimal_env, monkeypatch):
    """End-to-end target resolution: with a Telegram turn active, the real
    _targets() picks that chat, not the broadcast list."""
    from aria import telegram_notify as tn
    monkeypatch.setenv("TELEGRAM_ALLOWED", "111,222")
    token = context.set_active("telegram", "999")
    try:
        assert tn._targets(None) == [999]
    finally:
        context.reset(token)


def test_whatsapp_turn_routes_to_whatsapp(minimal_env, sent, wa_sent):
    """A WhatsApp-originated turn is delivered over WhatsApp and must never
    reach the Telegram send path."""
    token = context.set_active("whatsapp", "34600000000")
    try:
        out = notify.execute({"message": "secret for whatsapp user"})
    finally:
        context.reset(token)
    assert not sent, "WhatsApp turn must never reach the Telegram send path"
    assert len(wa_sent) == 1
    assert wa_sent[0]["text"] == "secret for whatsapp user"
    assert out == "[notify] Message sent."


def test_whatsapp_send_failure_is_reported(minimal_env, sent, monkeypatch):
    """A RuntimeError from the WhatsApp sender surfaces as a [notify error],
    and never falls back to Telegram."""
    def boom(text, to=None):
        raise RuntimeError("WhatsApp bridge unreachable")

    import aria.whatsapp_notify as wn
    monkeypatch.setattr(wn, "send", boom)
    token = context.set_active("whatsapp", "34600000000")
    try:
        out = notify.execute({"message": "x"})
    finally:
        context.reset(token)
    assert not sent
    assert out.startswith("[notify error]")
    assert "unreachable" in out


def test_unknown_channel_is_not_delivered_to_telegram(minimal_env, sent):
    token = context.set_active("matrix", "@user:example.org")
    try:
        out = notify.execute({"message": "hello"})
    finally:
        context.reset(token)
    assert not sent
    assert out.startswith("[notify error]")
    assert "matrix" in out


def test_route_helper(minimal_env):
    assert notify._route() == "telegram"          # no channel → telegram path
    token = context.set_active("whatsapp", "346")
    try:
        assert notify._route() == "whatsapp"
    finally:
        context.reset(token)


# ── Argument / error handling ─────────────────────────────────────────────────

def test_empty_message(minimal_env, sent):
    assert "No message provided" in notify.execute({})
    assert not sent


def test_send_failure_is_reported_not_raised(minimal_env, monkeypatch):
    def boom(text, chat_id=None):
        raise RuntimeError("TELEGRAM_TOKEN not set. Add it to ~/.aria/.env")

    import aria.telegram_notify as tn
    monkeypatch.setattr(tn, "send", boom)
    out = notify.execute({"message": "x"})
    assert out.startswith("[notify error]")
    assert "TELEGRAM_TOKEN" in out


# ── Classification ────────────────────────────────────────────────────────────

def test_still_classified_as_delivering(minimal_env):
    """Its description must keep tripping the side-effect classifier, or the
    text answer accompanying a notify call would be dropped."""
    from aria.agent import Agent
    agent = Agent(terminal=False)
    assert "notify" in {t["function"]["name"] for t in agent.tool_schemas}
    assert "notify" in agent._classify_side_effect_tools()


# ── whatsapp_notify target resolution ───────────────────────────────────────────

def test_wa_targets_explicit_to_wins(minimal_env, monkeypatch):
    from aria import whatsapp_notify as wn
    monkeypatch.setenv("WHATSAPP_ALLOWED", "111,222")
    token = context.set_active("whatsapp", "999")
    try:
        assert wn._targets("34600111222") == ["34600111222"]
    finally:
        context.reset(token)


def test_wa_targets_active_context(minimal_env, monkeypatch):
    from aria import whatsapp_notify as wn
    monkeypatch.setenv("WHATSAPP_ALLOWED", "111,222")
    token = context.set_active("whatsapp", "34600000000")
    try:
        assert wn._targets(None) == ["34600000000"]
    finally:
        context.reset(token)


def test_wa_targets_broadcast_no_channel(minimal_env, monkeypatch):
    from aria import whatsapp_notify as wn
    monkeypatch.setenv("WHATSAPP_ALLOWED", "111,222")
    assert wn._targets(None) == ["111", "222"]


def test_wa_targets_telegram_turn_does_not_leak(minimal_env, monkeypatch):
    """A Telegram turn is not a WhatsApp target → falls back to broadcast."""
    from aria import whatsapp_notify as wn
    monkeypatch.setenv("WHATSAPP_ALLOWED", "111,222")
    token = context.set_active("telegram", "999")
    try:
        assert wn._targets(None) == ["111", "222"]
    finally:
        context.reset(token)


def test_wa_missing_secret_raises(minimal_env, monkeypatch):
    from aria import whatsapp_notify as wn
    monkeypatch.delenv("ARIA_WA_SECRET", raising=False)
    monkeypatch.setenv("WHATSAPP_ALLOWED", "111")
    with pytest.raises(RuntimeError, match="ARIA_WA_SECRET"):
        wn.send("hi", to="111")


def test_wa_missing_allowed_raises(minimal_env, monkeypatch):
    from aria import whatsapp_notify as wn
    monkeypatch.setenv("ARIA_WA_SECRET", "s3cret")
    monkeypatch.delenv("WHATSAPP_ALLOWED", raising=False)
    with pytest.raises(RuntimeError, match="WHATSAPP_ALLOWED"):
        wn.send("hi")  # no explicit target, no context → needs the allow-list


def test_wa_send_posts_to_bridge(minimal_env, monkeypatch):
    """send() POSTs {to, text} with the X-Aria-Secret header to the push port,
    using stdlib urllib (no real network)."""
    from aria import whatsapp_notify as wn
    monkeypatch.setenv("ARIA_WA_SECRET", "s3cret")
    monkeypatch.setenv("ARIA_WA_PUSH_PORT", "7599")

    captured = {}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b""

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["secret"] = req.headers.get("X-aria-secret")
        captured["body"] = json.loads(req.data.decode())
        return _Resp()

    monkeypatch.setattr(wn.urllib.request, "urlopen", fake_urlopen)
    wn.send("hello there", to="34600123456")

    assert captured["url"] == "http://127.0.0.1:7599/send"
    assert captured["secret"] == "s3cret"
    assert captured["body"] == {"to": "34600123456", "text": "hello there"}


def test_wa_send_http_error_raises(minimal_env, monkeypatch):
    from aria import whatsapp_notify as wn
    import urllib.error
    import io
    monkeypatch.setenv("ARIA_WA_SECRET", "s3cret")

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 503, "unavailable", {},
            io.BytesIO(b'{"error": "WhatsApp client not ready"}'))

    monkeypatch.setattr(wn.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="503"):
        wn.send("x", to="111")


# ── whatsapp_bridge colon-in-reply fix ──────────────────────────────────────────

def test_strip_prefix_removes_only_agent_name(minimal_env, monkeypatch):
    from aria import whatsapp_bridge as wb
    monkeypatch.setenv("AGENT_NAME", "Aria")
    assert wb._strip_agent_prefix("Aria: done") == "done"


def test_strip_prefix_keeps_colon_in_body(minimal_env, monkeypatch):
    """The bug: 'Status: done' must NOT become 'done'."""
    from aria import whatsapp_bridge as wb
    monkeypatch.setenv("AGENT_NAME", "Aria")
    assert wb._strip_agent_prefix("Status: done") == "Status: done"


def test_strip_prefix_respects_agent_name(minimal_env, monkeypatch):
    from aria import whatsapp_bridge as wb
    monkeypatch.setenv("AGENT_NAME", "Jarvis")
    assert wb._strip_agent_prefix("Jarvis: hi") == "hi"
    assert wb._strip_agent_prefix("Aria: hi") == "Aria: hi"
