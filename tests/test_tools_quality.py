"""
Tool-quality fixes: TTY block narrowing, exact schedule cancel, base64 binary
write, ambiguous-patch guard, jira assignee-not-found, dispatcher error prefix,
and the gog keyring hint.
"""

from __future__ import annotations

import base64

import pytest


# ── ENV1: is_tty_command only blocks bare REPLs ──────────────────────────────

@pytest.mark.parametrize("cmd, blocked", [
    ("python3 script.py", False),
    ("python3", True),
    ("node app.js", False),
    ("node", True),
    ("bash deploy.sh", False),
    ("bash", True),
    ("mysql -e 'select 1'", False),
    ("mysql", True),
    ("vim notes.txt", True),       # always-interactive regardless of args
    ("ls -la", False),
])
def test_is_tty_command(cmd, blocked):
    from aria.tools._env import is_tty_command
    assert is_tty_command(cmd) is blocked


# ── schedule cancel matches the id exactly ───────────────────────────────────

def test_schedule_cancel_exact_id(minimal_env):
    from aria.tools import schedule
    from aria.task import tasks_dir
    pending = tasks_dir() / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    (pending / "05_abc12345.task").write_text("{}")
    (pending / "07_def67890.task").write_text("{}")

    # a substring of the id must NOT cancel the wrong task
    assert "not found" in schedule._cancel_task("abc")
    assert (pending / "05_abc12345.task").exists()

    # exact id cancels the right one only
    assert "cancelled" in schedule._cancel_task("abc12345")
    assert not (pending / "05_abc12345.task").exists()
    assert (pending / "07_def67890.task").exists()


# ── FA3: base64 writes go through write_bytes (binary works) ──────────────────

def test_file_access_base64_binary_write(minimal_env):
    from aria.tools import file_access
    ws = minimal_env
    target = str(ws / "blob.bin")
    raw = bytes([0, 1, 2, 253, 254, 255]) + b"\x89PNG\r\n"
    b64 = base64.b64encode(raw).decode()
    out = file_access.execute({"action": "write", "path": target,
                               "content": b64, "encoding": "base64"})
    assert "Written" in out
    assert (ws / "blob.bin").read_bytes() == raw          # no UnicodeDecodeError, bytes intact


# ── FA1: patch refuses an ambiguous (multi-match) edit ───────────────────────

def test_file_access_patch_refuses_ambiguous(minimal_env):
    from aria.tools import file_access
    ws = minimal_env
    target = str(ws / "f.txt")
    file_access.execute({"action": "write", "path": target, "content": "foo\nfoo\nbar\n"})
    out = file_access.execute({"action": "patch", "path": target, "old": "foo", "new": "X"})
    assert "appears 2 times" in out
    assert (ws / "f.txt").read_text() == "foo\nfoo\nbar\n"   # unchanged

    # a unique string patches cleanly
    ok = file_access.execute({"action": "patch", "path": target, "old": "bar", "new": "baz"})
    assert "Patched" in ok
    assert "baz" in (ws / "f.txt").read_text()


# ── J2: jira assignee lookup miss raises a clear error ───────────────────────

class _FakeJiraClient:
    def __init__(self, users): self._users = users
    def get(self, path, params=None):
        class R:
            status_code = 200
            def __init__(s, j): s._j = j
            def json(s): return s._j
        if path == "/user/search":
            return R(self._users)
        return R({})


def test_jira_assignee_not_found_raises(minimal_env):
    from aria.tools import jira
    with pytest.raises(ValueError, match="no Jira user found"):
        jira._resolve_account_id(_FakeJiraClient(users=[]), "Nobody Here")


def test_jira_assignee_found_resolves(minimal_env):
    from aria.tools import jira
    c = _FakeJiraClient(users=[{"accountId": "ACC1", "displayName": "Jane"}])
    assert jira._resolve_account_id(c, "jane@x.com") == "ACC1"


# ── dispatcher error prefix is consistent ────────────────────────────────────

def test_dispatcher_unknown_tool_prefix(minimal_env):
    from aria import tools
    out = tools.dispatch("definitely_not_a_real_tool", {})
    assert out.startswith("[tools] error:") and "unknown tool" in out


# ── gog keyring hint ─────────────────────────────────────────────────────────

def test_gog_keyring_hint_triggers_and_is_silent():
    from aria.tools._env import gog_keyring_hint
    assert "GOG_KEYRING_BACKEND" in gog_keyring_hint("error: keyring is locked")
    assert "GOG_KEYRING_BACKEND" in gog_keyring_hint("SecretStorage dbus error")
    assert gog_keyring_hint("Message not found") == ""


# ── L2: a broken user tool must not crash discovery ──────────────────────────

def test_broken_user_tool_does_not_crash_loading(tmp_path):
    from aria import tools
    d = tmp_path / "tools"; d.mkdir()
    (d / "broken.py").write_text("raise RuntimeError('boom at import')\n")
    (d / "good.py").write_text(
        "DEFINITION = {'name': 'mygood', 'description': 'x', "
        "'parameters': {'type': 'object', 'properties': {}}}\n"
        "def execute(args): return 'ok'\n"
    )
    schemas = tools.load_all(d)                       # must not raise
    names = [t["function"]["name"] for t in schemas]
    assert "mygood" in names                          # good tool loads despite broken sibling


# ── G1: gmail --json thread formatting (verified against real gog output) ─────

def test_gmail_format_threads_real_shape():
    from aria.tools.gmail import _format_threads
    raw = ('{"nextPageToken":"107","threads":[{"id":"19ed59fa67802873",'
           '"date":"2026-06-17 08:47","from":"AliExpress <t@x.com>",'
           '"subject":"Package left the region","labels":["UNREAD","INBOX"],'
           '"messageCount":1}]}')
    out = _format_threads(raw)
    assert "[19ed59fa67802873]" in out      # ID surfaced for read/mark_read
    assert "Package left the region" in out
    assert "UNREAD" in out


def test_gmail_format_threads_fallbacks():
    from aria.tools.gmail import _format_threads
    assert _format_threads('{"threads":[]}') == "No messages."
    assert _format_threads("plain text not json") == "plain text not json"   # no regression
    assert _format_threads("[gmail error] exit=1").startswith("[gmail error]")  # error passthrough
