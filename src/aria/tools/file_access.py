"""
aria/tools/file_access.py — Read, write, append, list, patch, and delete local files.

Security model:
  All paths are resolved and checked against an allow-list before any operation.
  Sensitive paths (~/.ssh, ~/.gnupg, ~/.aria/.env, etc.) are always blocked.

  Read / list:
    - Workspace (always allowed)
    - ARIA_FILE_READ_DIRS  — colon-separated extra dirs (default: ~/Documents:~/Downloads:~/projects)

  Write / append / patch:
    - Workspace (always allowed)
    - ARIA_FILE_WRITE_DIRS — colon-separated extra dirs (default: none)

  Delete:
    - Workspace only, always. Never outside.

Configure in ~/.aria/.env:
  ARIA_FILE_READ_DIRS=~/Documents:~/projects:~/code
  ARIA_FILE_WRITE_DIRS=~/projects
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

# ── Sensitive paths — always blocked regardless of allow-list ──────────────────
_BLOCKED = [
    "~/.ssh",
    "~/.gnupg",
    "~/.aria/.env",
    "~/.config/gogcli",
    "~/.aws",
    "~/.azure",
    "~/.gcloud",
    "~/.netrc",
    "/etc",
    "/proc",
    "/sys",
    "/dev",
    "/boot",
]


def _blocked_paths() -> list[Path]:
    return [Path(p).expanduser().resolve() for p in _BLOCKED]


def _workspace() -> Path:
    from aria import config
    return config.workspace_dir().resolve()


def _allow_list(env_var: str, defaults: list[str]) -> list[Path]:
    raw = os.environ.get(env_var, "")
    dirs = [d.strip() for d in raw.split(":") if d.strip()] if raw else defaults
    result = [_workspace()]  # workspace always included
    for d in dirs:
        try:
            result.append(Path(d).expanduser().resolve())
        except Exception:
            pass
    return result


def _read_allow() -> list[Path]:
    return _allow_list(
        "ARIA_FILE_READ_DIRS",
        [
            str(Path.home() / "Documents"),
            str(Path.home() / "Downloads"),
            str(Path.home() / "projects"),
        ],
    )


def _write_allow() -> list[Path]:
    return _allow_list("ARIA_FILE_WRITE_DIRS", [])


def _safe_path(raw: str, allow: list[Path]) -> Path:
    """
    Resolve path and verify it:
      1. Does not escape into a blocked sensitive directory.
      2. Falls within at least one allowed directory.
    Raises ValueError with a clear message if either check fails.
    """
    p = Path(raw).expanduser().resolve()
    p_str = str(p)

    # 1. Block sensitive paths
    for blocked in _blocked_paths():
        if p_str == str(blocked) or p_str.startswith(str(blocked) + "/"):
            raise ValueError(f"Access denied — path is in a protected location: {p}")

    # 2. Must be within an allowed directory
    for allowed in allow:
        if p_str == str(allowed) or p_str.startswith(str(allowed) + "/"):
            return p

    allowed_str = ", ".join(str(a) for a in allow)
    raise ValueError(
        f"Access denied — path is outside allowed directories.\n"
        f"  Path: {p}\n"
        f"  Allowed: {allowed_str}\n"
        f"  To expand access, set ARIA_FILE_READ_DIRS or ARIA_FILE_WRITE_DIRS in ~/.aria/.env"
    )


DEFINITION = {
    "name": "file_access",
    "description": (
        "Read, write, append, patch, list, or delete local files. "
        "Operations are restricted to the workspace and configured directories "
        "(ARIA_FILE_READ_DIRS, ARIA_FILE_WRITE_DIRS in ~/.aria/.env). "
        "Use encoding='base64' for write/append when content contains special characters. "
        "Use action='patch' to replace a specific string in a file without rewriting it. "
        "Use offset/limit for reading large files in chunks."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["read", "write", "append", "patch", "list", "delete"],
                "description": "Operation to perform.",
            },
            "path": {
                "type": "string",
                "description": "File or directory path.",
            },
            "content": {
                "type": "string",
                "description": (
                    "Content for write/append. "
                    "Plain text by default; base64-encoded if encoding='base64'."
                ),
            },
            "encoding": {
                "type": "string",
                "enum": ["utf-8", "base64"],
                "description": "Content encoding. Use 'base64' for code with special chars.",
                "default": "utf-8",
            },
            "old": {
                "type": "string",
                "description": "String to find and replace (for action='patch').",
            },
            "new": {
                "type": "string",
                "description": "Replacement string (for action='patch').",
            },
            "offset": {
                "type": "integer",
                "description": "Line number to start reading from (1-based, for action='read').",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of lines to return (for action='read').",
            },
        },
        "required": ["action", "path"],
    },
}


def _decode_content(args: dict) -> str:
    content = args.get("content", "")
    if args.get("encoding") == "base64":
        return base64.b64decode(content).decode("utf-8")
    return content


def execute(args: dict) -> str:
    action: str = args["action"]
    raw_path: str = args.get("path", "")

    try:
        if action in ("read", "list"):
            path = _safe_path(raw_path, _read_allow())
        elif action == "delete":
            # Delete restricted to workspace only — never outside
            path = _safe_path(raw_path, [_workspace()])
        else:
            # write, append, patch
            path = _safe_path(raw_path, _write_allow())
    except ValueError as e:
        return f"[file_access] {e}"

    match action:

        case "read":
            if not path.exists():
                return f"[file_access] Not found: {path}"
            text = path.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines(keepends=True)
            total = len(lines)

            offset = args.get("offset")
            limit  = args.get("limit")

            if offset is not None or limit is not None:
                start = max(0, (offset or 1) - 1)
                end   = start + (limit or len(lines))
                lines = lines[start:end]
                header = f"[lines {start+1}–{min(end, total)} of {total}]\n"
                return header + "".join(lines)

            if total > 300:
                return (
                    f"[file_access] File has {total} lines. "
                    f"Returning first 300. Use offset/limit to read more.\n"
                    + "".join(lines[:300])
                )
            return text

        case "write":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_decode_content(args), encoding="utf-8")
            return f"[file_access] Written: {path}"

        case "append":
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(_decode_content(args))
            return f"[file_access] Appended to: {path}"

        case "patch":
            old = args.get("old", "")
            new = args.get("new", "")
            if not old:
                return "[file_access] 'old' is required for patch."
            if not path.exists():
                return f"[file_access] Not found: {path}"
            text = path.read_text(encoding="utf-8")
            if old not in text:
                return f"[file_access] String not found in {path} — no changes made."
            patched = text.replace(old, new, 1)
            path.write_text(patched, encoding="utf-8")
            return f"[file_access] Patched: {path}"

        case "list":
            if not path.exists():
                return f"[file_access] Not found: {path}"
            if path.is_file():
                size  = path.stat().st_size
                lines = sum(1 for _ in path.open(encoding="utf-8", errors="replace"))
                return f"{path}  ({lines} lines, {size} bytes)"
            entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name))
            return "\n".join(
                f"{'📁' if e.is_dir() else '📄'} {e.name}" for e in entries
            )

        case "delete":
            if not path.exists():
                return f"[file_access] Not found: {path}"
            if path.is_dir():
                import shutil
                shutil.rmtree(path)
            else:
                path.unlink()
            return f"[file_access] Deleted: {path}"

        case _:
            return f"[file_access] Unknown action: {action}"
