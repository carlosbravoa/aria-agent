"""
Regression tests for workspace / reflection / installer infrastructure fixes:
.env preservation + permissions, cwd .env fallback, atomic writes with unique
temps, cross-process locking, reflection merge-at-write, settle window +
watermark, session-name collisions, forget_memory guard, lazy env tuning.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest


# ── Fake LLM client for reflection ────────────────────────────────────────────

class _ScriptedLLM:
    """chat.completions.create stub. `route(prompt) -> reply` decides the reply;
    `on_call(prompt)` runs side effects (simulating another process writing
    while the LLM call is in flight)."""

    def __init__(self, route, on_call=None):

        class _Comp:
            def create(_s, **kw):
                prompt = kw["messages"][0]["content"]
                if on_call:
                    on_call(prompt)
                msg = type("M", (), {"content": route(prompt)})
                return type("R", (), {"choices": [type("C", (), {"message": msg})]})

        self.chat = type("Chat", (), {"completions": _Comp()})()


def _run_reflect(ws, monkeypatch, llm):
    from aria import agent, reflect
    monkeypatch.setattr(agent, "_make_client", lambda *a, **k: llm)
    return reflect._run_locked(ws, False)


def _old_session(ws, name: str, age_s: float = 3600) -> Path:
    p = ws.root / "sessions" / f"{name}.md"
    p.write_text("**USER**\n\nhello\n")
    t = time.time() - age_s
    os.utime(p, (t, t))
    return p


# ── 1/2: aria-install .env preservation + permissions ─────────────────────────

def test_write_env_preserves_unmanaged_keys_verbatim(minimal_env, tmp_path):
    from aria import install
    target = tmp_path / "cfg" / ".env"
    target.parent.mkdir()
    target.write_text(
        "LLM_MODEL=old\n"
        "JIRA_BASE_URL=https://x.atlassian.net\n"
        "LLM_PROFILE1_NAME=fast\n"
        "ARIA_FILE_READ_DIRS=~/Documents:~/projects\n"
        'MY_CUSTOM="quoted value"   \n'
        "# a comment\n"
    )
    install._write_env(target, {"LLM_MODEL": "new", "AGENT_NAME": "A"})
    out = target.read_text()
    assert "LLM_MODEL=new" in out and "LLM_MODEL=old" not in out
    lines = out.splitlines()
    assert "JIRA_BASE_URL=https://x.atlassian.net" in lines   # active, not commented
    assert "LLM_PROFILE1_NAME=fast" in lines
    assert "ARIA_FILE_READ_DIRS=~/Documents:~/projects" in lines
    assert 'MY_CUSTOM="quoted value"' in out           # verbatim, quotes kept
    assert "Other settings (preserved)" in out
    # Re-running is idempotent: no duplicated keys.
    install._write_env(target, {"LLM_MODEL": "new", "AGENT_NAME": "A"})
    out2 = target.read_text()
    assert out2.count("MY_CUSTOM=") == 1
    assert out2.splitlines().count("LLM_PROFILE1_NAME=fast") == 1
    assert out2.splitlines().count("JIRA_BASE_URL=https://x.atlassian.net") == 1


def test_write_env_is_0600_and_dir_0700(minimal_env, tmp_path):
    from aria import install
    d = tmp_path / "aria"
    d.mkdir(mode=0o755)
    target = d / ".env"
    target.write_text("LLM_MODEL=x\n")
    target.chmod(0o644)
    backup = install._write_env(target, {"LLM_MODEL": "m"})
    assert (target.stat().st_mode & 0o777) == 0o600
    assert (d.stat().st_mode & 0o777) == 0o700
    assert (backup.stat().st_mode & 0o777) == 0o600


def test_setup_run_creates_private_env(minimal_env, monkeypatch):
    from aria import setup
    with pytest.raises(SystemExit):
        setup.run()
    aria_dir = Path.home() / ".aria"
    assert ((aria_dir / ".env").stat().st_mode & 0o777) == 0o600
    assert (aria_dir.stat().st_mode & 0o777) == 0o700


# ── 9: no cwd .env fallback ───────────────────────────────────────────────────

def test_config_ignores_cwd_env(minimal_env, tmp_path, monkeypatch):
    from aria import config, setup
    proj = tmp_path / "someproject"
    proj.mkdir()
    (proj / ".env").write_text("LLM_MODEL=hijacked\n")
    monkeypatch.chdir(proj)
    monkeypatch.delenv("ARIA_ENV", raising=False)
    assert config._find_env() is None
    assert setup.is_first_run() is True


# ── 3: _secure_write ──────────────────────────────────────────────────────────

def test_secure_write_unique_temp_and_cleanup_on_failure(tmp_workspace, monkeypatch):
    from aria import workspace as wsmod
    target = tmp_workspace.root / "memory" / "probe.md"

    def boom(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(wsmod.os, "replace", boom)
    with pytest.raises(OSError):
        wsmod._secure_write(target, "x")
    leftovers = [p for p in target.parent.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == [] and not target.exists()


# ── 4: cross-process lost updates ─────────────────────────────────────────────

def _feed_worker(root: str, n: int, tag: str) -> None:
    from aria.workspace import Workspace
    ws = Workspace(root)
    for i in range(n):
        ws.append_friction_log(f"{tag}-{i}")


def test_concurrent_friction_appends_are_not_lost(tmp_workspace):
    import multiprocessing as mp
    ctx = mp.get_context("fork")
    procs = [ctx.Process(target=_feed_worker, args=(str(tmp_workspace.root), 10, t))
             for t in ("a", "b", "c")]
    for p in procs:
        p.start()
    for p in procs:
        p.join(30)
    text = tmp_workspace.load_friction_log() or ""
    assert len([l for l in text.splitlines() if l.startswith("- ")]) == 30


def test_file_lock_is_reentrant(tmp_workspace):
    from aria.workspace import file_lock
    p = tmp_workspace.root / "memory" / "core.md"
    with file_lock(p):
        with file_lock(p):          # would self-deadlock without re-entrancy
            tmp_workspace.append_memory("- nested ok")
    assert "- nested ok" in tmp_workspace.list_memory_facts()


def test_clear_friction_log_keeps_entries_logged_after_snapshot(tmp_workspace):
    ws = tmp_workspace
    ws.append_friction_log("old-1")
    snapshot = ws.load_friction_log()
    ws.append_friction_log("new-during-llm")
    ws.clear_friction_log(snapshot)
    left = ws.load_friction_log() or ""
    assert "new-during-llm" in left and "old-1" not in left


def test_reflect_merges_entries_appended_during_llm_call(tmp_workspace, monkeypatch):
    ws = tmp_workspace
    _old_session(ws, "session_20260101_000000")
    ws.append_operational_memory("- old procedure")
    ws.append_memory("- name is Carlos")

    def on_call(prompt):
        if "operational memory entries" in prompt:
            ws.append_operational_memory("- learned mid-reflection")
        if "core memory" in prompt:
            ws.append_memory("- remembered mid-reflection")

    def route(prompt):
        if "operational memory entries" in prompt:
            return "- consolidated procedure"
        if "core memory" in prompt:
            return "- name is Carlos"
        return "- obs"

    _run_reflect(ws, monkeypatch, _ScriptedLLM(route, on_call))
    ops = ws.load_operational_memory() or ""
    assert "- consolidated procedure" in ops and "- learned mid-reflection" in ops
    assert "- old procedure" not in ops
    core = ws.list_memory_facts()
    assert "- remembered mid-reflection" in core and "- name is Carlos" in core


# ── 6: empty Phase-3 completion ───────────────────────────────────────────────

def test_empty_ops_completion_does_not_wipe_ops_memory(tmp_workspace, monkeypatch):
    ws = tmp_workspace
    _old_session(ws, "session_20260101_000000")
    ws.append_operational_memory("- keep me")
    route = lambda p: "" if "operational memory entries" in p else "- obs"
    _run_reflect(ws, monkeypatch, _ScriptedLLM(route))
    assert "- keep me" in (ws.load_operational_memory() or "")


# ── 5: settle window + watermark ──────────────────────────────────────────────

def test_active_session_is_deferred_not_skipped(tmp_workspace, monkeypatch):
    ws = tmp_workspace
    a = _old_session(ws, "session_20260101_000000")
    b = _old_session(ws, "session_20260101_000001", age_s=5)      # still active
    c = _old_session(ws, "session_20260101_000002")
    seen: list[str] = []

    def route(prompt):
        if prompt.startswith("Analyse these conversation logs"):
            seen.extend(s for s in (a.stem, b.stem, c.stem) if f"### {s}" in prompt)
        return "- obs"

    _run_reflect(ws, monkeypatch, _ScriptedLLM(route))
    assert seen == [a.stem, c.stem]                    # b deferred
    assert (ws.root / "memory" / "reflect_watermark").read_text() == a.stem

    # b settles → next pass picks up b only (c is not re-analysed).
    t = time.time() - 3600
    os.utime(b, (t, t))
    seen.clear()
    _run_reflect(ws, monkeypatch, _ScriptedLLM(route))
    assert seen == [b.stem]
    assert (ws.root / "memory" / "reflect_watermark").read_text() == c.stem
    assert ws.unanalysed_sessions() == []


def test_reflect_tuning_read_lazily(minimal_env, monkeypatch):
    from aria import reflect
    monkeypatch.setenv("ARIA_REFLECT_MAX_LINES", "7")
    assert "Hard limit: 7 bullet" in reflect._consolidation_prompt("x", None)
    monkeypatch.setenv("ARIA_FRICTION_REFLECT_MIN", "1")
    assert reflect._friction_is_hot("- 1 worst=git(1/2)") is True


def test_window_size_read_lazily(tmp_workspace, monkeypatch):
    ws = tmp_workspace
    ws.set_window_key("repl")
    monkeypatch.setenv("ARIA_WINDOW_MESSAGES", "3")
    for i in range(6):
        ws.append_conversation_window("user", f"m{i}", "Aria")
    assert len(ws.load_conversation_window_messages()) == 3


# ── 10: session names + atomic watermark ──────────────────────────────────────

def test_session_paths_unique_and_ordered(tmp_workspace):
    paths = [tmp_workspace.new_session_path() for _ in range(50)]
    assert len(set(paths)) == 50
    assert [p.stem for p in paths] == sorted(p.stem for p in paths)
    # New-format stems still sort after an old-format stem from the same second.
    old = "session_" + paths[0].stem.split("_", 1)[1].rsplit("_", 1)[0]
    assert paths[0].stem > old


def test_update_watermark_is_0600(tmp_workspace):
    p = tmp_workspace.new_session_path()
    tmp_workspace.update_watermark(p)
    wm = tmp_workspace.root / "memory" / "reflect_watermark"
    assert wm.read_text() == p.stem and (wm.stat().st_mode & 0o777) == 0o600


# ── 11: forget_memory guard ───────────────────────────────────────────────────

def test_forget_refuses_short_or_broad_queries(tmp_workspace):
    from aria.workspace import ForgetTooBroad
    ws = tmp_workspace
    for f in ("- likes tea", "- lives in Madrid", "- prefers email",
              "- name is Pete", "- uses vim"):
        ws.append_memory(f)
    with pytest.raises(ForgetTooBroad) as ei:
        ws.forget_memory("e")
    assert "likes tea" in str(ei.value)
    with pytest.raises(ForgetTooBroad):
        ws.forget_memory("   es ")         # "es" after strip → under 4 chars
    assert len(ws.list_memory_facts()) == 5  # nothing removed


def test_forget_exact_line_allowed_even_if_short(tmp_workspace):
    ws = tmp_workspace
    ws.append_memory("- vim")
    ws.append_memory("- uses vim daily")
    assert ws.forget_memory("vim") == 1          # exact match only
    assert ws.list_memory_facts() == ["- uses vim daily"]


def test_forget_too_many_matches_refused(tmp_workspace):
    from aria.workspace import ForgetTooBroad
    ws = tmp_workspace
    for i in range(4):
        ws.append_memory(f"- project alpha item {i}")
    with pytest.raises(ForgetTooBroad):
        ws.forget_memory("project alpha")
    assert ws.forget_memory("project alpha item 2") == 1
