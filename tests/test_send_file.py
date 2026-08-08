"""
Outbound file sending: the channel-context routing and — most importantly —
that send_file cannot be used to exfiltrate files the read allow-list forbids.
No network: telegram_notify.send_document is stubbed.
"""

import pytest

from aria import context
from aria.tools import send_file


@pytest.fixture
def sent(monkeypatch):
    """Capture send_document calls instead of hitting the Telegram API."""
    calls = []

    def fake_send_document(path, caption="", chat_id=None):
        calls.append({"path": str(path), "caption": caption, "chat_id": chat_id})
        from pathlib import Path
        return Path(path).name

    import aria.telegram_notify as tn
    monkeypatch.setattr(tn, "send_document", fake_send_document)
    return calls


@pytest.fixture(autouse=True)
def _clear_context():
    context.clear()
    yield
    context.clear()


def _workspace_file(ws, name="report.pdf", body=b"hello"):
    ws.mkdir(parents=True, exist_ok=True)
    p = ws / name
    p.write_bytes(body)
    return p


# ── Security: the allow-list must apply exactly as it does to reading ─────────

def test_blocked_path_is_refused(minimal_env, sent, monkeypatch):
    """A permanently blocked path can never be sent, authorised or not."""
    import os
    ssh = os.path.expanduser("~/.ssh/id_rsa")
    os.makedirs(os.path.dirname(ssh), exist_ok=True)
    with open(ssh, "w") as fh:
        fh.write("PRIVATE KEY")

    out = send_file.execute({"path": ssh})
    assert not sent, "blocked file must never reach Telegram"
    assert "PRIVATE KEY" not in out


def test_env_file_is_refused(minimal_env, sent):
    """~/.aria/.env holds the API keys — blocked, and must stay blocked here."""
    import os
    env = os.path.expanduser("~/.aria/.env")
    os.makedirs(os.path.dirname(env), exist_ok=True)
    with open(env, "w") as fh:
        fh.write("LLM_API_KEY=sk-secret")

    send_file.execute({"path": env})
    assert not sent


def test_outside_allowlist_asks_for_authorization(minimal_env, sent, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir(parents=True, exist_ok=True)
    target = outside / "notes.txt"
    target.write_text("data")

    out = send_file.execute({"path": str(target)})
    assert not sent, "must not send before the user authorises the directory"
    assert "authoriz" in out.lower() or "access" in out.lower()


def test_workspace_file_is_allowed(minimal_env, sent):
    p = _workspace_file(minimal_env)
    out = send_file.execute({"path": str(p)})
    assert len(sent) == 1
    assert sent[0]["path"] == str(p)
    assert "Sent report.pdf" in out


# ── Routing ───────────────────────────────────────────────────────────────────

def test_sends_to_the_active_chat(minimal_env, sent):
    p = _workspace_file(minimal_env)
    token = context.set_active("telegram", "4242")
    try:
        send_file.execute({"path": str(p)})
    finally:
        context.reset(token)
    assert sent[0]["chat_id"] is None      # resolved inside send_document
    assert context.current() is None


def test_refuses_to_cross_channels(minimal_env, sent):
    """A WhatsApp turn must not silently deliver over Telegram."""
    p = _workspace_file(minimal_env)
    token = context.set_active("whatsapp", "34600000000")
    try:
        out = send_file.execute({"path": str(p)})
    finally:
        context.reset(token)
    assert not sent
    assert "only supported on Telegram" in out


def test_current_chat_id_prefers_active_turn(minimal_env, monkeypatch):
    from aria import telegram_notify as tn
    monkeypatch.setenv("TELEGRAM_ALLOWED", "111,222")

    assert tn.current_chat_id() is None
    assert tn._targets(None) == [111, 222]        # no channel → broadcast

    token = context.set_active("telegram", "999")
    try:
        assert tn.current_chat_id() == 999
        assert tn._targets(None) == [999]         # active turn → just that chat
        assert tn._targets(555) == [555]          # explicit wins
    finally:
        context.reset(token)


def test_non_telegram_channel_has_no_chat_id(minimal_env):
    from aria import telegram_notify as tn
    token = context.set_active("whatsapp", "34600")
    try:
        assert tn.current_chat_id() is None
    finally:
        context.reset(token)


# ── Argument handling ─────────────────────────────────────────────────────────

def test_missing_path(minimal_env, sent):
    assert "No path provided" in send_file.execute({})


def test_missing_file(minimal_env, sent):
    out = send_file.execute({"path": str(minimal_env / "nope.pdf")})
    assert "Not found" in out
    assert not sent


def test_directory_is_refused_with_a_hint(minimal_env, sent):
    minimal_env.mkdir(parents=True, exist_ok=True)
    d = minimal_env / "folder"
    d.mkdir()
    out = send_file.execute({"path": str(d)})
    assert "directory" in out
    assert not sent


def test_caption_is_passed_through(minimal_env, sent):
    p = _workspace_file(minimal_env)
    send_file.execute({"path": str(p), "caption": "here is the report"})
    assert sent[0]["caption"] == "here is the report"


def test_upload_error_is_reported_not_raised(minimal_env, monkeypatch):
    p = _workspace_file(minimal_env)

    def boom(*a, **k):
        raise RuntimeError("Telegram API error 413: too big")

    import aria.telegram_notify as tn
    monkeypatch.setattr(tn, "send_document", boom)
    out = send_file.execute({"path": str(p)})
    assert out.startswith("[send_file error]")
    assert "413" in out


# ── The tool is registered and classified as a delivering tool ────────────────

def test_registered_and_classified_as_delivering(minimal_env):
    """Its description must trip the side-effect classifier, or the text answer
    accompanying the call would be dropped instead of delivered."""
    from aria.agent import Agent
    agent = Agent(terminal=False)
    assert "send_file" in {t["function"]["name"] for t in agent.tool_schemas}
    assert "send_file" in agent._classify_side_effect_tools()


def test_not_parallel_safe(minimal_env):
    """It mutates the outside world — must never run in a concurrent batch."""
    assert getattr(send_file, "PARALLEL_SAFE", False) is False
