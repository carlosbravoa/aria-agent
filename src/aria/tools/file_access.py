"""
aria/tools/file_access.py — Read, write, append, list, patch, and delete local files.

Encoding options for write/append:
  - encoding="utf-8"   (default) — plain text content field
  - encoding="base64"  — content field is base64-encoded bytes.
    Use this when writing scripts or code that contains quotes, braces,
    or backslashes that would break JSON escaping.

Patch action:
  - Replaces the first occurrence of `old` with `new` in the file.
    Safer than rewriting a whole file when only a small section changes,
    and avoids truncation of large files.

Read with offset:
  - offset and limit params let you page through large files without
    returning the whole thing (avoiding context-window truncation).
"""

from __future__ import annotations

import base64
from pathlib import Path

DEFINITION = {
    "name": "file_access",
    "description": (
        "Read, write, append, patch, list, or delete local files. "
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
    path = Path(args["path"]).expanduser()

    match action:
        case "read":
            if not path.exists():
                return f"[file_access] Not found: {path}"
            text = path.read_text(encoding="utf-8")
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

            # Warn if the file is large so the model knows to use offset/limit
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
                size = path.stat().st_size
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
