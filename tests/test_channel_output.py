"""Replies fitted to where they're read: the per-turn surface note, the
conversions (tables, headings, dialects, links), long replies as a summary
plus a file, and delivery through the host, pushes and remote control."""

from __future__ import annotations

import sys
import textwrap

import pytest

from aria.channels.output import ChannelFormat, adapt, convert, surface_note

TABLE = textwrap.dedent("""\
    Here are the results:

    | Name  | Age | City   |
    |-------|----:|--------|
    | Alice | 30  | Madrid |
    | Bob   |     | Lima   |

    Done.""")

BASIC = ChannelFormat(markdown="basic")
WHATSAPP = ChannelFormat(markdown="whatsapp")
COMMONMARK = ChannelFormat(markdown="commonmark", tables=True, headings=True)
PLAIN = ChannelFormat(markdown="plain")


def test_tables_become_lists_where_they_cant_render():
    out = convert(TABLE, BASIC)
    assert "|" not in out
    assert "• **Alice** — Age: 30, City: Madrid" in out
    assert "• **Bob** — City: Lima" in out                 # empty cells skipped
    assert out.startswith("Here are the results:") and out.endswith("Done.")


def test_tables_and_headings_kept_where_they_render():
    text = "# Title\n\n" + TABLE
    assert convert(text, COMMONMARK) == text


def test_headings_become_bold_and_rules_go():
    assert convert("## Summary\n\ntext\n\n---\n\nmore", BASIC) == "**Summary**\n\ntext\n\nmore"


def test_code_blocks_are_never_touched():
    code = "```\n| a | b |\n|---|---|\n# not a heading\n**x**\n```"
    assert convert(code, WHATSAPP) == code
    assert convert(code, PLAIN) == code


def test_whatsapp_dialect():
    out = convert("**bold** and __it__ and ~~gone~~ see [docs](https://x.org/a)", WHATSAPP)
    assert out == "*bold* and _it_ and ~gone~ see docs (https://x.org/a)"


def test_plain_strips_markdown():
    assert convert("**Hi** _there_, run `ls` — [site](https://a.b)", PLAIN) == \
        "Hi there, run ls — site (https://a.b)"


def test_long_reply_becomes_head_plus_file():
    fmt = ChannelFormat(markdown="basic", long_reply_chars=300)
    text = "\n\n".join(f"Paragraph {i}: " + "word " * 20 for i in range(10))
    msg, full = adapt(text, fmt, can_attach=True)
    assert full == text
    assert len(msg) <= 300 and msg.endswith("📎 The full reply is attached as a file.")
    assert "Paragraph 0" in msg and "Paragraph 9" not in msg
    # a channel without files gets the whole (converted) text; it splits it
    assert adapt(text, fmt, can_attach=False) == (convert(text, fmt), None)


def test_cut_never_ends_inside_a_code_block():
    fmt = ChannelFormat(markdown="basic", long_reply_chars=200)
    text = "Intro line.\n\n```\n" + "x = 1\n" * 60 + "```\n\nEnd."
    msg, full = adapt(text, fmt, can_attach=True)
    assert full and msg.count("```") % 2 == 0


def test_surface_note():
    note = surface_note("telegram", ChannelFormat(markdown="basic", long_reply_chars=3500))
    assert note.startswith("## Reply surface")
    assert "Telegram" in note and "does NOT show tables or headings" in note
    assert "under about 3500 characters" in note and "send_file" in note
    rich = surface_note("vicus", COMMONMARK)
    assert "full Markdown" in rich and "does NOT show" not in rich


def test_builtin_channels_declare_their_formats(minimal_env):
    import aria.channels as ch
    ch.reset_cache()
    assert ch.get("telegram").output.markdown == "basic"
    assert ch.get("whatsapp").output.markdown == "whatsapp"
    v = ch.get("vicus").output
    assert v.markdown == "commonmark" and v.tables and v.headings


# ── the model is told, per turn ───────────────────────────────────────────────

def test_the_model_gets_the_surface_note_on_channel_turns_only(minimal_env, native_client):
    from aria import context
    from aria.agent import Agent
    a = Agent(window_key="repl", terminal=True)
    inner = native_client("ok")
    seen = []
    real = inner.chat.completions.create
    inner.chat.completions.create = lambda **kw: seen.append(kw["messages"]) or real(**kw)
    a.client = inner
    a._render_answer = lambda t: None

    def ctx_text():
        return "\n".join(m["content"] for m in seen[-1] if m["role"] == "system")

    a.chat("terminal turn")
    assert "Reply surface" not in ctx_text()               # terminal: no rules
    token = context.set_active("telegram", "42")            # a remote-control phone turn
    try:
        a.chat("phone turn")
    finally:
        context.reset(token)
    assert "You are replying on Telegram" in ctx_text()
    # the system prompt (cached prefix) never changes between the two
    assert seen[0][0] == seen[1][0]


def test_scheduled_tasks_are_told_where_results_go(minimal_env, monkeypatch):
    from aria.agent import Agent
    monkeypatch.setenv("ARIA_TASK_ID", "t1")
    monkeypatch.setenv("TELEGRAM_TOKEN", "x")
    import aria.channels as ch
    ch.reset_cache()
    note = Agent(window_key="supervisor", terminal=False)._surface_note()
    assert "Telegram" in note and "scheduled task" in note


# ── delivery ──────────────────────────────────────────────────────────────────

FAKE = textwrap.dedent('''
    from aria.channels import ChannelPlugin
    from aria.channels.output import ChannelFormat
    SENT, FILES = [], []
    class Fake(ChannelPlugin):
        name = "fake"
        supports_files = True
        output = ChannelFormat(markdown="basic", long_reply_chars=200)
        def send(self, text, to=None): SENT.append((text, to))
        def send_file(self, path, caption="", to=None):
            FILES.append((open(path).read(), caption, to)); return "reply.md"
    PLUGIN = Fake()
''')


@pytest.fixture
def fake(minimal_env, tmp_path, monkeypatch):
    d = tmp_path / "channels"
    d.mkdir()
    (d / "fake.py").write_text(FAKE)
    monkeypatch.setenv("ARIA_CHANNELS_DIR", str(d))
    monkeypatch.setenv("ARIA_CHANNELS", "fake")
    import aria.channels as ch
    ch.reset_cache()
    ch.get("fake")
    yield sys.modules["_aria_user_channel_fake"]
    ch.reset_cache()


def test_host_fits_streamed_and_returned_replies_and_attaches_once(fake, monkeypatch):
    from aria import channel as sessions
    from aria.channels import host
    long = "\n\n".join("Paragraph " + "word " * 15 for _ in range(6))

    def fake_handle(ch, uid, text, response_cb=None, activity_cb=None):
        for r in (TABLE, long):
            response_cb(r)
        return [TABLE, long]                     # also returned, as channels expect
    monkeypatch.setattr(sessions, "handle", fake_handle)
    streamed = []
    returned = host.handle_message("fake", "u1", "hi", response_cb=streamed.append)
    assert "|" not in streamed[0] and "• **Alice**" in streamed[0]
    assert streamed[1].endswith("📎 The full reply is attached as a file.")
    assert returned == streamed                  # same fitted text
    assert len(fake.FILES) == 1                  # the file went once, to the right chat
    assert fake.FILES[0][0] == long and fake.FILES[0][2] == "u1"
    import os
    import tempfile
    leftovers = list((__import__("pathlib").Path(tempfile.gettempdir())
                      / f"aria-replies-{os.getuid()}").glob("fake-reply-*"))
    assert leftovers == []                       # deleted once sent


def test_pushes_are_converted(fake):
    import aria.channels as ch
    ch.push("## Daily report\n\n" + TABLE, channel="fake", to="u1")
    text, to = fake.SENT[-1]
    assert text.startswith("**Daily report**") and "|" not in text and to == "u1"
