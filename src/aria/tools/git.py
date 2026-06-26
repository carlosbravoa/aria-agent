"""
aria/tools/git.py — Git operations without shelling out by hand.

A focused, structured front-end to git so the agent doesn't have to assemble
shell strings (and so quoting/injection isn't a concern — every argument is
passed as a list, never through a shell). Covers the everyday loop: inspect
(status/diff/log/show/branch), stage, and commit; plus push/pull when asked.

Mutates the index/working tree, so it is NOT parallel-safe.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from aria.tools._env import build_env

DEFINITION = {
    "name": "git",
    "description": (
        "Run common git operations in a repository. Actions: status, diff "
        "(set staged=true for the index), log, show, branch (list), checkout "
        "(switch/create a branch), add (stage paths), commit (needs message; "
        "set add_all=true to stage everything first), push, pull. Prefer this "
        "over shell_run for git work."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["status", "diff", "log", "show", "branch", "checkout",
                         "add", "commit", "push", "pull"],
            },
            "path": {
                "type": "string",
                "description": "Repository directory (default: current directory).",
            },
            "message": {
                "type": "string",
                "description": "Commit message (required for commit).",
            },
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Files to stage (add) — defaults to all.",
            },
            "ref": {
                "type": "string",
                "description": "Branch/commit/path for diff, show, log, checkout.",
            },
            "staged": {
                "type": "boolean",
                "description": "For diff: show the staged (index) diff.",
            },
            "add_all": {
                "type": "boolean",
                "description": "For commit: stage all changes first (git add -A).",
            },
            "create": {
                "type": "boolean",
                "description": "For checkout: create the branch (git checkout -b).",
            },
            "limit": {
                "type": "integer",
                "description": "For log: number of commits (default 15).",
            },
        },
        "required": ["action"],
    },
}


def _git(root: Path, *git_args: str) -> str:
    cmd = ["git", "-C", str(root), *git_args]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                           env=build_env())
    except FileNotFoundError:
        return "[git] git is not installed."
    except subprocess.TimeoutExpired:
        return "[git] command timed out."
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    if r.returncode != 0:
        return f"[git error] {err or out or 'command failed'}"
    parts = [p for p in (out, err) if p]
    return "\n".join(parts) or "(ok, no output)"


def execute(args: dict) -> str:
    if not shutil.which("git"):
        return "[git] git is not installed."
    action = args.get("action", "")
    root = Path(args.get("path") or ".").expanduser()
    if not (root / ".git").exists() and action not in ("checkout",):
        # Allow git to resolve a parent repo, but warn if clearly not one.
        check = _git(root, "rev-parse", "--is-inside-work-tree")
        if check.startswith("[git"):
            return f"[git] {root} is not inside a git repository."

    if action == "status":
        return _git(root, "status", "--short", "--branch")

    if action == "diff":
        cmd = ["diff"]
        if args.get("staged"):
            cmd.append("--cached")
        if args.get("ref"):
            cmd.append(args["ref"])
        return _git(root, *cmd)

    if action == "log":
        limit = int(args.get("limit") or 15)
        cmd = ["log", f"-{limit}", "--oneline", "--decorate"]
        if args.get("ref"):
            cmd.append(args["ref"])
        return _git(root, *cmd)

    if action == "show":
        return _git(root, "show", "--stat", args.get("ref") or "HEAD")

    if action == "branch":
        return _git(root, "branch", "--all", "--verbose")

    if action == "checkout":
        ref = args.get("ref")
        if not ref:
            return "[git] 'ref' (branch name) is required for checkout."
        cmd = ["checkout"]
        if args.get("create"):
            cmd.append("-b")
        cmd.append(ref)
        return _git(root, *cmd)

    if action == "add":
        paths = args.get("paths") or ["-A"]
        return _git(root, "add", *paths) or "Staged."

    if action == "commit":
        message = (args.get("message") or "").strip()
        if not message:
            return "[git] 'message' is required for commit."
        if args.get("add_all"):
            staged = _git(root, "add", "-A")
            if staged.startswith("[git error]"):
                return staged
        return _git(root, "commit", "-m", message)

    if action == "push":
        return _git(root, "push")

    if action == "pull":
        return _git(root, "pull")

    return f"[git] Unknown action: {action}"
