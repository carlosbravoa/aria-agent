"""Tests for file_access multi-edit, line-range edit, and undo (roadmap #2)."""

from aria import config
from aria.tools import file_access as fa


def _wf(minimal_env, name="demo.py", body="a = 1\nb = 2\nc = 3\n"):
    """Create a file inside the always-writable workspace and return its path."""
    p = config.workspace_dir() / name
    fa.execute({"action": "write", "path": str(p), "content": body})
    return p


def test_multi_edit_applies_all(minimal_env):
    p = _wf(minimal_env)
    out = fa.execute({"action": "edit", "path": str(p), "edits": [
        {"old": "a = 1", "new": "a = 10"},
        {"old": "c = 3", "new": "c = 30"},
    ]})
    assert "Applied 2" in out
    assert p.read_text() == "a = 10\nb = 2\nc = 30\n"


def test_multi_edit_is_atomic_on_failure(minimal_env):
    p = _wf(minimal_env)
    out = fa.execute({"action": "edit", "path": str(p), "edits": [
        {"old": "b = 2", "new": "b = 20"},
        {"old": "NOPE", "new": "x"},          # second edit fails
    ]})
    assert "not found" in out
    assert p.read_text() == "a = 1\nb = 2\nc = 3\n"   # nothing changed


def test_multi_edit_rejects_ambiguous(minimal_env):
    p = _wf(minimal_env, body="x\nx\n")
    out = fa.execute({"action": "edit", "path": str(p),
                      "edits": [{"old": "x", "new": "y"}]})
    assert "appears 2 times" in out
    assert p.read_text() == "x\nx\n"


def test_replace_lines(minimal_env):
    p = _wf(minimal_env)
    out = fa.execute({"action": "replace_lines", "path": str(p),
                      "start_line": 2, "end_line": 2, "content": "b = 99"})
    assert "Replaced lines 2-2" in out
    assert p.read_text() == "a = 1\nb = 99\nc = 3\n"


def test_replace_lines_deletes_range_when_empty(minimal_env):
    p = _wf(minimal_env)
    fa.execute({"action": "replace_lines", "path": str(p),
                "start_line": 2, "end_line": 2, "content": ""})
    assert p.read_text() == "a = 1\nc = 3\n"


def test_replace_lines_rejects_bad_range(minimal_env):
    p = _wf(minimal_env)
    out = fa.execute({"action": "replace_lines", "path": str(p),
                      "start_line": 9, "end_line": 9, "content": "x"})
    assert "Invalid range" in out


def test_undo_reverts_last_edit(minimal_env):
    p = _wf(minimal_env)
    fa.execute({"action": "patch", "path": str(p), "old": "b = 2", "new": "b = 222"})
    assert "b = 222" in p.read_text()
    out = fa.execute({"action": "undo", "path": str(p)})
    assert "Reverted" in out
    assert p.read_text() == "a = 1\nb = 2\nc = 3\n"


def test_undo_removes_newly_created_file(minimal_env):
    p = config.workspace_dir() / "fresh.txt"
    fa.execute({"action": "write", "path": str(p), "content": "hi"})
    assert p.exists()
    out = fa.execute({"action": "undo", "path": str(p)})
    assert "Removed" in out
    assert not p.exists()


def test_delete_then_undo_restores(minimal_env):
    p = _wf(minimal_env, name="del.txt", body="keep me\n")
    fa.execute({"action": "delete", "path": str(p)})
    assert not p.exists()
    fa.execute({"action": "undo", "path": str(p)})
    assert p.exists() and p.read_text() == "keep me\n"


def test_undo_with_no_state(minimal_env):
    p = config.workspace_dir() / "never_touched.txt"
    assert "No undo state" in fa.execute({"action": "undo", "path": str(p)})
