"""
Workspace tests: per-channel conversation window + self-trim, role
reconstruction, operational-memory cap, core_is_empty onboarding gate,
secret redaction, and file permissions.
"""

from __future__ import annotations

import os
import stat

import pytest


def test_window_is_per_channel(tmp_workspace):
    ws = tmp_workspace
    ws.set_window_key("telegram:111")
    ws.append_conversation_window("user", "book a table", "Aria")
    ws.set_window_key("telegram:222")
    ws.append_conversation_window("user", "what's the weather", "Aria")

    ws.set_window_key("telegram:111")
    a = [m["content"] for m in ws.load_conversation_window_messages()]
    ws.set_window_key("telegram:222")
    b = [m["content"] for m in ws.load_conversation_window_messages()]

    assert a == ["book a table"]
    assert b == ["what's the weather"]


def test_window_self_trims_on_append(tmp_workspace, monkeypatch):
    from aria import workspace as wsmod
    ws = tmp_workspace
    ws.set_window_key("repl")
    cap = wsmod._WINDOW_MESSAGES
    for i in range(cap + 25):
        ws.append_conversation_window("user", f"message {i}", "Aria")
    msgs = ws.load_conversation_window_messages()
    assert len(msgs) == cap                       # bounded even without close()
    assert msgs[-1]["content"] == f"message {cap + 24}"


def test_window_roles_reconstructed(tmp_workspace):
    ws = tmp_workspace
    ws.set_window_key("repl")
    ws.append_conversation_window("user", "hello", "Aria")
    ws.append_conversation_window("assistant", "hi there", "Aria")
    msgs = ws.load_conversation_window_messages()
    assert msgs[0]["role"] == "user" and msgs[0]["content"] == "hello"
    assert msgs[1]["role"] == "assistant" and msgs[1]["content"] == "hi there"


def test_window_excluded_from_memory(tmp_workspace):
    ws = tmp_workspace
    ws.set_window_key("repl")
    ws.append_conversation_window("user", "secret chat content", "Aria")
    assert "secret chat content" not in ws.load_memory()


def test_operational_memory_cap(tmp_workspace, monkeypatch):
    monkeypatch.setenv("ARIA_OPSMEM_MAX_LINES", "5")
    ws = tmp_workspace
    for i in range(20):
        ws.append_operational_memory(f"- entry {i}")
    content = ws.load_operational_memory() or ""
    entries = [l for l in content.splitlines() if l.strip().startswith("-")]
    assert len(entries) <= 5
    assert "entry 19" in content  # newest kept


def test_core_is_empty_gate(tmp_workspace):
    ws = tmp_workspace
    assert ws.core_is_empty() is True
    ws.append_memory("- User's name is Carlos")
    assert ws.core_is_empty() is False


def test_redaction_on_window_write(tmp_workspace):
    ws = tmp_workspace
    ws.set_window_key("repl")
    ws.append_conversation_window("user", "my key is sk-ABCDEFGHIJKLMNOPQRSTUVWX", "Aria")
    raw = ws.load_conversation_window()
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVWX" not in raw


def test_memory_dir_permissions(tmp_workspace):
    ws = tmp_workspace
    ws.append_memory("- a fact")
    mem_dir = ws.root / "memory"
    core    = mem_dir / "core.md"
    assert stat.S_IMODE(mem_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(core.stat().st_mode) == 0o600


def test_legacy_window_migrated_to_repl(tmp_workspace):
    ws = tmp_workspace
    legacy = ws.root / "memory" / "conversation_window.md"
    legacy.write_text("**User:** legacy context", encoding="utf-8")
    ws.set_window_key("repl")  # triggers migration
    msgs = ws.load_conversation_window_messages()
    assert [m["content"] for m in msgs] == ["legacy context"]
    assert not legacy.exists()
