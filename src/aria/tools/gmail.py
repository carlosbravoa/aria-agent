"""
tools/gmail.py — Gmail access via the `gog` CLI tool.

Setup: configure gog with `gog auth login` before use.
Docs: https://github.com/googleapis/google-cloud-go (or your gog variant)
"""

import subprocess
import shlex
import os

# Override CLI binary via env if needed
_CLI = os.getenv("GMAIL_CLI", "gog")

DEFINITION = {
    "name": "gmail",
    "description": (
        "Interact with Gmail via the gog CLI. "
        "Actions: list (recent emails), read (by message ID), send, search."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "read", "send", "search"],
                "description": "Gmail action to perform.",
            },
            "query": {
                "type": "string",
                "description": "Search query (for 'search' action) or message ID (for 'read').",
            },
            "to": {"type": "string", "description": "Recipient email (for 'send')."},
            "subject": {"type": "string", "description": "Email subject (for 'send')."},
            "body": {"type": "string", "description": "Email body (for 'send')."},
            "max_results": {
                "type": "integer",
                "description": "Max emails to return for list/search (default 10).",
                "default": 10,
            },
        },
        "required": ["action"],
    },
}


def _run(cmd: str) -> str:
    try:
        result = subprocess.run(
            shlex.split(cmd),
            capture_output=True,
            text=True,
            timeout=20,
        )
        out = result.stdout.strip()
        err = result.stderr.strip()
        if result.returncode != 0:
            return f"[gmail error] {err or 'Unknown error'}"
        return out or "(no output)"
    except FileNotFoundError:
        return (
            f"[gmail error] CLI '{_CLI}' not found. "
            "Install and configure it, then set GMAIL_CLI in .env."
        )
    except Exception as exc:
        return f"[gmail error] {exc}"


def execute(args: dict) -> str:
    action = args["action"]
    n = args.get("max_results", 10)

    match action:
        case "list":
            return _run(f"{_CLI} messages list --max-results {n}")

        case "read":
            msg_id = args.get("query", "")
            if not msg_id:
                return "[gmail] 'query' must contain a message ID for 'read'."
            return _run(f"{_CLI} messages get {msg_id}")

        case "search":
            query = args.get("query", "")
            if not query:
                return "[gmail] 'query' is required for search."
            return _run(f'{_CLI} messages list --query "{query}" --max-results {n}')

        case "send":
            to = args.get("to", "")
            subject = args.get("subject", "")
            body = args.get("body", "")
            if not (to and subject):
                return "[gmail] 'to' and 'subject' are required for send."
            return _run(f'{_CLI} messages send --to "{to}" --subject "{subject}" --body "{body}"')

        case _:
            return f"[gmail] Unknown action: {action}"
