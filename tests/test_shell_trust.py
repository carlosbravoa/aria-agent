"""Tests for the learnable shell approval flow + opt-in sandbox (roadmap #4)."""

import pytest

from aria.tools import shell_run as sr


@pytest.fixture
def allowlist(tmp_path, monkeypatch):
    f = tmp_path / "shell_allowlist.json"
    monkeypatch.setattr(sr, "_ALLOWLIST_FILE", f)
    return f


def test_command_prefix():
    assert sr._command_prefix("git push origin main") == "git push"
    assert sr._command_prefix("rm -rf build/") == "rm -rf"
    assert sr._command_prefix("ls") == "ls"


def test_allowlist_roundtrip_and_boundary(allowlist):
    sr._persist_allow("git push")
    assert sr._is_allowlisted("git push origin main")   # prefix on token boundary
    assert sr._is_allowlisted("git push")
    assert not sr._is_allowlisted("git status")
    assert not sr._is_allowlisted("gitpush evil")        # no false boundary match


def test_persist_is_idempotent(allowlist):
    sr._persist_allow("npm install")
    sr._persist_allow("npm install")
    assert sr._load_allowlist() == ["npm install"]


def test_gate_skips_prompt_when_allowlisted(allowlist, monkeypatch):
    monkeypatch.setattr(sr, "_is_interactive", lambda: True)
    sr._persist_allow("rm -rf")
    # destructive, but pre-approved → allowed without calling input()
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(
        AssertionError("should not prompt")))
    assert sr._gate("rm -rf build/") is None


def test_gate_prompts_when_not_allowlisted(allowlist, monkeypatch):
    monkeypatch.setattr(sr, "_is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    assert sr._gate("rm -rf build/") == "[shell_run] Cancelled by user."


def test_confirm_always_persists_prefix(allowlist, monkeypatch):
    monkeypatch.setattr(sr, "_is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "a")
    assert sr._confirm("docker build .") is True
    assert "docker build" in sr._load_allowlist()


def test_sandbox_prefix_unset(monkeypatch):
    monkeypatch.delenv("ARIA_SHELL_SANDBOX", raising=False)
    assert sr._sandbox_prefix() == []


def test_sandbox_prefix_missing_binary(monkeypatch):
    monkeypatch.setenv("ARIA_SHELL_SANDBOX", "definitely-not-a-real-binary-xyz")
    assert sr._sandbox_prefix() == []      # graceful: don't break every command


def test_sandbox_wraps_command(monkeypatch):
    # 'env' exists everywhere and is a transparent wrapper — proves the wrap path.
    monkeypatch.setenv("ARIA_SHELL_SANDBOX", "env")
    assert sr._sandbox_prefix() == ["env"]
    out = sr._run_shell("echo sandboxed-ok", None, None, 10)
    assert "sandboxed-ok" in out
