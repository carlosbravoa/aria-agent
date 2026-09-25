"""
Risky tool actions ask the user (aria.approval) when nobody is at a terminal —
a channel turn, remote control, or a scheduled task — instead of being refused
outright (shell) or running unchecked (deletes, git push, update). In the
terminal nothing changes. Everything is offline: approval.request is stubbed
per test, except the end-to-end tests at the bottom, which drive the real
request/answer file protocol through a fake drop-in channel plugin.
"""

from __future__ import annotations

import sys
import threading
import time

import pytest

from aria import approval, context


# ── helpers ───────────────────────────────────────────────────────────────────

class _Asks:
    """Stand-in for approval.request: records summaries, answers `ok`."""

    def __init__(self, ok: bool, why: str = ""):
        self.ok = ok
        self.why = why or ("approved" if ok else "denied by the user")
        self.summaries: list[str] = []

    def __call__(self, summary: str):
        self.summaries.append(summary)
        return self.ok, self.why


@pytest.fixture
def asks(monkeypatch):
    """Factory: asks(ok) installs a stub approval.request and returns it."""
    def install(ok: bool, why: str = "") -> _Asks:
        stub = _Asks(ok, why)
        monkeypatch.setattr(approval, "request", stub)
        return stub
    return install


@pytest.fixture
def channel_turn():
    """Run the test body as a Telegram channel turn."""
    token = context.set_active("telegram", "4242")
    yield
    context.reset(token)


@pytest.fixture
def task_turn(monkeypatch):
    """Run the test body as a scheduled supervisor task that opted in to
    approvals (ARIA_APPROVAL_TASKS=on — tasks don't ask by default)."""
    monkeypatch.setenv("ARIA_TASK_ID", "abc12345")
    monkeypatch.setenv("ARIA_APPROVAL_TASKS", "on")


@pytest.fixture
def terminal(monkeypatch):
    """The REPL: no channel context, no task."""
    context.clear()
    monkeypatch.delenv("ARIA_TASK_ID", raising=False)


@pytest.fixture(autouse=True)
def _clean_approval_env(minimal_env, monkeypatch):
    for k in ("ARIA_APPROVALS", "ARIA_APPROVAL_REQUIRED", "ARIA_APPROVAL_TIMEOUT",
              "ARIA_TASK_ID", "ARIA_SHELL_UNATTENDED", "ARIA_SHELL_SAFE_EXTRA"):
        monkeypatch.delenv(k, raising=False)
    context.clear()
    yield
    context.clear()


# ── shell_run ─────────────────────────────────────────────────────────────────

@pytest.fixture
def sr(monkeypatch):
    from aria.tools import shell_run
    ran: list = []
    monkeypatch.setattr(shell_run, "_run_shell",
                        lambda cmd, **kw: ran.append(cmd) or "RAN")
    monkeypatch.setattr(shell_run, "_run_script",
                        lambda argv, **kw: ran.append(argv) or "RAN")
    monkeypatch.setattr(shell_run.os, "isatty", lambda fd: False)
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(
        AssertionError("must never prompt on the terminal")))
    shell_run.ran = ran
    return shell_run


def test_shell_safe_mode_non_allowlisted_asks_and_runs(sr, asks, channel_turn):
    stub = asks(True)
    assert sr.execute({"command": "make build"}) == "RAN"
    assert stub.summaries == ["run: make build"]
    assert sr.ran == ["make build"]


def test_shell_safe_mode_non_allowlisted_denied(sr, asks, channel_turn):
    asks(False)
    out = sr.execute({"command": "make build"})
    assert "not on the unattended-safe allowlist" in out
    assert "denied by the user" in out
    assert sr.ran == []


def test_shell_allowlisted_runs_without_asking(sr, asks, channel_turn):
    stub = asks(False)
    assert sr.execute({"command": "ls -la"}) == "RAN"
    assert stub.summaries == []


def test_shell_destructive_asks_in_task(sr, asks, task_turn):
    stub = asks(True)
    assert sr.execute({"command": "rm -rf build/"}) == "RAN"
    assert stub.summaries == ["run: rm -rf build/"]


def test_shell_destructive_expired_keeps_refusal(sr, asks, channel_turn):
    asks(False, "not approved within 300 s")
    out = sr.execute({"command": "rm -rf build/"})
    assert "Refused" in out and "destructive" in out
    assert "not approved within 300 s" in out
    assert sr.ran == []


def test_shell_full_mode_destructive_asks(sr, asks, channel_turn, monkeypatch):
    monkeypatch.setenv("ARIA_SHELL_UNATTENDED", "full")
    stub = asks(True)
    assert sr.execute({"command": "rm -rf build/"}) == "RAN"
    assert stub.summaries == ["run: rm -rf build/"]


def test_shell_script_summary_has_interpreter_and_first_lines(sr, asks, channel_turn):
    stub = asks(True)
    script = "import shutil\nshutil.rmtree('build')\n"
    assert sr.execute({"script": script, "interpreter": "python3"}) == "RAN"
    assert len(stub.summaries) == 1
    assert "python3" in stub.summaries[0]
    assert "shutil.rmtree('build')" in stub.summaries[0]


def test_shell_off_mode_never_asks(sr, asks, channel_turn, monkeypatch):
    monkeypatch.setenv("ARIA_SHELL_UNATTENDED", "off")
    stub = asks(True)
    out = sr.execute({"command": "make build"})
    assert "ARIA_SHELL_UNATTENDED=off" in out
    assert stub.summaries == [] and sr.ran == []


def test_shell_secret_path_never_asks(sr, asks, channel_turn):
    stub = asks(True)
    out = sr.execute({"command": "cat ~/.ssh/id_rsa"})
    assert "Refused" in out and "sensitive path" in out
    assert stub.summaries == [] and sr.ran == []


def test_shell_approvals_off_is_old_behaviour(sr, asks, channel_turn, monkeypatch):
    monkeypatch.setenv("ARIA_APPROVALS", "off")
    stub = asks(True)
    out = sr.execute({"command": "rm -rf build/"})
    assert out.startswith("[shell_run] Refused")
    assert "approval" not in out
    assert stub.summaries == [] and sr.ran == []


def test_shell_no_tty_no_channel_no_task_still_refuses(sr, asks, terminal):
    # Non-interactive but not "unattended" (e.g. piped single-shot): old rules.
    stub = asks(True)
    out = sr.execute({"command": "rm -rf build/"})
    assert "Refused" in out and stub.summaries == []


def test_shell_terminal_keeps_tty_prompt(sr, asks, terminal, monkeypatch):
    stub = asks(True)
    monkeypatch.setattr(sr.os, "isatty", lambda fd: True)
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    assert sr.execute({"command": "rm -rf build/"}) == "[shell_run] Cancelled by user."
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    assert sr.execute({"command": "rm -rf build/"}) == "RAN"
    assert stub.summaries == []


def test_shell_tty_command_rejected_before_asking(sr, asks, channel_turn):
    stub = asks(True)
    out = sr.execute({"command": "vim notes.txt"})
    assert "interactive terminal" in out and stub.summaries == []


# ── gog-backed tools (gmail / drive / calendar) ───────────────────────────────

@pytest.fixture
def gog(monkeypatch):
    """Record every gog command line; `get --json` lookups return a name."""
    from aria.tools import calendar, drive, gmail
    cmds: list[str] = []

    def fake(cmd: str, **kw) -> str:
        cmds.append(cmd)
        if " drive get " in cmd:
            return '{"file": {"id": "F1", "name": "Report.pdf"}}'
        if " calendar get " in cmd:
            return '{"event": {"id": "E1", "summary": "Standup"}}'
        return "OK"
    for mod in (calendar, drive, gmail):
        monkeypatch.setattr(mod._gog, "run", fake)
    return cmds


def test_drive_delete_asks_with_name(gog, asks, channel_turn):
    from aria.tools import drive
    stub = asks(True)
    assert drive.execute({"action": "delete", "file_id": "F1"}) == "OK"
    assert stub.summaries == ["delete Drive file 'Report.pdf' (id F1)"]
    assert any("drive delete" in c for c in gog)


def test_drive_delete_denied(gog, asks, task_turn):
    from aria.tools import drive
    asks(False)
    out = drive.execute({"action": "delete", "file_id": "F1"})
    assert out.startswith("[approval] Not done") and "denied by the user" in out
    assert not any("drive delete" in c for c in gog)


def test_drive_delete_terminal_unchanged(gog, asks, terminal):
    from aria.tools import drive
    stub = asks(False)
    assert drive.execute({"action": "delete", "file_id": "F1"}) == "OK"
    assert stub.summaries == []
    assert gog == [c for c in gog if "drive get" not in c]   # no name lookup


def test_drive_delete_approvals_off(gog, asks, channel_turn, monkeypatch):
    from aria.tools import drive
    monkeypatch.setenv("ARIA_APPROVALS", "off")
    assert drive.execute({"action": "delete", "file_id": "F1"}) == "OK"


def test_drive_other_actions_never_ask(gog, asks, channel_turn):
    from aria.tools import drive
    stub = asks(False)
    assert drive.execute({"action": "rename", "file_id": "F1", "name": "x"}) == "OK"
    assert stub.summaries == []


def test_calendar_delete_asks_with_title(gog, asks, channel_turn):
    from aria.tools import calendar
    stub = asks(True)
    assert calendar.execute({"action": "delete", "event_id": "E1"}) == "OK"
    assert stub.summaries == ["delete calendar event 'Standup' (id E1)"]


def test_calendar_delete_denied(gog, asks, channel_turn):
    from aria.tools import calendar
    asks(False)
    out = calendar.execute({"action": "delete", "event_id": "E1"})
    assert out.startswith("[approval] Not done")
    assert not any("calendar delete" in c for c in gog)


def test_calendar_create_opt_in(gog, asks, channel_turn, monkeypatch):
    from aria.tools import calendar
    stub = asks(False)
    args = {"action": "create", "summary": "Lunch",
            "start": "2026-10-01T12:00:00", "end": "2026-10-01T13:00:00"}
    assert calendar.execute(args) == "OK"          # not in the default set
    assert stub.summaries == []
    monkeypatch.setenv("ARIA_APPROVAL_REQUIRED", "delete,calendar_create")
    out = calendar.execute(args)
    assert out.startswith("[approval] Not done")
    assert "Lunch" in stub.summaries[0]
    out = calendar.execute({"action": "update", "event_id": "E1", "summary": "Brunch"})
    assert out.startswith("[approval] Not done")
    assert "'Standup' (id E1)" in stub.summaries[1] and "Brunch" in stub.summaries[1]


def test_gmail_send_opt_in(gog, asks, task_turn, monkeypatch):
    from aria.tools import gmail
    stub = asks(False)
    args = {"action": "send", "to": "x@y.com", "subject": "Hi", "body": "b"}
    assert gmail.execute(args) == "OK"              # default: scheduled sends keep working
    assert stub.summaries == []
    monkeypatch.setenv("ARIA_APPROVAL_REQUIRED", "gmail_send")
    out = gmail.execute(args)
    assert out.startswith("[approval] Not done")
    assert stub.summaries == ["send email to x@y.com: Hi"]
    assert sum("gmail send" in c for c in gog) == 1


def test_gmail_send_opt_in_approved(gog, asks, channel_turn, monkeypatch):
    from aria.tools import gmail
    monkeypatch.setenv("ARIA_APPROVAL_REQUIRED", "gmail_send")
    asks(True)
    assert gmail.execute({"action": "send", "to": "x@y.com", "subject": "Hi"}) == "OK"


# ── file_access ───────────────────────────────────────────────────────────────

def test_file_access_delete(minimal_env, asks, channel_turn):
    from aria.tools import file_access
    f = minimal_env / "notes" / "old.md"
    f.parent.mkdir(parents=True)
    f.write_text("x")
    asks(False)
    out = file_access.execute({"action": "delete", "path": str(f)})
    assert out.startswith("[approval] Not done") and f.exists()
    stub = asks(True)
    out = file_access.execute({"action": "delete", "path": str(f)})
    assert "Deleted" in out and not f.exists()
    assert stub.summaries == [f"delete file {f}"]


def test_file_access_delete_terminal_unchanged(minimal_env, asks, terminal):
    from aria.tools import file_access
    f = minimal_env / "old.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("x")
    stub = asks(False)
    assert "Deleted" in file_access.execute({"action": "delete", "path": str(f)})
    assert stub.summaries == []


# ── git ───────────────────────────────────────────────────────────────────────

@pytest.fixture
def fake_git(monkeypatch, tmp_path):
    from aria.tools import git
    calls: list[tuple] = []

    def fake(root, *args):
        calls.append(args)
        if args[:2] == ("rev-parse", "--abbrev-ref") and args[-1] == "HEAD":
            return "main"
        if args[-1] == "@{u}":
            return "origin/main"
        return "pushed"
    monkeypatch.setattr(git, "_git", fake)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    return git, calls, repo


def test_git_push_asks(fake_git, asks, channel_turn):
    git, calls, repo = fake_git
    stub = asks(True)
    assert git.execute({"action": "push", "path": str(repo)}) == "pushed"
    assert stub.summaries[0].startswith("push branch main to origin")
    assert ("push",) in calls


def test_git_push_denied(fake_git, asks, task_turn):
    git, calls, repo = fake_git
    asks(False)
    out = git.execute({"action": "push", "path": str(repo)})
    assert out.startswith("[approval] Not done")
    assert ("push",) not in calls


def test_git_push_terminal_unchanged(fake_git, asks, terminal):
    git, calls, repo = fake_git
    stub = asks(False)
    assert git.execute({"action": "push", "path": str(repo)}) == "pushed"
    assert stub.summaries == [] and calls == [("push",)]


def test_git_commit_never_asks(fake_git, asks, channel_turn):
    git, calls, repo = fake_git
    stub = asks(False)
    git.execute({"action": "commit", "path": str(repo), "message": "m"})
    assert stub.summaries == []


# ── update ────────────────────────────────────────────────────────────────────

@pytest.fixture
def fake_update(monkeypatch, tmp_path):
    from aria.tools import update
    src = tmp_path / "src"
    (src / ".git").mkdir(parents=True)
    monkeypatch.setenv("ARIA_SOURCE_DIR", str(src))
    calls: list[list] = []

    def fake_git(args, cwd):
        calls.append(args)
        if args == ["rev-parse", "HEAD"]:
            return 0, "a" * 40, ""
        if args[0] == "rev-parse":
            return 0, "b" * 40, ""
        if args[0] == "log":
            return 0, "bbbbbbb new thing", ""
        return 0, "", ""
    monkeypatch.setattr(update, "_git", fake_git)
    # stop right after the reset: a failed pip install rolls back and returns
    monkeypatch.setattr(update, "_pip_install", lambda src: (1, "", "stop here"))
    return update, calls


def _resets(calls):
    return [c for c in calls if c[:2] == ["reset", "--hard"]]


def test_update_asks_before_applying(fake_update, asks, channel_turn):
    update, calls = fake_update
    stub = asks(True)
    out = update.execute({})
    assert "pip install failed" in out            # got past the approval
    assert _resets(calls)[0] == ["reset", "--hard", "b" * 40]
    assert stub.summaries == ["update Aria aaaaaaaaa → bbbbbbbbb (1 commit(s) from "
                              "origin/main), then restart services"]


def test_update_denied_changes_nothing(fake_update, asks, task_turn):
    update, calls = fake_update
    asks(False)
    out = update.execute({})
    assert "[approval] Not done" in out and "Incoming" in out
    assert _resets(calls) == []


def test_update_dry_run_never_asks(fake_update, asks, channel_turn):
    update, calls = fake_update
    stub = asks(False)
    assert "Dry run" in update.execute({"dry_run": True})
    assert stub.summaries == []


def test_update_terminal_unchanged(fake_update, asks, terminal):
    update, calls = fake_update
    stub = asks(False)
    update.execute({})
    assert stub.summaries == [] and _resets(calls)


# ── approval core, end to end with a drop-in channel plugin ───────────────────

_FAKE_PLUGIN = '''
from aria.channels.base import ChannelPlugin

SENT = []

class Plugin(ChannelPlugin):
    name = "fakechan"

    def send(self, text, to=None):
        SENT.append(("text", text, to))

    def send_approval(self, code, summary, to=None, expires_min=5):
        SENT.append((code, summary, to))
'''


@pytest.fixture
def fakechan(tmp_path, monkeypatch):
    from aria import channels
    d = tmp_path / "channels"
    d.mkdir()
    (d / "fakechan.py").write_text(_FAKE_PLUGIN)
    monkeypatch.setenv("ARIA_CHANNELS_DIR", str(d))
    monkeypatch.setenv("ARIA_NOTIFY_CHANNEL", "fakechan")
    monkeypatch.setattr(approval, "_POLL", 0.02)
    channels.reset_cache()
    assert channels.get("fakechan") is not None
    yield sys.modules["_aria_user_channel_fakechan"].SENT
    channels.reset_cache()
    sys.modules.pop("_aria_user_channel_fakechan", None)


def _answer_when_sent(sent: list, fn) -> threading.Thread:
    """Wait (in another thread) for the approval to be sent, then answer it."""
    def run():
        deadline = time.monotonic() + 5
        while not sent and time.monotonic() < deadline:
            time.sleep(0.01)
        if sent:
            fn(sent[0][0])
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def test_request_approved_by_button(fakechan, monkeypatch):
    monkeypatch.setattr(approval, "_timeout", lambda: 5.0)
    token = context.set_active("fakechan", "u1")
    try:
        t = _answer_when_sent(fakechan, lambda code: approval.answer(code, True, "fakechan", "u1"))
        assert approval.request("run: make") == (True, "approved")
    finally:
        context.reset(token)
    t.join(2)
    code, summary, to = fakechan[0]
    assert summary == "run: make" and to == "u1" and len(code) == 4


def test_request_denied_by_text_reply(fakechan, monkeypatch):
    monkeypatch.setattr(approval, "_timeout", lambda: 5.0)
    token = context.set_active("fakechan", "u1")
    replies: list = []
    try:
        t = _answer_when_sent(fakechan, lambda code: replies.append(
            approval.try_answer_text("fakechan", "u1", f"no {code}")))
        ok, why = approval.request("delete x")
    finally:
        context.reset(token)
    t.join(2)
    assert (ok, why) == (False, "denied by the user")
    assert replies == ["❌ Denied."]


def test_request_other_user_cannot_answer(fakechan, monkeypatch):
    monkeypatch.setattr(approval, "_timeout", lambda: 0.5)
    token = context.set_active("fakechan", "u1")
    replies: list = []
    try:
        t = _answer_when_sent(fakechan, lambda code: replies.append(
            approval.try_answer_text("fakechan", "intruder", f"yes {code}")))
        ok, why = approval.request("delete x")
    finally:
        context.reset(token)
    t.join(2)
    assert not ok and "not approved within" in why
    # not this user's request → not an answer at all (reaches the agent instead)
    assert replies == [None]


def test_request_from_task_goes_to_push_channel(fakechan, task_turn, monkeypatch):
    monkeypatch.setattr(approval, "_timeout", lambda: 5.0)
    t = _answer_when_sent(fakechan, lambda code: approval.try_answer_text(
        "fakechan", "anyone", f"yes {code}"))
    assert approval.request("push branch main to origin") == (True, "approved")
    t.join(2)
    assert fakechan[0][2] is None                  # broadcast, not a single user


def test_request_times_out(fakechan, monkeypatch):
    monkeypatch.setattr(approval, "_timeout", lambda: 0.2)
    token = context.set_active("fakechan", "u1")
    try:
        ok, why = approval.request("delete x")
    finally:
        context.reset(token)
    assert not ok and "not approved within" in why
    code = fakechan[0][0]
    assert approval._read(code)["status"] == "expired"
    assert approval.answer(code, True, "fakechan", "u1").startswith("No pending")


def test_shell_end_to_end_through_fake_channel(fakechan, sr, monkeypatch):
    monkeypatch.setattr(approval, "_timeout", lambda: 5.0)
    token = context.set_active("fakechan", "u1")
    try:
        t = _answer_when_sent(fakechan, lambda code: approval.try_answer_text(
            "fakechan", "u1", f"yes {code}"))
        assert sr.execute({"command": "rm -rf build/"}) == "RAN"
    finally:
        context.reset(token)
    t.join(2)
    assert fakechan[0][1] == "run: rm -rf build/"


def test_tasks_keep_old_behaviour_by_default(monkeypatch, asks):
    """Without ARIA_APPROVAL_TASKS, a scheduled task neither asks nor blocks:
    deletes run and shell refusals are instant, exactly as before approvals."""
    stub = asks(True)
    monkeypatch.setenv("ARIA_TASK_ID", "abc12345")
    monkeypatch.delenv("ARIA_APPROVAL_TASKS", raising=False)
    context.clear()
    assert approval.check("delete", "delete X") is None
    assert not approval.should_ask()
    from aria.tools import shell_run
    monkeypatch.setenv("ARIA_SHELL_UNATTENDED", "safe")
    out = shell_run.execute({"command": "rm -rf /tmp/nothing-here"})
    assert "Refused" in out and "approval" not in out
    assert not stub.summaries


def test_shell_kind_is_configurable(monkeypatch, asks, channel_turn):
    """ARIA_APPROVAL_REQUIRED without 'shell' → shell refusals don't ask."""
    from aria.tools import shell_run
    stub = asks(True)
    monkeypatch.setenv("ARIA_SHELL_UNATTENDED", "safe")
    monkeypatch.setenv("ARIA_APPROVAL_REQUIRED", "delete")
    out = shell_run.execute({"command": "rm -rf /tmp/nothing-here"})
    assert "Refused" in out and not stub.summaries
