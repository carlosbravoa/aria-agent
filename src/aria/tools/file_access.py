"""
aria/tools/file_access.py — Read, write, append, list, patch, and delete local files.

Security model:
  All paths are resolved and checked against an allow-list before any operation.
  Sensitive paths (~/.ssh, ~/.gnupg, all of ~/.aria except the workspace, cloud
  credential dirs, /etc, /proc, …) are always blocked and can never be authorized.

  Read / list:
    - Workspace (always allowed)
    - ARIA_FILE_READ_DIRS  — colon-separated extra dirs
    - authorized_dirs.json — user-granted directories (runtime, no restart needed)

  Write / append / patch:
    - Workspace (always allowed)
    - ARIA_FILE_WRITE_DIRS — colon-separated extra dirs
    - authorized_dirs.json — user-granted directories with write permission

  Delete:
    - Workspace only, always. Never outside.

  Authorization flow:
    When a path is denied, the tool returns an authorization request message.
    The agent surfaces this to the user naturally ("I need access to X — shall I proceed?").
    If the user agrees, the agent calls file_access with action='authorize' to grant access.
    The original operation is then retried automatically.
    Blocked paths can NEVER be authorized regardless of user response.

Configure in ~/.aria/.env:
  ARIA_FILE_READ_DIRS=~/Documents:~/projects:~/code
  ARIA_FILE_WRITE_DIRS=~/projects
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

# ── Sensitive paths — always blocked, can never be authorized ─────────────────
_BLOCKED = [
    "~/.ssh",
    "~/.gnupg",
    # Aria's entire control plane: .env (secrets), authorized_dirs.json (the
    # approval store — agent must not grant itself access), tasks/ (the
    # autonomous job queue), tools/ (auto-loaded code = persistence), and runtime
    # state (.last_profile, browser_state.json, update_state.json). The workspace
    # (~/.aria/workspace) is carved out in _is_blocked so memory/soul/sessions
    # stay accessible.
    "~/.aria",
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

# Authorization request sentinel — agent uses this to detect a permission request
_AUTH_REQUEST = "[file_access:auth_required]"

# Authorized dirs file — written by authorize action, read at every _safe_path call
_MAX_READ_LINES = int(os.environ.get("ARIA_FILE_MAX_LINES", "500"))

# Authorized dirs file — written by authorize action, read at every _safe_path call
_AUTH_FILE = Path.home() / ".aria" / "authorized_dirs.json"

# Single-level undo store: the pre-edit content of each mutated file, keyed by a
# hash of its absolute path. Lets `action=undo` revert the last write/patch/edit/
# delete on a path. Internal — written directly, never through the allow-list.
_BACKUP_DIR = Path.home() / ".aria" / ".file_backups"


def _backup_file(path: Path) -> Path:
    import hashlib
    h = hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()
    return _BACKUP_DIR / f"{h}.json"


def _save_backup(path: Path) -> None:
    """Snapshot a file's current state before mutating it (best-effort)."""
    try:
        _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        if path.exists():
            data = base64.b64encode(path.read_bytes()).decode("ascii")
            payload = {"path": str(path), "existed": True, "content_b64": data}
        else:
            payload = {"path": str(path), "existed": False}
        bp = _backup_file(path)
        bp.write_text(json.dumps(payload), encoding="utf-8")
        bp.chmod(0o600)
    except Exception:
        pass  # never let backup failure block the actual operation


def _undo(path: Path) -> str:
    bp = _backup_file(path)
    if not bp.exists():
        return f"[file_access] No undo state for {path}."
    try:
        payload = json.loads(bp.read_text(encoding="utf-8"))
    except Exception as exc:
        return f"[file_access] Undo state unreadable: {exc}"
    if payload.get("existed"):
        path.write_bytes(base64.b64decode(payload["content_b64"]))
        result = f"[file_access] Reverted {path} to its previous content."
    else:
        # The last op created the file — undo removes it.
        if path.exists():
            path.unlink()
        result = f"[file_access] Removed {path} (it was newly created)."
    bp.unlink(missing_ok=True)
    return result


def _blocked_paths() -> list[Path]:
    return [Path(p).expanduser().resolve() for p in _BLOCKED]


def _workspace() -> Path:
    from aria import config
    return config.workspace_dir().resolve()


def _load_authorized() -> dict[str, str]:
    """Load user-granted directories from authorized_dirs.json.
    Returns {path_str: "read"|"write"}. Empty dict if file doesn't exist.
    """
    try:
        if _AUTH_FILE.exists():
            return json.loads(_AUTH_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_authorized(authorized: dict[str, str]) -> None:
    _AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    _AUTH_FILE.write_text(
        json.dumps(authorized, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _allow_list(env_var: str, defaults: list[str]) -> list[Path]:
    raw = os.environ.get(env_var, "")
    dirs = [d.strip() for d in raw.split(":") if d.strip()] if raw else defaults
    result = [_workspace()]
    for d in dirs:
        try:
            result.append(Path(d).expanduser().resolve())
        except Exception:
            pass
    return result


def _read_allow() -> list[Path]:
    base = _allow_list(
        "ARIA_FILE_READ_DIRS",
        [
            str(Path.home() / "Documents"),
            str(Path.home() / "Downloads"),
            str(Path.home() / "projects"),
        ],
    )
    # Add user-authorized dirs (both read and write grants allow reading)
    authorized = _load_authorized()
    for p, level in authorized.items():
        try:
            base.append(Path(p).expanduser().resolve())
        except Exception:
            pass
    return base


def _write_allow() -> list[Path]:
    base = _allow_list("ARIA_FILE_WRITE_DIRS", [])
    # Add user-authorized dirs with write permission
    authorized = _load_authorized()
    for p, level in authorized.items():
        if level == "write":
            try:
                base.append(Path(p).expanduser().resolve())
            except Exception:
                pass
    return base


def _is_blocked(p: Path) -> bool:
    """Return True if path is in a permanently blocked location.

    The agent's workspace lives under ~/.aria but is legitimately accessible
    (memory, soul, sessions), so it is carved out BEFORE the block check — this
    lets us block ~/.aria's control plane without also blocking the workspace.
    """
    p_str = str(p)
    try:
        ws = str(_workspace())
        if p_str == ws or p_str.startswith(ws + "/"):
            return False
    except Exception:
        pass
    for blocked in _blocked_paths():
        if p_str == str(blocked) or p_str.startswith(str(blocked) + "/"):
            return True
    return False


def _safe_path(raw: str, allow: list[Path], action: str = "") -> Path:
    """
    Resolve path and verify it:
      1. Not in a blocked sensitive directory (hard stop — no authorization possible).
      2. Within at least one allowed directory.
    Raises ValueError with structured message if blocked.
    Raises PermissionError with auth request if outside allow-list (but not blocked).
    """
    p = Path(raw).expanduser().resolve()
    p_str = str(p)

    # 1. Hard block — can never be authorized
    if _is_blocked(p):
        raise ValueError(f"Access denied — path is in a protected location: {p}")

    # 2. Must be within an allowed directory
    for allowed in allow:
        if p_str == str(allowed) or p_str.startswith(str(allowed) + "/"):
            return p

    # 3. Not in allow-list — request authorization
    # Find the appropriate parent directory to request access to
    parent = p if p.is_dir() else p.parent
    level  = "write" if action in ("write", "append", "patch", "edit",
                                   "replace_lines", "undo") else "read"
    raise PermissionError(
        f"{_AUTH_REQUEST} "
        f"path={parent} "
        f"level={level}"
    )


def _format_auth_request(parent: Path, level: str) -> str:
    """Format a user-facing authorization request message."""
    return (
        f"I need {level} access to `{parent}` to complete this request.\n"
        f"This directory is not currently in the authorized list.\n"
        f"Would you like to grant {level} access to `{parent}`?\n"
        f"(If yes, I'll remember this for future requests. "
        f"Sensitive system directories can never be authorized.)"
    )


DEFINITION = {
    "name": "file_access",
    "description": (
        "Read, write, append, patch, edit, replace_lines, list, delete, or undo "
        "local files. Operations are restricted to the workspace and configured "
        "directories. If a path is outside the allowed directories, the tool will "
        "ask the user for authorization — use action='authorize' once the user "
        "agrees. Use encoding='base64' for binary content. "
        "action='patch' replaces ONE unique string. action='edit' applies SEVERAL "
        "{old,new} replacements atomically in one call (prefer this for multi-spot "
        "changes — fewer round-trips). action='replace_lines' replaces a line range "
        "[start_line,end_line]. action='undo' reverts the last write/patch/edit/"
        "delete on a path. Use offset/limit for reading large files in chunks."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["read", "write", "append", "patch", "edit",
                         "replace_lines", "list", "delete", "undo", "authorize"],
                "description": (
                    "Operation to perform. "
                    "Use 'authorize' to grant access to a directory after the user agrees — "
                    "provide path and level ('read' or 'write')."
                ),
            },
            "edits": {
                "type": "array",
                "description": "For action='edit': a list of {old, new} replacements "
                               "applied in order; each 'old' must be unique. "
                               "All-or-nothing — no write if any fails.",
                "items": {
                    "type": "object",
                    "properties": {
                        "old": {"type": "string"},
                        "new": {"type": "string"},
                    },
                },
            },
            "start_line": {
                "type": "integer",
                "description": "For replace_lines: first line to replace (1-based).",
            },
            "end_line": {
                "type": "integer",
                "description": "For replace_lines: last line to replace (inclusive).",
            },
            "path": {
                "type": "string",
                "description": "File or directory path.",
            },
            "content": {
                "type": "string",
                "description": "Content for write/append.",
            },
            "encoding": {
                "type": "string",
                "enum": ["utf-8", "base64"],
                "description": "Content encoding. Use 'base64' for binary content.",
                "default": "utf-8",
            },
            "level": {
                "type": "string",
                "enum": ["read", "write"],
                "description": "Access level for authorize action. 'write' also grants read.",
                "default": "read",
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
                "description": "Line number to start reading from (1-based).",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of lines to return.",
            },
        },
        "required": ["action", "path"],
    },
}


def _decode_content(args: dict):
    """Return str for utf-8 content, or bytes for base64 (so binary writes work
    — the schema advertises base64 for binary, but decoding to utf-8 + write_text
    crashed on real binary data)."""
    content = args.get("content", "")
    if args.get("encoding") == "base64":
        return base64.b64decode(content)        # bytes
    return content                               # str


def execute(args: dict) -> str:
    action: str  = args["action"]
    raw_path: str = args.get("path", "")

    # ── Authorize action ──────────────────────────────────────────────────────
    if action == "authorize":
        return _do_authorize(raw_path, args.get("level", "read"))

    # ── Path validation ───────────────────────────────────────────────────────
    try:
        if action in ("read", "list"):
            path = _safe_path(raw_path, _read_allow(), action)
        elif action == "delete":
            path = _safe_path(raw_path, [_workspace()], action)
        else:
            path = _safe_path(raw_path, _write_allow(), action)

    except ValueError as e:
        # Hard block — no authorization possible
        return f"[file_access] {e}"

    except PermissionError as e:
        # Authorization request — parse and format for the agent
        msg = str(e)
        # Extract path and level from the structured message
        try:
            parts   = msg.replace(_AUTH_REQUEST, "").strip().split()
            p_part  = next(p for p in parts if p.startswith("path="))
            l_part  = next(p for p in parts if p.startswith("level="))
            req_path  = Path(p_part[5:])
            req_level = l_part[6:]
        except Exception:
            req_path  = Path(raw_path).expanduser().resolve().parent
            req_level = "read"
        return _format_auth_request(req_path, req_level)

    # ── File operations ───────────────────────────────────────────────────────
    match action:

        case "read":
            if not path.exists():
                return f"[file_access] Not found: {path}"
            text  = path.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines(keepends=True)
            total = len(lines)

            offset = args.get("offset")
            limit  = args.get("limit")

            if offset is not None or limit is not None:
                start = max(0, (offset or 1) - 1)
                end   = start + (limit or len(lines))
                lines = lines[start:end]
                return f"[lines {start+1}–{min(end, total)} of {total}]\n" + "".join(lines)

            if total > _MAX_READ_LINES:
                return (
                    f"[file_access] File has {total} lines. "
                    f"Returning first {_MAX_READ_LINES}. Use offset/limit to read more.\n"
                    + "".join(lines[:_MAX_READ_LINES])
                )
            return text

        case "write":
            path.parent.mkdir(parents=True, exist_ok=True)
            _save_backup(path)
            data = _decode_content(args)
            if isinstance(data, bytes):
                path.write_bytes(data)
            else:
                path.write_text(data, encoding="utf-8")
            return f"[file_access] Written: {path}"

        case "append":
            path.parent.mkdir(parents=True, exist_ok=True)
            _save_backup(path)
            data = _decode_content(args)
            if isinstance(data, bytes):
                with path.open("ab") as f:
                    f.write(data)
            else:
                with path.open("a", encoding="utf-8") as f:
                    f.write(data)
            return f"[file_access] Appended to: {path}"

        case "patch":
            old = args.get("old", "")
            new = args.get("new", "")
            if not old:
                return "[file_access] 'old' is required for patch."
            if not path.exists():
                return f"[file_access] Not found: {path}"
            text  = path.read_text(encoding="utf-8")
            count = text.count(old)
            if count == 0:
                return f"[file_access] String not found in {path} — no changes made."
            if count > 1:
                # Refuse an ambiguous patch instead of silently editing the first
                # of several matches (which could change the wrong line).
                return (f"[file_access] '{old[:50]}…' appears {count} times in {path}. "
                        "Provide a longer, unique string so the right occurrence is patched.")
            _save_backup(path)
            path.write_text(text.replace(old, new, 1), encoding="utf-8")
            return f"[file_access] Patched: {path}"

        case "edit":
            edits = args.get("edits") or []
            if not edits:
                return "[file_access] 'edits' (list of {old,new}) is required for edit."
            if not path.exists():
                return f"[file_access] Not found: {path}"
            work = path.read_text(encoding="utf-8")
            # Validate + apply on a working copy; write only if ALL succeed, so a
            # bad edit never leaves the file half-changed.
            for i, e in enumerate(edits):
                old = e.get("old", "")
                new = e.get("new", "")
                if not old:
                    return f"[file_access] edit #{i+1}: 'old' is required — no changes made."
                count = work.count(old)
                if count == 0:
                    return (f"[file_access] edit #{i+1}: '{old[:40]}…' not found "
                            "— no changes made.")
                if count > 1:
                    return (f"[file_access] edit #{i+1}: '{old[:40]}…' appears {count} "
                            "times — make it unique. No changes made.")
                work = work.replace(old, new, 1)
            _save_backup(path)
            path.write_text(work, encoding="utf-8")
            return f"[file_access] Applied {len(edits)} edit(s) to {path}"

        case "replace_lines":
            if not path.exists():
                return f"[file_access] Not found: {path}"
            try:
                start = int(args.get("start_line"))
                end   = int(args.get("end_line", start))
            except (TypeError, ValueError):
                return "[file_access] replace_lines needs integer start_line/end_line."
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
            n = len(lines)
            if start < 1 or end < start or start > n + 1:
                return (f"[file_access] Invalid range {start}-{end} for a "
                        f"{n}-line file.")
            content = args.get("content", "")
            if content == "":
                repl = []                            # pure deletion of the range
            else:
                body = content[:-1] if content.endswith("\n") else content
                repl = [ln + "\n" for ln in body.split("\n")]
            _save_backup(path)
            new_lines = lines[:start - 1] + repl + lines[min(end, n):]
            path.write_text("".join(new_lines), encoding="utf-8")
            return (f"[file_access] Replaced lines {start}-{min(end, n)} of {path} "
                    f"({len(repl)} new line(s)).")

        case "undo":
            return _undo(path)

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
                _save_backup(path)              # file deletes are undo-able
                path.unlink()
            return f"[file_access] Deleted: {path}"

        case _:
            return f"[file_access] Unknown action: {action}"


def _do_authorize(raw_path: str, level: str) -> str:
    """
    Grant access to a directory. Called after the user explicitly agrees.
    Blocked paths can never be authorized — this is enforced here regardless
    of what the agent or user says.
    """
    if not raw_path:
        return "[file_access] 'path' is required for authorize."

    p = Path(raw_path).expanduser().resolve()

    # Hard safety check — blocked paths can NEVER be authorized
    if _is_blocked(p):
        return (
            f"[file_access] Cannot authorize access to `{p}` — "
            f"this is a protected system location."
        )

    level = level.lower().strip()
    if level not in ("read", "write"):
        level = "read"

    authorized = _load_authorized()
    authorized[str(p)] = level
    _save_authorized(authorized)

    level_desc = "read and write" if level == "write" else "read-only"
    return (
        f"[file_access] Access granted: {level_desc} access to `{p}`.\n"
        f"This will be remembered for future sessions."
    )
