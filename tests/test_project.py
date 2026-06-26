"""Tests for per-project context: conventions file + project-scoped notes (#3)."""

import os
from pathlib import Path

import pytest

from aria import project


@pytest.fixture
def proj(tmp_path):
    (tmp_path / ".git").mkdir()                 # marks the project root
    return tmp_path


def test_find_project_root_walks_up(proj):
    sub = proj / "a" / "b"
    sub.mkdir(parents=True)
    assert project.find_project_root(sub) == proj.resolve()


def test_find_project_root_falls_back_to_cwd(tmp_path):
    # no marker anywhere → returns the start dir itself
    assert project.find_project_root(tmp_path) == tmp_path.resolve()


def test_load_conventions_prefers_aria_md(proj):
    (proj / "AGENTS.md").write_text("agents")
    (proj / ".aria.md").write_text("aria wins")
    name, text = project.load_conventions(proj)
    assert name == ".aria.md" and text == "aria wins"


def test_load_conventions_none_when_absent(proj):
    assert project.load_conventions(proj) is None


def test_project_notes_roundtrip(proj, tmp_path):
    ws = tmp_path / "ws"
    (ws / "memory").mkdir(parents=True)
    project.append_note("test cmd is pytest -q", ws, start=proj)
    notes = project.load_notes(project.find_project_root(proj), ws)
    assert "pytest -q" in notes


def test_project_notes_isolated_by_root(tmp_path):
    ws = tmp_path / "ws"
    (ws / "memory").mkdir(parents=True)
    a = tmp_path / "proj_a"; (a / ".git").mkdir(parents=True)
    b = tmp_path / "proj_b"; (b / ".git").mkdir(parents=True)
    project.append_note("note for A", ws, start=a)
    project.append_note("note for B", ws, start=b)
    assert "note for A" in project.load_notes(a.resolve(), ws)
    assert "note for A" not in (project.load_notes(b.resolve(), ws) or "")


def test_learn_project_scope_writes_project_notes(minimal_env, tmp_path, monkeypatch):
    from aria.tools import learn
    from aria import config
    repo = tmp_path / "repo"; (repo / ".git").mkdir(parents=True)
    monkeypatch.chdir(repo)
    out = learn.execute({"procedure": "deploy with make ship", "scope": "project"})
    assert "project note" in out
    notes = project.load_notes(project.find_project_root(repo), config.workspace_dir())
    assert "make ship" in notes
