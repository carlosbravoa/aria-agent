"""
aria/tools/gmail.py — Gmail via the gogcli (gog) CLI.

Correct command syntax (gogcli / steipete):
  gog gmail search '<query>' --max <n> [--json]
  gog gmail get <thread_id> [--json]
  gog gmail send --to <email> --subject <subject> --body <text>
  gog gmail thread modify <thread_id> --remove UNREAD   (mark as read)

Required env var in ~/.aria/.env:
  GOG_ACCOUNT=you@gmail.com
  GMAIL_CLI=gog   (optional, defaults to gog)

Docs: https://github.com/steipete/gogcli
"""

from __future__ import annotations

import os
import subprocess
import shlex

from aria.tools._env import build_env, gog_keyring_hint

_CLI = os.getenv("GMAIL_CLI", "gog")

DEFINITION = {
    "name": "gmail",
    "description": (
        "Interact with Gmail via the gog CLI. "
        "Actions: list (recent emails), read (full thread by ID), send, search (by query), mark_read (mark thread as read). "
        "list and search return each thread as `[thread_id] sender — \"subject\"`; "
        "pass that bracketed thread_id to read or mark_read."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "read", "send", "search", "mark_read"],
                "description": "Gmail action to perform.",
            },
            "query": {
                "type": "string",
                "description": (
                    "Search query for 'search' (Gmail search syntax, e.g. 'is:unread newer_than:7d'). "
                    "Thread ID for 'read' and 'mark_read'."
                ),
            },
            "to":      {"type": "string", "description": "Recipient email address (for 'send')."},
            "subject": {"type": "string", "description": "Email subject (for 'send')."},
            "body":    {"type": "string", "description": "Email body text (for 'send')."},
            "max_results": {
                "type": "integer",
                "description": "Max results to return for list/search (default 10).",
                "default": 10,
            },
        },
        "required": ["action"],
    },
}


def _run(cmd: str) -> str:
    env = build_env()
    # Ensure GOG_ACCOUNT is set — gog requires it
    if "GOG_ACCOUNT" not in env:
        return (
            "[gmail error] GOG_ACCOUNT is not set. "
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
            details = err or out or "no output"
            return (f"[gmail error] exit={result.returncode}\ncmd: {cmd}\n{details}"
                    + gog_keyring_hint(details))
        return out or "(no output)"
    except FileNotFoundError:
        return (
            f"[gmail error] '{_CLI}' not found in PATH. "
            "Ensure it is installed and GMAIL_CLI is set correctly in ~/.aria/.env"
        )
    except subprocess.TimeoutExpired:
        return f"[gmail error] command timed out: {cmd}"
    except Exception as exc:
        return f"[gmail error] {exc}"


def _format_threads(raw: str) -> str:
    """Format gog's `--json` thread list into compact, ID-first lines so the
    model can reliably pass the thread id to read/mark_read. Falls back to the
    raw output if it isn't the expected JSON (older gog, or an error string)."""
    import json
    if raw.startswith("[gmail"):           # _run already returned an error → pass through
        return raw
    try:
        threads = json.loads(raw).get("threads", [])
    except Exception:
        return raw                          # not JSON → no regression, return as-is
    if not threads:
        return "No messages."
    lines = []
    for t in threads:
        tid     = t.get("id", "?")
        subject = t.get("subject", "(no subject)")
        sender  = t.get("from", "")
        labels  = t.get("labels") or []
        flags   = ["UNREAD"] if "UNREAD" in labels else []
        cnt     = t.get("messageCount")
        if isinstance(cnt, int) and cnt > 1:
            flags.append(f"{cnt} msgs")
        meta = "  ·  ".join(filter(None, [", ".join(flags), t.get("date", "")]))
        line = f'[{tid}] {sender} — "{subject}"'
        lines.append(f"{line}  ·  {meta}" if meta else line)
    return f"{len(threads)} thread(s):\n" + "\n".join(lines)


def execute(args: dict) -> str:
    action = args["action"]
    n = args.get("max_results", 10)

    match action:
        case "list":
            return _format_threads(_run(f"{_CLI} gmail search 'in:inbox' --max {n} --json"))

        case "search":
            query = args.get("query", "")
            if not query:
                return "[gmail] 'query' is required for search."
            return _format_threads(_run(f"{_CLI} gmail search {shlex.quote(query)} --max {n} --json"))

        case "read":
            thread_id = args.get("query", "")
            if not thread_id:
                return "[gmail] 'query' must contain a thread ID for 'read'."
            return _run(f"{_CLI} gmail get {shlex.quote(thread_id)}")

        case "send":
            to      = args.get("to", "")
            subject = args.get("subject", "")
            body    = args.get("body", "")
            if not (to and subject):
                return "[gmail] 'to' and 'subject' are required for send."
            return _run(
                f"{_CLI} gmail send"
                f" --to {shlex.quote(to)}"
                f" --subject {shlex.quote(subject)}"
                f" --body {shlex.quote(body)}"
            )

        case "mark_read":
            thread_id = args.get("query", "")
            if not thread_id:
                return "[gmail] 'query' must contain a thread ID for 'mark_read'."
            return _run(f"{_CLI} gmail thread modify {shlex.quote(thread_id)} --remove UNREAD")

        case _:
            return f"[gmail] Unknown action: {action}"
