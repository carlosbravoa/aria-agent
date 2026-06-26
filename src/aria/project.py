"""
aria/project.py — Per-project (per-repository) context.

Two things make Aria feel "at home" in a codebase:
  1. A conventions file the repo ships (`.aria.md`, `AGENTS.md`, or `CLAUDE.md`)
     — loaded read-only and injected into the system prompt as Project Context.
  2. Project-scoped notes — operational knowledge the agent learns about THIS
     repo (test command, deploy steps, gotchas), kept separate from global memory
     so different projects don't bleed into each other. Stored under the workspace,
     keyed by a hash of the project root, and injected when working in that root.

Project root is resolved by walking up from the cwd to the first directory that
holds a `.git` dir or a conventions file; failing that, the cwd itself.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

# Checked in order; the first that exists wins as the conventions file.
CONVENTION_FILES = (".aria.md", "AGENTS.md", "CLAUDE.md")
_MAX_CONV_CHARS = 8000
_MAX_NOTES_CHARS = 4000
_ROOT_MARKERS = (".git", *CONVENTION_FILES)


def find_project_root(start: str | os.PathLike | None = None) -> Path:
    """Nearest ancestor (incl. start) with a .git or conventions file; else cwd."""
    try:
        here = Path(start or os.getcwd()).resolve()
    except OSError:
        return Path.cwd()
    for d in (here, *here.parents):
        if any((d / m).exists() for m in _ROOT_MARKERS):
            return d
    return here


def load_conventions(root: Path) -> tuple[str, str] | None:
    """Return (filename, text) of the project's conventions file, or None."""
    for name in CONVENTION_FILES:
        f = root / name
        if f.is_file():
            try:
                text = f.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            if not text:
                continue
            if len(text) > _MAX_CONV_CHARS:
                text = text[:_MAX_CONV_CHARS] + "\n…[truncated]"
            return name, text
    return None


def notes_path(root: Path, workspace_root: Path) -> Path:
    """Per-project notes file under the workspace, keyed by the project root."""
    h = hashlib.sha1(str(root).encode("utf-8")).hexdigest()[:16]
    return workspace_root / "memory" / "project_notes" / f"{h}.md"


def load_notes(root: Path, workspace_root: Path) -> str | None:
    p = notes_path(root, workspace_root)
    if not p.is_file():
        return None
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    # Drop the bookkeeping header line (the recorded project path).
    lines = [ln for ln in text.splitlines() if not ln.startswith("<!--")]
    body = "\n".join(lines).strip()
    if not body:
        return None
    if len(body) > _MAX_NOTES_CHARS:
        body = body[-_MAX_NOTES_CHARS:]
    return body


def append_note(note: str, workspace_root: Path,
                start: str | os.PathLike | None = None) -> Path:
    """Append a project-scoped note for the cwd's project. Returns the file path."""
    root = find_project_root(start)
    p = notes_path(root, workspace_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    header = f"<!-- project: {root} -->\n"
    existing = p.read_text(encoding="utf-8") if p.exists() else header
    if not existing.startswith("<!--"):
        existing = header + existing
    if not existing.endswith("\n"):
        existing += "\n"
    p.write_text(existing + f"- {note.strip()}\n", encoding="utf-8")
    p.chmod(0o600)
    return p
