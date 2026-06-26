"""
aria/tools/code_search.py — Fast code/text search across a directory tree.

Prefers ripgrep (`rg`) when available — fast, respects .gitignore, skips binary
files. Falls back to `git grep` inside a repo, then to a pure-Python walk so the
tool always works. Read-only, so a batch of searches may run concurrently.

Two actions:
  - search: regex/text match across files → `path:line: matched text`
  - files:  list files whose NAME matches a glob (find-by-name)
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from aria.tools._env import build_env

# Read-only — no shared local state. A batch of searches can run in parallel.
PARALLEL_SAFE = True

_MAX_RESULTS = 200
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__",
              ".mypy_cache", ".pytest_cache", "dist", "build", ".idea"}

DEFINITION = {
    "name": "code_search",
    "description": (
        "Search a codebase fast. action='search' finds a regex/text pattern "
        "across files and returns `path:line: text` matches (ripgrep-backed, "
        "respects .gitignore). action='files' lists files whose name matches a "
        "glob. Use this to locate code, symbols, usages, TODOs, or config before "
        "reading or editing — far cheaper than reading whole files."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["search", "files"],
                "description": "'search' for content, 'files' for filenames.",
            },
            "pattern": {
                "type": "string",
                "description": "Regex (search) or filename glob like '*.py' (files).",
            },
            "path": {
                "type": "string",
                "description": "Directory to search (default: current directory).",
            },
            "glob": {
                "type": "string",
                "description": "Restrict 'search' to files matching this glob "
                               "(e.g. '*.py'). Optional.",
            },
            "ignore_case": {
                "type": "boolean",
                "description": "Case-insensitive search (default false).",
            },
            "max_results": {
                "type": "integer",
                "description": f"Cap on matches returned (default {_MAX_RESULTS}).",
            },
        },
        "required": ["action", "pattern"],
    },
}


def execute(args: dict) -> str:
    action = args.get("action", "search")
    pattern = args.get("pattern", "")
    if not pattern:
        return "[code_search] 'pattern' is required."
    root = Path(args.get("path") or ".").expanduser()
    if not root.exists():
        return f"[code_search] Path not found: {root}"
    limit = int(args.get("max_results") or _MAX_RESULTS)

    if action == "files":
        return _find_files(root, pattern, limit)
    return _search(root, pattern, args.get("glob"),
                   bool(args.get("ignore_case")), limit)


def _find_files(root: Path, glob: str, limit: int) -> str:
    matches: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            if Path(fn).match(glob):
                matches.append(os.path.join(dirpath, fn))
                if len(matches) >= limit:
                    break
        if len(matches) >= limit:
            break
    if not matches:
        return f"[code_search] No files matching '{glob}' under {root}."
    head = f"{len(matches)} file(s) matching '{glob}':\n"
    return head + "\n".join(sorted(matches))


def _search(root: Path, pattern: str, glob: str | None,
            ignore_case: bool, limit: int) -> str:
    if shutil.which("rg"):
        out = _ripgrep(root, pattern, glob, ignore_case, limit)
        if out is not None:
            return out
    # Fallbacks: git grep inside a repo, then pure Python.
    if shutil.which("git") and (root / ".git").exists():
        out = _git_grep(root, pattern, glob, ignore_case, limit)
        if out is not None:
            return out
    return _python_grep(root, pattern, glob, ignore_case, limit)


def _ripgrep(root, pattern, glob, ignore_case, limit):
    cmd = ["rg", "--line-number", "--no-heading", "--color", "never",
           "--max-count", "50"]
    if ignore_case:
        cmd.append("--ignore-case")
    if glob:
        cmd += ["--glob", glob]
    cmd += ["--regexp", pattern, str(root)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                           env=build_env())
    except Exception:
        return None
    if r.returncode not in (0, 1):       # 1 = no matches (not an error)
        return None
    return _format_lines(r.stdout.splitlines(), pattern, root, limit)


def _git_grep(root, pattern, glob, ignore_case, limit):
    cmd = ["git", "-C", str(root), "grep", "--line-number", "--no-color"]
    if ignore_case:
        cmd.append("--ignore-case")
    cmd += ["-e", pattern]
    if glob:
        cmd += ["--", glob]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                           env=build_env())
    except Exception:
        return None
    if r.returncode not in (0, 1):
        return None
    return _format_lines(r.stdout.splitlines(), pattern, root, limit)


def _python_grep(root, pattern, glob, ignore_case, limit):
    try:
        rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        return f"[code_search] Bad regex: {exc}"
    hits: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            if glob and not Path(fn).match(glob):
                continue
            fp = os.path.join(dirpath, fn)
            try:
                with open(fp, "r", encoding="utf-8", errors="strict") as f:
                    for i, line in enumerate(f, 1):
                        if rx.search(line):
                            hits.append(f"{fp}:{i}:{line.rstrip()}")
                            if len(hits) >= limit:
                                return _join(hits, pattern, limit, capped=True)
            except (UnicodeDecodeError, OSError):
                continue                  # binary or unreadable — skip
    return _join(hits, pattern, limit)


def _format_lines(lines, pattern, root, limit):
    capped = len(lines) > limit
    return _join(lines[:limit], pattern, limit, capped=capped)


def _join(lines, pattern, limit, capped: bool = False) -> str:
    if not lines:
        return f"[code_search] No matches for '{pattern}'."
    body = "\n".join(lines)
    if capped:
        body += f"\n… (truncated at {limit} matches — narrow the pattern/path)"
    return body
