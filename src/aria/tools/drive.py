"""
aria/tools/drive.py — Google Drive via the gogcli (gog) CLI.

Command syntax reference:
  gog drive ls [--parent <folderId>] [--max N] [--query <query>]
  gog drive search "<text>" [--max N]
  gog drive get <fileId>
  gog drive url <fileId>
  gog drive download <fileId> [--format pdf|docx|pptx|xlsx] [--out <path>]
  gog drive upload <path> [--parent <folderId>]
  gog drive mkdir "<name>" [--parent <folderId>]
  gog drive rename <fileId> "<new name>"
  gog drive move <fileId> --parent <folderId>
  gog drive delete <fileId>

Required env vars in ~/.aria/.env:
  GOG_ACCOUNT=you@gmail.com
  GMAIL_CLI=gog   (same binary used for Drive)
  GOG_KEYRING_BACKEND=file
  GOG_KEYRING_PASSWORD=your-passphrase
"""

from __future__ import annotations

import os
import shlex
import subprocess

from aria.tools._env import build_env

_CLI = os.getenv("GMAIL_CLI", "gog")

DEFINITION = {
    "name": "drive",
    "description": (
        "Access and manage Google Drive files and folders via the gog CLI. "
        "Actions: list (browse a folder), search (find files by name/content), "
        "get (file metadata), url (shareable link), read (download text content of Docs/Sheets/text files), "
        "download (save to local path), upload (upload a local file), "
        "mkdir (create folder), rename, move, delete."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "search", "get", "url", "read", "download",
                         "upload", "mkdir", "rename", "move", "delete"],
                "description": "Drive operation to perform.",
            },
            "query": {
                "type": "string",
                "description": "Search query (for search and list actions). "
                               "For list, this is a Drive query e.g. \"mimeType='application/pdf'\". "
                               "For search, plain text like 'Q3 report'.",
            },
            "file_id": {
                "type": "string",
                "description": "Drive file or folder ID. Required for get, url, read, download, rename, move, delete.",
            },
            "parent_id": {
                "type": "string",
                "description": "Parent folder ID. Used by list (browse folder), upload, mkdir, move.",
            },
            "path": {
                "type": "string",
                "description": "Local file path. Required for upload. Used as destination for download.",
            },
            "name": {
                "type": "string",
                "description": "Name for mkdir or new name for rename.",
            },
            "format": {
                "type": "string",
                "enum": ["pdf", "docx", "pptx", "xlsx", "txt", "csv", "md"],
                "description": "Export format for download/read of Google Docs, Sheets, Slides.",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum results to return for list/search. Default: 20.",
                "default": 20,
            },
        },
        "required": ["action"],
    },
}


def _run(cmd: str, capture_stdout: bool = True) -> str:
    env = build_env()
    if "GOG_ACCOUNT" not in env:
        return (
            "[drive error] GOG_ACCOUNT not set. "
            "Add GOG_ACCOUNT=you@gmail.com to ~/.aria/.env"
        )
    try:
        result = subprocess.run(
            shlex.split(cmd),
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        out = result.stdout.strip()
        err = result.stderr.strip()
        if result.returncode != 0:
            detail = err or out or "no output"
            return f"[drive error] exit={result.returncode}\ncmd: {cmd}\n{detail}"
        return out or "(no output)"
    except FileNotFoundError:
        return f"[drive error] '{_CLI}' not found. Ensure gog is installed."
    except subprocess.TimeoutExpired:
        return f"[drive error] command timed out: {cmd}"
    except Exception as exc:
        return f"[drive error] {exc}"


def execute(args: dict) -> str:
    action  = args["action"]
    file_id = shlex.quote(args["file_id"]) if args.get("file_id") else ""
    parent  = args.get("parent_id", "")
    path    = args.get("path", "")
    name    = args.get("name", "")
    fmt     = args.get("format", "")
    n       = int(args.get("max_results", 20))
    query   = args.get("query", "")

    match action:

        case "list":
            cmd = f"{_CLI} drive ls --max {n}"
            if parent:
                cmd += f" --parent {shlex.quote(parent)}"
            if query:
                cmd += f" --query {shlex.quote(query)}"
            return _run(cmd)

        case "search":
            if not query:
                return "[drive] 'query' is required for search."
            return _run(f"{_CLI} drive search {shlex.quote(query)} --max {n}")

        case "get":
            if not file_id:
                return "[drive] 'file_id' is required for get."
            return _run(f"{_CLI} drive get {file_id}")

        case "url":
            if not file_id:
                return "[drive] 'file_id' is required for url."
            return _run(f"{_CLI} drive url {file_id}")

        case "read":
            # Download text content to stdout for the agent to read directly.
            # Best for Google Docs, plain text, markdown files.
            if not file_id:
                return "[drive] 'file_id' is required for read."
            env = build_env()
            if "GOG_ACCOUNT" not in env:
                return "[drive error] GOG_ACCOUNT not set in ~/.aria/.env"
            cmd_parts = [_CLI, "drive", "download", args["file_id"], "--out", "-"]
            if fmt:
                cmd_parts += ["--format", fmt]
            try:
                result = subprocess.run(
                    cmd_parts,
                    capture_output=True,
                    timeout=30,
                    env=env,
                )
                if result.returncode != 0:
                    err = result.stderr.decode(errors="replace").strip()
                    return f"[drive error] {err or 'download failed'}"
                content = result.stdout.decode(errors="replace")
                if len(content) > 8000:
                    content = content[:8000] + "\n… [truncated — use download to save full file]"
                return content or "(empty file)"
            except subprocess.TimeoutExpired:
                return "[drive error] read timed out"
            except Exception as exc:
                return f"[drive error] {exc}"

        case "download":
            if not file_id:
                return "[drive] 'file_id' is required for download."
            if not path:
                return "[drive] 'path' is required for download (local destination)."
            cmd = f"{_CLI} drive download {file_id} --out {shlex.quote(path)}"
            if fmt:
                cmd += f" --format {shlex.quote(fmt)}"
            result = _run(cmd)
            return result or f"[drive] Downloaded to {path}"

        case "upload":
            if not path:
                return "[drive] 'path' is required for upload."
            cmd = f"{_CLI} drive upload {shlex.quote(path)}"
            if parent:
                cmd += f" --parent {shlex.quote(parent)}"
            return _run(cmd)

        case "mkdir":
            if not name:
                return "[drive] 'name' is required for mkdir."
            cmd = f"{_CLI} drive mkdir {shlex.quote(name)}"
            if parent:
                cmd += f" --parent {shlex.quote(parent)}"
            return _run(cmd)

        case "rename":
            if not file_id or not name:
                return "[drive] 'file_id' and 'name' are required for rename."
            return _run(f"{_CLI} drive rename {file_id} {shlex.quote(name)}")

        case "move":
            if not file_id or not parent:
                return "[drive] 'file_id' and 'parent_id' are required for move."
            return _run(f"{_CLI} drive move {file_id} --parent {shlex.quote(parent)}")

        case "delete":
            if not file_id:
                return "[drive] 'file_id' is required for delete."
            return _run(f"{_CLI} drive delete {file_id}")

        case _:
            return f"[drive] Unknown action: {action}"
