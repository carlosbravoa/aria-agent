"""Tests for the code_search and git tools (offline, use a tmp repo)."""

import subprocess
import pytest

from aria.tools import code_search, git


# ── code_search ───────────────────────────────────────────────────────────────

def _seed_tree(tmp_path):
    (tmp_path / "a.py").write_text("def hello():\n    return 'world'\n")
    (tmp_path / "b.py").write_text("x = 1  # TODO: fix\nprint(x)\n")
    (tmp_path / "notes.md").write_text("a markdown TODO line\n")
    sub = tmp_path / "pkg"
    sub.mkdir()
    (sub / "c.py").write_text("def hello_again():\n    pass\n")
    return tmp_path


def test_code_search_finds_content(tmp_path):
    _seed_tree(tmp_path)
    out = code_search.execute({"action": "search", "pattern": "def hello",
                               "path": str(tmp_path)})
    assert "a.py" in out and "c.py" in out
    assert "b.py" not in out


def test_code_search_glob_restricts(tmp_path):
    _seed_tree(tmp_path)
    out = code_search.execute({"action": "search", "pattern": "TODO",
                               "path": str(tmp_path), "glob": "*.py"})
    assert "b.py" in out
    assert "notes.md" not in out          # excluded by *.py glob


def test_code_search_no_match(tmp_path):
    _seed_tree(tmp_path)
    out = code_search.execute({"action": "search", "pattern": "zzzNotThere",
                               "path": str(tmp_path)})
    assert "No matches" in out


def test_code_search_files_by_name(tmp_path):
    _seed_tree(tmp_path)
    out = code_search.execute({"action": "files", "pattern": "*.py",
                               "path": str(tmp_path)})
    assert "a.py" in out and "c.py" in out
    assert "notes.md" not in out


def test_code_search_requires_pattern(tmp_path):
    out = code_search.execute({"action": "search", "pattern": "", "path": str(tmp_path)})
    assert "required" in out


# ── git ───────────────────────────────────────────────────────────────────────

def _init_repo(tmp_path):
    def run(*a):
        subprocess.run(["git", *a], cwd=tmp_path, check=True,
                       capture_output=True, env={"HOME": str(tmp_path),
                       "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                       "PATH": "/usr/bin:/bin"})
    run("init", "-q")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (tmp_path / "f.txt").write_text("one\n")
    run("add", "-A")
    run("commit", "-qm", "initial")
    return tmp_path


@pytest.fixture
def repo(tmp_path):
    return _init_repo(tmp_path)


def test_git_status_and_log(repo):
    (repo / "f.txt").write_text("one\ntwo\n")
    status = git.execute({"action": "status", "path": str(repo)})
    assert "f.txt" in status
    log = git.execute({"action": "log", "path": str(repo), "limit": 5})
    assert "initial" in log


def test_git_diff_shows_change(repo):
    (repo / "f.txt").write_text("one\ntwo\n")
    out = git.execute({"action": "diff", "path": str(repo)})
    assert "+two" in out


def test_git_commit_requires_message(repo):
    out = git.execute({"action": "commit", "path": str(repo)})
    assert "required" in out


def test_git_add_then_commit(repo):
    (repo / "g.txt").write_text("new file\n")
    git.execute({"action": "add", "path": str(repo), "paths": ["g.txt"]})
    out = git.execute({"action": "commit", "path": str(repo), "message": "add g"})
    # commit succeeds (mentions the branch / file count) and isn't an error
    assert not out.startswith("[git error]")
    log = git.execute({"action": "log", "path": str(repo), "limit": 5})
    assert "add g" in log


def test_git_unknown_action(repo):
    assert "Unknown action" in git.execute({"action": "frobnicate", "path": str(repo)})


def test_git_outside_repo(tmp_path):
    out = git.execute({"action": "status", "path": str(tmp_path)})
    assert "not inside a git repository" in out or "[git" in out
