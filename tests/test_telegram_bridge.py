"""Tests the Telegram progress bridge: worker-thread callbacks → asyncio loop
(typing keep-alive, live tool trail, streamed responses)."""

import asyncio
import threading
from types import SimpleNamespace

import pytest

pytest.importorskip("telegram")


class _FakeBot:
    def __init__(self):
        self.sent, self.edited, self.actions = [], [], []
        self._mid = 0

    async def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append(text)
        self._mid += 1
        return SimpleNamespace(message_id=self._mid)

    async def edit_message_text(self, text, chat_id=None, message_id=None):
        self.edited.append(text)

    async def send_chat_action(self, chat_id, action):
        self.actions.append(action)


@pytest.fixture
def running_loop():
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=2)


def test_progress_streams_responses_and_builds_trail(running_loop):
    from aria.telegram_bot import _Progress
    bot = _FakeBot()
    prog = _Progress(bot, "1", running_loop)

    # Called from this (worker) thread, like the agent loop does:
    prog.activity("web_fetch ✓")          # creates the trail message
    prog.activity("shell_run ✓")          # edits it
    prog.response("Hello there")           # streamed mid-turn
    prog.response("Second message")

    # trail posted first, then each response as a separate message
    assert bot.sent[0].startswith("🛠")
    assert "Hello there" in bot.sent
    assert "Second message" in bot.sent
    assert prog.sent == 2
    assert bot.edited and "shell_run ✓" in bot.edited[-1]   # trail updated in place


def test_progress_trail_can_be_disabled(running_loop, monkeypatch):
    monkeypatch.setenv("ARIA_TELEGRAM_PROGRESS", "off")
    from aria.telegram_bot import _Progress
    bot = _FakeBot()
    prog = _Progress(bot, "1", running_loop)
    prog.activity("web_fetch ✓")
    prog.response("answer")
    assert all(not s.startswith("🛠") for s in bot.sent)    # no trail
    assert "answer" in bot.sent                              # response still sent
