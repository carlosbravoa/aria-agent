"""
Characterization tests for the gog-backed tools (gmail, calendar, drive).

Pin the exact subprocess argv, env, timeout/kwargs and returned strings so the
shared-helper refactor (tools/_gog.py) is provably behaviour-preserving.
subprocess.run is patched on the subprocess module itself, so the tests are
agnostic to which module actually performs the call.
"""

from __future__ import annotations

import subprocess

import pytest

from aria.tools import calendar, drive, gmail
from aria.tools._env import build_env

HINT = ("\nHint: for headless/systemd use, set GOG_KEYRING_BACKEND=file "
        "and GOG_KEYRING_PASSWORD in ~/.aria/.env.")
NASTY = "it's a \"test\"; rm -rf ~ $(whoami) `id` & | > *"


class Recorder:
    def __init__(self, mode="ok", stdout="OUT\n", stderr="", rc=0):
        self.mode, self.stdout, self.stderr, self.rc = mode, stdout, stderr, rc
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if self.mode == "timeout":
            raise subprocess.TimeoutExpired(argv, kw.get("timeout"))
        if self.mode == "missing":
            raise FileNotFoundError(argv[0])
        if self.mode == "boom":
            raise RuntimeError("kaboom")
        return subprocess.CompletedProcess(argv, self.rc, self.stdout, self.stderr)


@pytest.fixture
def gog_env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".aria").mkdir(parents=True)
    (home / ".aria" / ".env").write_text("GOG_ACCOUNT=me@example.com\nGOG_MARK=1\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GOG_ACCOUNT", raising=False)
    for mod in (gmail, calendar, drive):
        monkeypatch.setattr(mod, "_CLI", "gog")
    return home


@pytest.fixture
def rec(monkeypatch, gog_env):
    r = Recorder()
    monkeypatch.setattr(subprocess, "run", r)
    return r


def _check_kwargs(rec, text=True):
    argv, kw = rec.calls[-1]
    expected = {"capture_output": True, "timeout": 30, "env": build_env()}
    if text:
        expected["text"] = True
    assert kw == expected
    assert kw["env"]["GOG_ACCOUNT"] == "me@example.com"
    return argv


# ── argv per action ───────────────────────────────────────────────────────────

GMAIL_CASES = [
    ({"action": "list"}, ["gog", "gmail", "search", "in:inbox", "--max", "10", "--json"]),
    ({"action": "list", "max_results": 3}, ["gog", "gmail", "search", "in:inbox", "--max", "3", "--json"]),
    ({"action": "search", "query": NASTY, "max_results": "0"},
     ["gog", "gmail", "search", NASTY, "--max", "1", "--json"]),
    ({"action": "read", "query": "abc 'def'"}, ["gog", "gmail", "get", "abc 'def'"]),
    ({"action": "send", "to": "a b@x.com", "subject": NASTY, "body": "line1\nline2 \"q\""},
     ["gog", "gmail", "send", "--to", "a b@x.com", "--subject", NASTY,
      "--body", "line1\nline2 \"q\""]),
    ({"action": "send", "to": "a@x.com", "subject": "s"},
     ["gog", "gmail", "send", "--to", "a@x.com", "--subject", "s", "--body", ""]),
    ({"action": "mark_read", "query": "t;1"},
     ["gog", "gmail", "thread", "modify", "t;1", "--remove", "UNREAD"]),
]

CAL_CASES = [
    ({"action": "list"}, ["gog", "calendar", "events", "primary", "--days", "7"]),
    ({"action": "list", "calendar_id": "my cal", "start": "2026-01-01T00:00:00", "days": 3},
     ["gog", "calendar", "events", "my cal", "--from", "2026-01-01T00:00:00"]),
    ({"action": "list", "end": "E", "days": 3}, ["gog", "calendar", "events", "primary", "--to", "E"]),
    ({"action": "list", "start": "S", "end": "E"},
     ["gog", "calendar", "events", "primary", "--from", "S", "--to", "E"]),
    ({"action": "list", "days": 2}, ["gog", "calendar", "events", "primary", "--days", "2"]),
    ({"action": "get", "event_id": "e 1"}, ["gog", "calendar", "get", "primary", "e 1", "--json"]),
    ({"action": "create", "summary": NASTY, "start": "S", "end": "E"},
     ["gog", "calendar", "create", "primary", "--summary", NASTY, "--from", "S", "--to", "E"]),
    ({"action": "create", "summary": "x", "start": "S", "end": "E", "description": "d 'q'",
      "attendees": "a@x,b@y", "location": "Room $1"},
     ["gog", "calendar", "create", "primary", "--summary", "x", "--from", "S", "--to", "E",
      "--description", "d 'q'", "--attendees", "a@x,b@y", "--location", "Room $1"]),
    ({"action": "update", "event_id": "ev"}, ["gog", "calendar", "update", "primary", "ev"]),
    ({"action": "update", "event_id": "ev", "summary": "s", "start": "S", "end": "E",
      "description": "d", "location": "l", "attendees": "a"},
     ["gog", "calendar", "update", "primary", "ev", "--summary", "s", "--from", "S", "--to", "E",
      "--description", "d", "--location", "l", "--attendees", "a"]),
    ({"action": "delete", "event_id": NASTY}, ["gog", "calendar", "delete", "primary", NASTY]),
    ({"action": "respond", "event_id": "ev", "status": "accepted"},
     ["gog", "calendar", "respond", "primary", "ev", "--status", "accepted"]),
]

DRIVE_CASES = [
    ({"action": "list"}, ["gog", "drive", "ls", "--max", "20"]),
    ({"action": "list", "parent_id": "p 1", "query": "mimeType='application/pdf'", "max_results": 5},
     ["gog", "drive", "ls", "--max", "5", "--parent", "p 1", "--query", "mimeType='application/pdf'"]),
    ({"action": "search", "query": NASTY}, ["gog", "drive", "search", NASTY, "--max", "20"]),
    ({"action": "get", "file_id": "f 'x'"}, ["gog", "drive", "get", "f 'x'"]),
    ({"action": "url", "file_id": "f1"}, ["gog", "drive", "url", "f1"]),
    ({"action": "upload", "path": "__WS__/up load.txt", "parent_id": "p;1"},
     ["gog", "drive", "upload", "__WS__/up load.txt", "--parent", "p;1"]),
    ({"action": "upload", "path": "__WS__/u.txt"}, ["gog", "drive", "upload", "__WS__/u.txt"]),
    ({"action": "download", "file_id": "f1", "path": "__WS__/d l.txt", "format": "pdf"},
     ["gog", "drive", "download", "f1", "--out", "__WS__/d l.txt", "--format", "pdf"]),
    ({"action": "download", "file_id": "f1", "path": "__WS__/d.txt"},
     ["gog", "drive", "download", "f1", "--out", "__WS__/d.txt"]),
    ({"action": "mkdir", "name": NASTY}, ["gog", "drive", "mkdir", NASTY]),
    ({"action": "mkdir", "name": "n", "parent_id": "p"}, ["gog", "drive", "mkdir", "n", "--parent", "p"]),
    ({"action": "rename", "file_id": "f1", "name": "new $name"},
     ["gog", "drive", "rename", "f1", "new $name"]),
    ({"action": "move", "file_id": "f1", "parent_id": "p 2"},
     ["gog", "drive", "move", "f1", "--parent", "p 2"]),
    ({"action": "delete", "file_id": "f`1`"}, ["gog", "drive", "delete", "f`1`"]),
]


@pytest.fixture
def ws(tmp_path, monkeypatch):
    w = tmp_path / "workspace"
    w.mkdir()
    monkeypatch.setenv("ARIA_WORKSPACE", str(w))
    for name in ("up load.txt", "u.txt"):
        (w / name).write_text("x")
    return str(w)


def _sub(obj, ws):
    if isinstance(obj, str):
        return obj.replace("__WS__", ws)
    if isinstance(obj, list):
        return [_sub(o, ws) for o in obj]
    if isinstance(obj, dict):
        return {k: _sub(v, ws) for k, v in obj.items()}
    return obj


@pytest.mark.parametrize("mod,args,argv", (
    [(gmail, a, v) for a, v in GMAIL_CASES]
    + [(calendar, a, v) for a, v in CAL_CASES]
    + [(drive, a, v) for a, v in DRIVE_CASES]))
def test_argv_env_and_success(rec, ws, mod, args, argv):
    args, argv = _sub(args, ws), _sub(argv, ws)
    rec.stdout = "  result text \n"
    out = mod.execute(args)
    assert len(rec.calls) == 1
    assert _check_kwargs(rec) == argv
    # gmail list/search: non-JSON output → _format_threads passes it through
    assert out == "result text"


def test_empty_output(rec):
    rec.stdout = "  \n"
    assert gmail.execute({"action": "read", "query": "t"}) == "(no output)"
    assert calendar.execute({"action": "get", "event_id": "e"}) == "(no output)"
    assert drive.execute({"action": "get", "file_id": "f"}) == "(no output)"


def test_gmail_list_formats_json(rec):
    rec.stdout = '{"threads": [{"id": "t1", "subject": "Hi", "from": "Bob"}]}'
    assert gmail.execute({"action": "list"}) == '1 thread(s):\n[t1] Bob — "Hi"'


# ── error paths ──────────────────────────────────────────────────────────────

ERR_CASES = [
    (gmail, {"action": "search", "query": "a 'b'"}, "gmail",
     "gog gmail search 'a '\"'\"'b'\"'\"'' --max 10 --json"),
    (gmail, {"action": "list"}, "gmail", "gog gmail search 'in:inbox' --max 10 --json"),
    (calendar, {"action": "create", "summary": "a b", "start": "S", "end": "E"}, "calendar",
     "gog calendar create primary --summary 'a b' --from S --to E"),
    (drive, {"action": "rename", "file_id": "f 1", "name": "x"}, "drive",
     "gog drive rename 'f 1' x"),
]


@pytest.mark.parametrize("mod,args,tag,cmd", ERR_CASES)
def test_nonzero_exit_keyring(monkeypatch, gog_env, mod, args, tag, cmd):
    r = Recorder(stdout="o", stderr=" keyring is locked \n", rc=3)
    monkeypatch.setattr(subprocess, "run", r)
    assert mod.execute(args) == f"[{tag} error] exit=3\ncmd: {cmd}\nkeyring is locked" + HINT


@pytest.mark.parametrize("mod,args,tag,cmd", ERR_CASES)
def test_nonzero_exit_plain(monkeypatch, gog_env, mod, args, tag, cmd):
    r = Recorder(stdout=" some out ", stderr="", rc=1)
    monkeypatch.setattr(subprocess, "run", r)
    assert mod.execute(args) == f"[{tag} error] exit=1\ncmd: {cmd}\nsome out"
    r2 = Recorder(stdout="", stderr="", rc=2)
    monkeypatch.setattr(subprocess, "run", r2)
    assert mod.execute(args) == f"[{tag} error] exit=2\ncmd: {cmd}\nno output"


@pytest.mark.parametrize("mod,args,tag,cmd", ERR_CASES)
def test_timeout(monkeypatch, gog_env, mod, args, tag, cmd):
    monkeypatch.setattr(subprocess, "run", Recorder(mode="timeout"))
    assert mod.execute(args) == f"[{tag} error] command timed out: {cmd}"


@pytest.mark.parametrize("mod,args,tag,cmd", ERR_CASES)
def test_generic_exception(monkeypatch, gog_env, mod, args, tag, cmd):
    monkeypatch.setattr(subprocess, "run", Recorder(mode="boom"))
    assert mod.execute(args) == f"[{tag} error] kaboom"


def test_missing_binary(monkeypatch, gog_env):
    monkeypatch.setattr(subprocess, "run", Recorder(mode="missing"))
    assert gmail.execute({"action": "read", "query": "t"}) == (
        "[gmail error] 'gog' not found in PATH. "
        "Ensure it is installed and GMAIL_CLI is set correctly in ~/.aria/.env")
    assert calendar.execute({"action": "get", "event_id": "e"}) == (
        "[calendar error] 'gog' not found in PATH. "
        "Ensure gog is installed and GMAIL_CLI is set in ~/.aria/.env")
    assert drive.execute({"action": "get", "file_id": "f"}) == (
        "[drive error] 'gog' not found. Ensure gog is installed.")


def test_missing_binary_uses_module_cli(monkeypatch, gog_env):
    monkeypatch.setattr(subprocess, "run", Recorder(mode="missing"))
    for mod in (gmail, calendar, drive):
        monkeypatch.setattr(mod, "_CLI", "mygog")
    assert "'mygog' not found" in gmail.execute({"action": "read", "query": "t"})
    assert "'mygog' not found" in calendar.execute({"action": "get", "event_id": "e"})
    assert "'mygog' not found" in drive.execute({"action": "get", "file_id": "f"})


def test_missing_account(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("GOG_ACCOUNT", raising=False)
    r = Recorder()
    monkeypatch.setattr(subprocess, "run", r)
    assert gmail.execute({"action": "read", "query": "t"}) == (
        "[gmail error] GOG_ACCOUNT is not set. Add GOG_ACCOUNT=you@gmail.com to ~/.aria/.env")
    assert calendar.execute({"action": "get", "event_id": "e"}) == (
        "[calendar error] GOG_ACCOUNT is not set. Add GOG_ACCOUNT=you@gmail.com to ~/.aria/.env")
    assert drive.execute({"action": "get", "file_id": "f"}) == (
        "[drive error] GOG_ACCOUNT not set. Add GOG_ACCOUNT=you@gmail.com to ~/.aria/.env")
    assert drive.execute({"action": "read", "file_id": "f"}) == (
        "[drive error] GOG_ACCOUNT not set in ~/.aria/.env")
    assert r.calls == []


def test_direct_run_signatures(rec):
    """Module-level _run(cmd) stays callable (tests monkeypatch it)."""
    assert gmail._run("gog gmail get 'a b'") == "OUT"
    assert calendar._run("gog calendar get primary x") == "OUT"
    assert drive._run("gog drive get x") == "OUT"
    assert drive._run("gog drive get x", capture_stdout=False) == "OUT"
    assert rec.calls[0][0] == ["gog", "gmail", "get", "a b"]


# ── validation messages (no subprocess) ──────────────────────────────────────

@pytest.mark.parametrize("mod,args,msg", [
    (gmail, {"action": "search"}, "[gmail] 'query' is required for search."),
    (gmail, {"action": "read"}, "[gmail] 'query' must contain a thread ID for 'read'."),
    (gmail, {"action": "send", "to": "a"}, "[gmail] 'to' and 'subject' are required for send."),
    (gmail, {"action": "mark_read"}, "[gmail] 'query' must contain a thread ID for 'mark_read'."),
    (gmail, {"action": "nope"}, "[gmail] Unknown action: nope"),
    (calendar, {"action": "get"}, "[calendar] 'event_id' is required for get."),
    (calendar, {"action": "create"}, "[calendar] 'summary' is required for create."),
    (calendar, {"action": "create", "summary": "s", "start": "S"},
     "[calendar] 'start' and 'end' are required for create."),
    (calendar, {"action": "update"}, "[calendar] 'event_id' is required for update."),
    (calendar, {"action": "delete"}, "[calendar] 'event_id' is required for delete."),
    (calendar, {"action": "respond"}, "[calendar] 'event_id' is required for respond."),
    (calendar, {"action": "respond", "event_id": "e"},
     "[calendar] 'status' (accepted|declined|tentative) is required for respond."),
    (calendar, {"action": "nope"}, "[calendar] Unknown action: nope"),
    (drive, {"action": "search"}, "[drive] 'query' is required for search."),
    (drive, {"action": "get"}, "[drive] 'file_id' is required for get."),
    (drive, {"action": "url"}, "[drive] 'file_id' is required for url."),
    (drive, {"action": "read"}, "[drive] 'file_id' is required for read."),
    (drive, {"action": "download"}, "[drive] 'file_id' is required for download."),
    (drive, {"action": "download", "file_id": "f"},
     "[drive] 'path' is required for download (local destination)."),
    (drive, {"action": "upload"}, "[drive] 'path' is required for upload."),
    (drive, {"action": "mkdir"}, "[drive] 'name' is required for mkdir."),
    (drive, {"action": "rename", "file_id": "f"}, "[drive] 'file_id' and 'name' are required for rename."),
    (drive, {"action": "move", "file_id": "f"}, "[drive] 'file_id' and 'parent_id' are required for move."),
    (drive, {"action": "delete"}, "[drive] 'file_id' is required for delete."),
    (drive, {"action": "nope"}, "[drive] Unknown action: nope"),
])
def test_validation_messages(rec, mod, args, msg):
    assert mod.execute(args) == msg
    assert rec.calls == []


# ── drive read (bytes mode, own subprocess call) ─────────────────────────────

def test_drive_read(monkeypatch, gog_env):
    r = Recorder(stdout=b"hello \xff", stderr=b"")
    monkeypatch.setattr(subprocess, "run", r)
    assert drive.execute({"action": "read", "file_id": "f 1", "format": "txt"}) == "hello �"
    assert _check_kwargs(r, text=False) == ["gog", "drive", "download", "f 1", "--out", "-",
                                            "--format", "txt"]
    drive.execute({"action": "read", "file_id": "f"})
    assert r.calls[-1][0] == ["gog", "drive", "download", "f", "--out", "-"]

    r.stdout = b"x" * 8001
    assert drive.execute({"action": "read", "file_id": "f"}) == (
        "x" * 8000 + "\n… [truncated — use download to save full file]")
    r.stdout = b""
    assert drive.execute({"action": "read", "file_id": "f"}) == "(empty file)"

    r2 = Recorder(stdout=b"", stderr=b" bad \n", rc=1)
    monkeypatch.setattr(subprocess, "run", r2)
    assert drive.execute({"action": "read", "file_id": "f"}) == "[drive error] bad"
    r2.stderr = b""
    assert drive.execute({"action": "read", "file_id": "f"}) == "[drive error] download failed"

    monkeypatch.setattr(subprocess, "run", Recorder(mode="timeout"))
    assert drive.execute({"action": "read", "file_id": "f"}) == "[drive error] read timed out"
    monkeypatch.setattr(subprocess, "run", Recorder(mode="missing"))
    # FileNotFoundError is caught by the generic handler: str(exc) == "gog"
    assert drive.execute({"action": "read", "file_id": "f"}) == "[drive error] gog"
