"""
Tests for the self-update tool: config-error paths, PEP 668 pip handling, the
import-validator, and the fetch/diff/dry-run flow against a real temp git repo.
No network, no systemd, no real install side effects.
"""

from __future__ import annotations

import subprocess

import pytest


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


def test_errors_when_source_unset(minimal_env, monkeypatch):
    from aria.tools import update
    monkeypatch.delenv("ARIA_SOURCE_DIR", raising=False)
    assert "ARIA_SOURCE_DIR not set" in update.execute({})


def test_errors_when_source_missing(minimal_env, monkeypatch, tmp_path):
    from aria.tools import update
    monkeypatch.setenv("ARIA_SOURCE_DIR", str(tmp_path / "nope"))
    assert "Source directory not found" in update.execute({})


def test_errors_when_not_a_git_repo(minimal_env, monkeypatch, tmp_path):
    from aria.tools import update
    monkeypatch.setenv("ARIA_SOURCE_DIR", str(tmp_path))
    assert "Not a git repository" in update.execute({})


def test_pip_install_adds_break_system_packages_when_externally_managed(monkeypatch, tmp_path):
    from aria.tools import update
    captured = {}
    monkeypatch.setattr(update, "_run", lambda argv, cwd=None, timeout=120: captured.setdefault("argv", argv) or (0, "", ""))
    # pretend the stdlib has an EXTERNALLY-MANAGED marker
    stdlib = tmp_path / "stdlib"; stdlib.mkdir()
    (stdlib / "EXTERNALLY-MANAGED").write_text("")
    monkeypatch.setattr(update.sysconfig, "get_path", lambda name: str(stdlib))
    update._pip_install(tmp_path)
    assert "--break-system-packages" in captured["argv"]


def test_pip_install_no_flag_when_not_externally_managed(monkeypatch, tmp_path):
    from aria.tools import update
    captured = {}
    monkeypatch.setattr(update, "_run", lambda argv, cwd=None, timeout=120: captured.setdefault("argv", argv) or (0, "", ""))
    stdlib = tmp_path / "stdlib"; stdlib.mkdir()   # no EXTERNALLY-MANAGED file
    monkeypatch.setattr(update.sysconfig, "get_path", lambda name: str(stdlib))
    update._pip_install(tmp_path)
    assert "--break-system-packages" not in captured["argv"]


def test_validator_passes_on_current_good_install(minimal_env):
    # The installed package is healthy → the import-smoke gate must pass.
    from aria.tools import update
    ok, detail = update._validate_imports()
    assert ok, f"validator should pass on good code: {detail}"


def test_validator_fails_on_broken_package(minimal_env, monkeypatch):
    # Point the subprocess at a broken 'aria' package on a temp sys.path.
    from aria.tools import update
    import sys, textwrap, tempfile, os
    broken = tempfile.mkdtemp()
    pkg = os.path.join(broken, "aria"); os.makedirs(pkg)
    open(os.path.join(pkg, "__init__.py"), "w").close()
    # a submodule that raises on import
    with open(os.path.join(pkg, "boom.py"), "w") as f:
        f.write("raise RuntimeError('boom')\n")
    real_run = subprocess.run
    def fake_run(argv, **kw):
        env = dict(kw.get("env") or os.environ)
        env["PYTHONPATH"] = broken + os.pathsep + env.get("PYTHONPATH", "")
        kw["env"] = env
        return real_run(argv, **kw)
    monkeypatch.setattr(update.subprocess, "run", fake_run)
    ok, detail = update._validate_imports()
    assert not ok


def test_dry_run_reports_incoming_commits(minimal_env, monkeypatch, tmp_path):
    from aria.tools import update
    # bare "remote" + a working clone
    remote = tmp_path / "remote.git"
    work   = tmp_path / "work"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(tmp_path, "clone", str(remote), str(work))
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        _git(work, "config", k, v)
    (work / "a.txt").write_text("one")
    _git(work, "add", "."); _git(work, "commit", "-m", "commit A")
    _git(work, "branch", "-M", "main"); _git(work, "push", "-u", "origin", "main")
    (work / "a.txt").write_text("two")
    _git(work, "commit", "-am", "commit B"); _git(work, "push")
    _git(work, "reset", "--hard", "HEAD~1")   # local now behind origin/main by 1

    monkeypatch.setenv("ARIA_SOURCE_DIR", str(work))
    monkeypatch.setenv("ARIA_UPDATE_BRANCH", "main")
    out = update.execute({"dry_run": True})
    assert "commit B" in out          # shows the incoming commit
    assert "Dry run" in out
    # dry run made no changes: still on commit A
    rc = subprocess.run(["git", "log", "-1", "--pretty=%s"], cwd=work,
                        capture_output=True, text=True)
    assert rc.stdout.strip() == "commit A"


# ── Phase 2: post-restart watchdog / auto-rollback ────────────────────────────

def test_arm_watchdog_writes_marker(minimal_env, tmp_path):
    import json
    from aria.tools import update
    update._arm_watchdog("abc123def", tmp_path, "main")
    st = json.loads(update._state_path().read_text())
    assert st["pending"] and st["prev_sha"] == "abc123def" and st["deadline"] > 0


def _stub_rollback_calls(monkeypatch):
    from aria.tools import update
    calls = {"git": [], "pip": 0, "restart": []}
    monkeypatch.setattr(update, "_git", lambda args, cwd: calls["git"].append(args) or (0, "", ""))
    monkeypatch.setattr(update, "_pip_install", lambda src: calls.__setitem__("pip", calls["pip"] + 1) or (0, "", ""))
    monkeypatch.setattr(update, "_run", lambda argv, cwd=None, timeout=120: calls["restart"].append(argv) or (0, "", ""))
    return calls


def test_rollback_reverts_within_window(minimal_env, monkeypatch, tmp_path):
    import json, time
    from aria.tools import update
    sp = update._state_path(); sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps({"pending": True, "prev_sha": "deadbeef00", "src": str(tmp_path),
                              "branch": "main", "deadline": time.time() + 600}))
    calls = _stub_rollback_calls(monkeypatch)
    update.rollback_main()
    assert ["reset", "--hard", "deadbeef00"] in calls["git"]
    assert calls["pip"] == 1
    assert any("restart" in argv for argv in calls["restart"])
    assert not sp.exists() and not sp.with_suffix(".claimed").exists()   # consumed


def test_rollback_skips_after_confirm_window(minimal_env, monkeypatch, tmp_path):
    import json, time
    from aria.tools import update
    sp = update._state_path(); sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps({"pending": True, "prev_sha": "deadbeef00", "src": str(tmp_path),
                              "branch": "main", "deadline": time.time() - 5}))  # expired
    calls = _stub_rollback_calls(monkeypatch)
    update.rollback_main()
    assert calls["git"] == [] and calls["pip"] == 0   # no rollback after window
    assert not sp.exists()


def test_rollback_noop_without_marker(minimal_env, monkeypatch):
    from aria.tools import update
    sp = update._state_path()
    if sp.exists():
        sp.unlink()
    calls = _stub_rollback_calls(monkeypatch)
    update.rollback_main()                 # must not raise
    assert calls["git"] == [] and calls["pip"] == 0


def test_rollback_is_idempotent_second_call_noops(minimal_env, monkeypatch, tmp_path):
    import json, time
    from aria.tools import update
    sp = update._state_path(); sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps({"pending": True, "prev_sha": "deadbeef00", "src": str(tmp_path),
                              "branch": "main", "deadline": time.time() + 600}))
    calls = _stub_rollback_calls(monkeypatch)
    update.rollback_main()    # claims + rolls back
    update.rollback_main()    # marker gone → no-op
    assert calls["pip"] == 1  # only one rollback happened


# ── install.py unit generation ────────────────────────────────────────────────

def test_service_unit_has_watchdog_directives():
    from aria import install
    u = install._service("Aria Telegram", "/bin/aria-telegram", "/home/u/.aria/.env")
    assert "OnFailure=aria-rollback.service" in u
    assert "StartLimitBurst=5" in u and "StartLimitIntervalSec=300" in u


def test_rollback_unit_is_oneshot_and_not_enabled():
    from aria import install
    u = install._rollback_service("/home/u/.local/bin/aria-rollback", "/home/u/.aria/.env")
    assert "Type=oneshot" in u and "aria-rollback" in u
    assert "[Install]" not in u   # triggered on demand, never enabled at boot