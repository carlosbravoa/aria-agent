"""
aria/tools/send_file.py — Send a file from disk to the user over Telegram.

Security: this tool hands file contents to the outside world, so it is exactly
as dangerous as an exfiltration primitive if it resolves paths loosely. It does
NOT implement its own path checking — it calls file_access.resolve_readable(),
so the read allow-list, the permanent block-list (~/.ssh, ~/.aria/.env, cloud
credentials, /etc …) and the user-authorization flow all apply identically to
reading a file and to sending one.

Routing: replies in the chat the current turn belongs to (aria.context). With
no active channel — supervisor tasks, cron, `aria --notify` — it falls back to
the TELEGRAM_ALLOWED broadcast list.
"""

from __future__ import annotations

DEFINITION = {
    "name": "send_file",
    "description": (
        "Send a file from disk to the user via Telegram as a document "
        "attachment they can download. Use when the user asks you to send, "
        "share, or deliver an actual file — a report, an export, a log, a "
        "generated document, a photo. The file must already exist: create it "
        "first with file_access or shell_run, then send it. For plain text "
        "answers just reply normally instead of sending a file."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the existing file to send.",
            },
            "caption": {
                "type": "string",
                "description": (
                    "Optional short message shown with the file "
                    "(truncated at ~1000 characters)."
                ),
            },
        },
        "required": ["path"],
    },
}


def execute(args: dict) -> str:
    raw = (args.get("path") or "").strip()
    if not raw:
        return "[send_file] No path provided."
    caption = (args.get("caption") or "").strip()

    try:
        from aria import config, context
        config.load()

        from aria.tools import file_access

        # Same authorisation gate as reading the file — never a second, weaker one.
        path, denial = file_access.resolve_readable(raw)
        if denial:
            return denial
        if path is None:                      # defensive; resolve_readable is total
            return f"[send_file] Could not resolve path: {raw}"
        if not path.exists():
            return f"[send_file] Not found: {path}"
        if path.is_dir():
            return (f"[send_file] {path} is a directory. Archive it first "
                    f"(e.g. with shell_run) and send the archive.")

        active = context.current()
        if active and active.channel != "telegram":
            return (f"[send_file] Sending files is only supported on Telegram, "
                    f"not {active.channel}. The file is at {path}.")

        from aria.telegram_notify import send_document
        name = send_document(path, caption=caption)
        size = path.stat().st_size
        return f"[send_file] Sent {name} ({size / 1024:.0f} KB)."

    except RuntimeError as e:
        return f"[send_file error] {e}"
    except Exception as e:
        return f"[send_file error] Unexpected error: {e}"
