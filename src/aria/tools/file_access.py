"""
tools/file_access.py — Read, write, list, and delete local files.
"""

from pathlib import Path

DEFINITION = {
    "name": "file_access",
    "description": "Read, write, append, list, or delete local files and directories.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["read", "write", "append", "list", "delete"],
                "description": "Operation to perform.",
            },
            "path": {"type": "string", "description": "File or directory path."},
            "content": {
                "type": "string",
                "description": "Content to write/append (required for write/append).",
            },
        },
        "required": ["action", "path"],
    },
}


def execute(args: dict) -> str:
    action: str = args["action"]
    path = Path(args["path"]).expanduser()

    match action:
        case "read":
            if not path.exists():
                return f"[file_access] Not found: {path}"
            return path.read_text(encoding="utf-8")

        case "write":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(args.get("content", ""), encoding="utf-8")
            return f"[file_access] Written: {path}"

        case "append":
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(args.get("content", ""))
            return f"[file_access] Appended to: {path}"

        case "list":
            if not path.exists():
                return f"[file_access] Not found: {path}"
            if path.is_file():
                return str(path)
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
