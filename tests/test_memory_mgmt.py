"""
Tests for the memory-management additions (roadmap: "Memory becomes real") and
the supporting engine helpers:

- Workspace: atomic _secure_write, append_memory dedup, list/forget/search,
  save/load_core_memory.
- remember/learn tools: add/list/forget actions.
- memory_search tool.
- agent token helpers (_estimate_tokens/_message_tokens) and _make_client
  resilience config.
- telegram _Progress._send_response success/failure reporting (progress.sent fix).
"""

from __future__ import annotations

import asyncio

import pytest


# ── Workspace: atomic write ────────────────────────────────────────────────────

def test_secure_write_is_atomic_and_0600(tmp_workspace):
    from aria.workspace import _secure_write
    target = tmp_workspace.root / "memory" / "atomic_probe.md"
    _secure_write(target, "hello")
    assert target.read_text() == "hello"
    assert (target.stat().st_mode & 0o777) == 0o600
    # no leftover sibling temp file
    assert not (target.parent / (target.name + ".tmp")).exists()


# ── Workspace: memory dedup / list / forget / search ────────────────────────────

def test_append_memory_dedups_exact_facts(tmp_workspace):
    tmp_workspace.append_memory("- likes tea")
    tmp_workspace.append_memory("- likes tea")   # exact duplicate → skipped
    tmp_workspace.append_memory("- lives in Madrid")
    facts = tmp_workspace.list_memory_facts("core.md")
    assert facts.count("- likes tea") == 1
    assert "- lives in Madrid" in facts


def test_list_memory_facts_excludes_header_and_comments(tmp_workspace):
    tmp_workspace.append_memory("- fact one")
    facts = tmp_workspace.list_memory_facts("core.md")
    assert facts == ["- fact one"]  # no "# Core Memory", no <!-- ts -->


def test_forget_memory_removes_matching_entry_and_comment(tmp_workspace):
    tmp_workspace.append_memory("- likes tea")
    tmp_workspace.append_memory("- likes coffee")
    removed = tmp_workspace.forget_memory("coffee", "core.md")
    assert removed == 1
    facts = tmp_workspace.list_memory_facts("core.md")
    assert facts == ["- likes tea"]
    raw = (tmp_workspace.root / "memory" / "core.md").read_text()
    # the forgotten fact's timestamp comment is gone too (no orphan comments)
    assert "coffee" not in raw


def test_forget_memory_no_match_returns_zero(tmp_workspace):
    tmp_workspace.append_memory("- likes tea")
    assert tmp_workspace.forget_memory("nonexistent", "core.md") == 0


def test_search_memory_across_stores(tmp_workspace):
    tmp_workspace.append_memory("- name is Carlos")
    tmp_workspace.append_operational_memory("- use Jira project PROJ")
    core_hits = tmp_workspace.search_memory("carlos")
    assert core_hits and core_hits[0][0] == "core.md"
    ops_hits = tmp_workspace.search_memory("jira")
    assert ops_hits and ops_hits[0][0] == "operational_memory.md"
    assert tmp_workspace.search_memory("zzz-not-there") == []


def test_save_and_load_core_memory(tmp_workspace):
    tmp_workspace.save_core_memory("- a\n- b")
    assert tmp_workspace.load_core_memory() == "- a\n- b"


# ── remember / learn tools ──────────────────────────────────────────────────────

def test_remember_tool_add_list_forget(minimal_env):
    from aria.tools import remember
    assert "Saved" in remember.execute({"fact": "user is a pilot"})
    listing = remember.execute({"action": "list"})
    assert "pilot" in listing
    forgot = remember.execute({"action": "forget", "fact": "pilot"})
    assert "Removed 1" in forgot
    assert "empty" in remember.execute({"action": "list"}).lower()


def test_learn_tool_add_list_forget(minimal_env):
    from aria.tools import learn
    assert "Saved" in learn.execute({"procedure": "deploy with make ship"})
    assert "make ship" in learn.execute({"action": "list"})
    assert "Removed 1" in learn.execute({"action": "forget", "procedure": "make ship"})


def test_memory_search_tool(minimal_env):
    from aria.tools import memory_search, remember
    remember.execute({"fact": "timezone is Europe/Madrid"})
    out = memory_search.execute({"query": "madrid"})
    assert "Madrid" in out and "core.md" in out
    assert memory_search.DEFINITION["name"] == "memory_search"


# ── agent engine helpers ────────────────────────────────────────────────────────

def test_estimate_and_message_tokens():
    from aria.agent import _estimate_tokens, _message_tokens
    assert _estimate_tokens("") == 0
    assert _estimate_tokens("a" * 400) == 100
    m = {"role": "assistant", "content": "hi",
         "tool_calls": [{"function": {"name": "x", "arguments": '{"a":1}'}}]}
    assert _message_tokens(m) > _estimate_tokens("hi")


def test_make_client_sets_retry_and_timeout(minimal_env):
    from aria.agent import _make_client
    client = _make_client("http://test.invalid", "k")
    assert client.max_retries == 4
    assert client.timeout is not None


# ── telegram progress.sent accounting fix ───────────────────────────────────────

class _FakeBot:
    def __init__(self, fail=False):
        self.fail = fail
        self.sent = []

    async def send_message(self, chat_id, text, parse_mode=None):
        if self.fail:
            raise RuntimeError("boom")
        self.sent.append((text, parse_mode))


def test_send_response_reports_delivery(minimal_env):
    from aria import telegram_bot
    ok = telegram_bot._Progress(_FakeBot(), "123", loop=None)
    assert asyncio.run(ok._send_response("hello")) is True

    bad = telegram_bot._Progress(_FakeBot(fail=True), "123", loop=None)
    assert asyncio.run(bad._send_response("hello")) is False
