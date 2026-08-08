"""
Inbound attachment handling: filename sanitisation, inbox layout, retention
pruning, and the Telegram media handler (bot fully mocked — no network).
"""

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from aria import attachments as att


# ── Filename sanitisation (untrusted input) ───────────────────────────────────

@pytest.mark.parametrize("raw", [
    "../../.ssh/authorized_keys",
    "/etc/passwd",
    "....//....//evil.sh",
    "..",
    ".",
])
def test_safe_name_strips_traversal(raw):
    name = att.safe_name(raw)
    assert "/" not in name and "\\" not in name
    assert name not in ("", ".", "..")
    assert not name.startswith(".")


def test_safe_name_keeps_ordinary_names():
    assert att.safe_name("Quarterly Report 2026.pdf") == "Quarterly Report 2026.pdf"


def test_safe_name_falls_back_when_empty():
    assert att.safe_name(None) == "attachment.bin"
    assert att.safe_name("   ") == "attachment.bin"


def test_safe_name_strips_control_characters():
    assert "\x00" not in att.safe_name("re\x00port\x07.pdf")


def test_safe_name_caps_length_but_keeps_extension():
    name = att.safe_name("A" * 400 + ".pdf")
    assert len(name) <= 120
    assert name.endswith(".pdf")


# ── Inbox layout ──────────────────────────────────────────────────────────────

def test_destination_is_inside_workspace(minimal_env):
    dest = att.destination("telegram", "12345", "report.pdf")
    assert str(dest).startswith(str(minimal_env))
    assert dest.parent.name == "12345"
    assert dest.parent.parent.name == "telegram"
    assert dest.name.endswith("_report.pdf")


def test_destination_never_escapes_inbox_with_hostile_name(minimal_env):
    dest = att.destination("telegram", "1", "../../../../etc/cron.d/pwn")
    inbox = minimal_env / "inbox"
    assert inbox.resolve() in dest.resolve().parents


def test_destination_avoids_collisions(minimal_env):
    a = att.destination("telegram", "1", "same.pdf")
    a.write_bytes(b"first")
    b = att.destination("telegram", "1", "same.pdf")
    assert a != b


def test_inbox_is_readable_by_file_access(minimal_env):
    """The whole point of using the workspace: no extra allow-list config."""
    from aria.tools import file_access as fa
    dest = att.destination("telegram", "1", "note.txt")
    dest.write_text("hello from telegram")
    att.finalize(dest)
    assert "hello from telegram" in fa.execute({"action": "read", "path": str(dest)})


def test_finalize_locks_permissions(minimal_env):
    dest = att.destination("telegram", "1", "secret.txt")
    dest.write_text("x")
    att.finalize(dest)
    assert dest.stat().st_mode & 0o777 == 0o600


# ── Retention ─────────────────────────────────────────────────────────────────

def test_prune_removes_aged_out_files(minimal_env, monkeypatch):
    monkeypatch.setenv("ARIA_INBOX_KEEP_DAYS", "7")
    old = att.destination("telegram", "1", "old.txt")
    old.write_text("stale")
    ancient = time.time() - 30 * 86400
    import os
    os.utime(old, (ancient, ancient))

    fresh = att.destination("telegram", "1", "fresh.txt")
    fresh.write_text("new")

    assert att.prune() >= 1
    assert not old.exists()
    assert fresh.exists()


def test_prune_enforces_size_quota_oldest_first(minimal_env, monkeypatch):
    import os
    monkeypatch.setenv("ARIA_INBOX_KEEP_DAYS", "0")     # age pruning off
    monkeypatch.setenv("ARIA_INBOX_MAX_MB", "1")

    paths = []
    for i in range(4):
        p = att.destination("telegram", "1", f"blob{i}.bin")
        p.write_bytes(b"x" * (400 * 1024))
        os.utime(p, (time.time() + i, time.time() + i))   # ascending mtime
        paths.append(p)

    att.prune()
    assert not paths[0].exists(), "oldest should go first"
    assert paths[-1].exists(),    "newest should survive"


def test_prune_is_safe_on_empty_inbox(minimal_env):
    assert att.prune() == 0


# ── Agent-facing description ──────────────────────────────────────────────────

def test_describe_includes_path_and_caption(minimal_env):
    dest = att.destination("telegram", "1", "report.pdf")
    text = att.describe(dest, channel="telegram", kind="document",
                        original_name="report.pdf", mime="application/pdf",
                        size=1234, caption="summarise this please")
    assert str(dest) in text
    assert "summarise this please" in text
    assert "application/pdf" in text


def test_describe_warns_agent_it_cannot_see_images(minimal_env):
    dest = att.destination("telegram", "1", "pic.jpg")
    text = att.describe(dest, channel="telegram", kind="photo",
                        original_name="pic.jpg", mime="image/jpeg", size=10)
    assert "cannot see image" in text


def test_describe_warns_agent_it_cannot_transcribe_voice(minimal_env):
    dest = att.destination("telegram", "1", "v.ogg")
    text = att.describe(dest, channel="telegram", kind="voice",
                        original_name="v.ogg", mime="audio/ogg", size=10)
    assert "transcription is not enabled" in text


def test_describe_handles_missing_caption(minimal_env):
    dest = att.destination("telegram", "1", "x.pdf")
    text = att.describe(dest, channel="telegram", kind="document")
    assert "No message accompanied the file" in text


# ── Telegram media handler ────────────────────────────────────────────────────

pytest.importorskip("telegram")


class _FakeFile:
    def __init__(self, payload=b"%PDF-1.4 fake"):
        self.payload = payload

    async def download_to_drive(self, custom_path):
        Path(custom_path).write_bytes(self.payload)


class _FakeBot:
    def __init__(self):
        self.requested = []

    async def get_file(self, file_id):
        self.requested.append(file_id)
        return _FakeFile()

    async def send_chat_action(self, *a, **k):
        pass


def _make_update(monkeypatch, **attachment):
    """Build a minimal Update whose message carries one attachment."""
    sent: list[str] = []

    async def reply_text(text, parse_mode=None):
        sent.append(text)

    msg = SimpleNamespace(
        document=None, photo=None, voice=None, audio=None,
        video=None, video_note=None, animation=None,
        caption="", reply_to_message=None, text=None,
        reply_text=reply_text,
    )
    for k, v in attachment.items():
        setattr(msg, k, v)
    update = SimpleNamespace(message=msg, effective_chat=SimpleNamespace(id=42))
    return update, sent


def test_on_media_downloads_and_runs_turn(minimal_env, monkeypatch):
    from aria import telegram_bot as tb

    monkeypatch.setenv("TELEGRAM_ALLOWED", "42")
    captured = {}

    async def fake_run_turn(update, context, chat_id, user_text):
        captured["chat_id"]   = chat_id
        captured["user_text"] = user_text

    monkeypatch.setattr(tb, "_run_turn", fake_run_turn)

    doc = SimpleNamespace(file_id="F1", file_name="report.pdf",
                          mime_type="application/pdf", file_size=1024)
    update, _ = _make_update(monkeypatch, document=doc, caption="read this")
    ctx = SimpleNamespace(bot=_FakeBot())

    asyncio.run(tb.on_media(update, ctx))

    assert captured["chat_id"] == "42"
    assert "report.pdf" in captured["user_text"]
    assert "read this"  in captured["user_text"]

    saved = list((minimal_env / "inbox" / "telegram" / "42").iterdir())
    assert len(saved) == 1
    assert saved[0].read_bytes().startswith(b"%PDF")


def test_on_media_rejects_oversized_file(minimal_env, monkeypatch):
    from aria import telegram_bot as tb
    monkeypatch.setenv("TELEGRAM_ALLOWED", "42")

    ran = []
    async def fake_run_turn(*a, **k):
        ran.append(1)
    monkeypatch.setattr(tb, "_run_turn", fake_run_turn)

    doc = SimpleNamespace(file_id="F1", file_name="huge.zip",
                          mime_type="application/zip", file_size=50 * 1024 * 1024)
    update, sent = _make_update(monkeypatch, document=doc)
    asyncio.run(tb.on_media(update, SimpleNamespace(bot=_FakeBot())))

    assert not ran, "must not spend a turn on a file it cannot fetch"
    assert any("20 MB" in s for s in sent)


def test_on_media_rejects_unauthorised_chat(minimal_env, monkeypatch):
    from aria import telegram_bot as tb
    monkeypatch.setenv("TELEGRAM_ALLOWED", "999")

    doc = SimpleNamespace(file_id="F1", file_name="x.pdf",
                          mime_type="application/pdf", file_size=10)
    update, sent = _make_update(monkeypatch, document=doc)
    asyncio.run(tb.on_media(update, SimpleNamespace(bot=_FakeBot())))
    assert sent == ["Unauthorised."]


def test_on_media_picks_largest_photo(minimal_env, monkeypatch):
    from aria import telegram_bot as tb
    monkeypatch.setenv("TELEGRAM_ALLOWED", "42")

    captured = {}
    async def fake_run_turn(update, context, chat_id, user_text):
        captured["user_text"] = user_text
    monkeypatch.setattr(tb, "_run_turn", fake_run_turn)

    photos = [
        SimpleNamespace(file_id="small", file_unique_id="s", file_size=100),
        SimpleNamespace(file_id="large", file_unique_id="l", file_size=9000),
    ]
    update, _ = _make_update(monkeypatch, photo=photos)
    bot = _FakeBot()
    asyncio.run(tb.on_media(update, SimpleNamespace(bot=bot)))

    assert bot.requested == ["large"]
    assert "cannot see image" in captured["user_text"]


def test_on_media_reports_download_failure(minimal_env, monkeypatch):
    from aria import telegram_bot as tb
    monkeypatch.setenv("TELEGRAM_ALLOWED", "42")

    class _BrokenBot(_FakeBot):
        async def get_file(self, file_id):
            raise RuntimeError("network down")

    ran = []
    async def fake_run_turn(*a, **k):
        ran.append(1)
    monkeypatch.setattr(tb, "_run_turn", fake_run_turn)

    doc = SimpleNamespace(file_id="F1", file_name="x.pdf",
                          mime_type="application/pdf", file_size=10)
    update, sent = _make_update(monkeypatch, document=doc)
    asyncio.run(tb.on_media(update, SimpleNamespace(bot=_BrokenBot())))

    assert not ran
    # _md_to_html escapes apostrophes to &#x27;, so match on a clean substring.
    assert any("download that file" in s for s in sent)
    assert any("network down" in s for s in sent)


def test_unknown_attachment_type_is_reported(minimal_env, monkeypatch):
    from aria import telegram_bot as tb
    monkeypatch.setenv("TELEGRAM_ALLOWED", "42")
    update, sent = _make_update(monkeypatch)      # no attachment set
    asyncio.run(tb.on_media(update, SimpleNamespace(bot=_FakeBot())))
    assert any("handle that kind of attachment" in s for s in sent)


# ── End-to-end: a PDF arrives and the agent reads it ──────────────────────────

def test_pdf_attachment_round_trip(minimal_env, native_client, monkeypatch):
    """A PDF sent over Telegram lands in the inbox, and the agent can read it
    with file_access using only the path from describe() — no extra config."""
    from aria.tools.file_access import _is_pdf
    from test_pdf_read import _make_pdf      # sibling module; tests/ is on sys.path

    dest = att.destination("telegram", "42", "quarterly.pdf")
    dest.write_bytes(_make_pdf(["Revenue was up 12 percent"]))
    att.finalize(dest)
    assert _is_pdf(dest)

    prompt = att.describe(dest, channel="telegram", kind="document",
                          original_name="quarterly.pdf",
                          mime="application/pdf", size=dest.stat().st_size,
                          caption="what does this say?")

    from aria.agent import Agent
    agent = Agent(window_key="telegram:42", terminal=False)
    agent.client = native_client(
        {"tool_calls": [("file_access", {"action": "read", "path": str(dest)})]},
        "It reports revenue up 12 percent.",
    )
    replies = agent.chat_yield(prompt)

    assert any("12 percent" in r for r in replies)
    # The tool result really carried the extracted text, not binary mojibake.
    tool_msgs = [m for m in agent.history if m.get("role") == "tool"]
    assert any("Revenue was up 12 percent" in str(m.get("content")) for m in tool_msgs)


def test_inbox_directories_are_private(minimal_env):
    """User-sent files can be sensitive; every inbox level is 700."""
    dest = att.destination("telegram", "42", "x.txt")
    root = minimal_env / "inbox"
    for d in (root, root / "telegram", root / "telegram" / "42"):
        assert d.stat().st_mode & 0o777 == 0o700, d
