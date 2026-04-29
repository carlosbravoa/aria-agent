"""
aria/tools/imap.py — IMAP inbox management for any email provider.

Uses imaplib from the Python standard library — no extra dependencies.

Supports multiple accounts via named prefixes in ~/.aria/.env:
  IMAP_DEFAULT_HOST=imap.example.com
  IMAP_DEFAULT_USER=you@example.com
  IMAP_DEFAULT_PASSWORD=app-password
  IMAP_DEFAULT_PORT=993              # optional, default 993 (SSL)

For multiple accounts, use any prefix instead of DEFAULT:
  IMAP_WORK_HOST=imap.company.com
  IMAP_WORK_USER=me@company.com
  IMAP_WORK_PASSWORD=secret
  IMAP_PERSONAL_HOST=imap.fastmail.com
  IMAP_PERSONAL_USER=me@fastmail.com
  IMAP_PERSONAL_PASSWORD=secret

Provider quick reference:
  Gmail (non-gog):  imap.gmail.com        port 993  (needs app password)
  Outlook/Hotmail:  outlook.office365.com port 993  (needs app password)
  iCloud:           imap.mail.me.com      port 993  (needs app-specific password)
  Fastmail:         imap.fastmail.com     port 993
  Yahoo:            imap.mail.yahoo.com   port 993  (needs app password)
  ProtonMail:       127.0.0.1             port 1143 (via Proton Bridge)
"""

from __future__ import annotations

import email
import email.header
import imaplib
import os
import re
from email.utils import parsedate_to_datetime

DEFINITION = {
    "name": "imap",
    "description": (
        "Manage email via IMAP — works with any provider (Outlook, iCloud, Fastmail, Yahoo, etc.). "
        "Actions: list (recent messages), search (by subject/from/date), read (full message), "
        "mark_read, mark_unread, move (to folder), delete, list_folders. "
        "Use the 'account' parameter to select which account (matches IMAP_<ACCOUNT>_HOST in .env). "
        "Defaults to the DEFAULT account."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "search", "read", "mark_read", "mark_unread",
                         "move", "delete", "list_folders"],
                "description": "IMAP operation to perform.",
            },
            "account": {
                "type": "string",
                "description": (
                    "Account name matching IMAP_<ACCOUNT>_HOST in .env. "
                    "Default: 'DEFAULT'. Examples: 'WORK', 'PERSONAL'."
                ),
                "default": "DEFAULT",
            },
            "folder": {
                "type": "string",
                "description": "Mailbox folder. Default: INBOX.",
                "default": "INBOX",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of messages to return. Default: 10.",
                "default": 10,
            },
            "query": {
                "type": "string",
                "description": (
                    "Search query in IMAP format or natural shorthand. "
                    "Shorthands: 'unread', 'today', 'from:addr', 'subject:text'. "
                    "Raw IMAP: 'UNSEEN', 'FROM \"boss@co.com\"', 'SUBJECT \"meeting\"'. "
                    "Combine: 'unread from:boss@co.com'"
                ),
            },
            "uid": {
                "type": "string",
                "description": "Message UID (from list/search results). Required for read, mark_*, move, delete.",
            },
            "destination": {
                "type": "string",
                "description": "Target folder for move action.",
            },
        },
        "required": ["action"],
    },
}


# ── Credential loading ────────────────────────────────────────────────────────

def _get_credentials(account: str) -> tuple[str, str, str, int]:
    """Return (host, user, password, port) for the named account."""
    prefix = f"IMAP_{account.upper()}_"
    host   = os.environ.get(f"{prefix}HOST", "")
    user   = os.environ.get(f"{prefix}USER", "")
    passw  = os.environ.get(f"{prefix}PASSWORD", "")
    port   = int(os.environ.get(f"{prefix}PORT", "993"))

    if not host:
        raise ValueError(
            f"IMAP account '{account}' not configured. "
            f"Add {prefix}HOST, {prefix}USER, {prefix}PASSWORD to ~/.aria/.env"
        )
    if not user or not passw:
        raise ValueError(
            f"IMAP account '{account}' is missing USER or PASSWORD. "
            f"Check {prefix}USER and {prefix}PASSWORD in ~/.aria/.env"
        )
    return host, user, passw, port


# ── Connection ────────────────────────────────────────────────────────────────

def _connect(host: str, user: str, passw: str, port: int) -> imaplib.IMAP4_SSL:
    try:
        conn = imaplib.IMAP4_SSL(host, port)
        conn.login(user, passw)
        return conn
    except imaplib.IMAP4.error as e:
        raise ConnectionError(f"IMAP login failed for {user}@{host}: {e}") from e
    except OSError as e:
        raise ConnectionError(f"Cannot connect to {host}:{port}: {e}") from e


# ── Query translation ─────────────────────────────────────────────────────────

def _translate_query(query: str) -> str:
    """
    Translate natural shorthand to IMAP search criteria.
    Multiple shorthands are ANDed together.
    """
    if not query:
        return "ALL"

    parts = []
    q = query.strip()

    # Extract shorthands
    if "unread" in q.lower():
        parts.append("UNSEEN")
        q = re.sub(r"\bunread\b", "", q, flags=re.IGNORECASE).strip()

    if "today" in q.lower():
        from datetime import date
        d = date.today().strftime("%d-%b-%Y")
        parts.append(f'SINCE "{d}"')
        q = re.sub(r"\btoday\b", "", q, flags=re.IGNORECASE).strip()

    for m in re.finditer(r"from:(\S+)", q, re.IGNORECASE):
        parts.append(f'FROM "{m.group(1)}"')
        q = q.replace(m.group(0), "").strip()

    for m in re.finditer(r"subject:(.+?)(?:\s+\w+:|$)", q, re.IGNORECASE):
        parts.append(f'SUBJECT "{m.group(1).strip()}"')
        q = q.replace(m.group(0), "").strip()

    # Remaining text treated as raw IMAP criteria
    q = q.strip()
    if q:
        parts.append(q)

    return " ".join(parts) if parts else "ALL"


# ── Message parsing ───────────────────────────────────────────────────────────

def _decode_header(value: str) -> str:
    """Decode RFC2047 encoded email header."""
    parts = []
    for chunk, charset in email.header.decode_header(value):
        if isinstance(chunk, bytes):
            parts.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            parts.append(chunk)
    return "".join(parts)


def _extract_body(msg: email.message.Message, max_chars: int = 2000) -> str:
    """Extract plain text body from a potentially multipart message."""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                try:
                    payload = part.get_payload(decode=True)
                    charset = part.get_content_charset() or "utf-8"
                    text = payload.decode(charset, errors="replace")
                    return text[:max_chars] + ("…" if len(text) > max_chars else "")
                except Exception:
                    continue
        return "(no plain text body)"
    else:
        try:
            payload = msg.get_payload(decode=True)
            charset = msg.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            return text[:max_chars] + ("…" if len(text) > max_chars else "")
        except Exception:
            return "(could not decode body)"


def _format_message(uid: str, msg: email.message.Message, full: bool = False) -> str:
    subject = _decode_header(msg.get("Subject", "(no subject)"))
    sender  = _decode_header(msg.get("From", ""))
    date    = msg.get("Date", "")
    try:
        date = parsedate_to_datetime(date).strftime("%Y-%m-%d %H:%M")
    except Exception:
        pass

    if full:
        body = _extract_body(msg)
        return (
            f"UID: {uid}\n"
            f"From: {sender}\n"
            f"Subject: {subject}\n"
            f"Date: {date}\n"
            f"\n{body}"
        )
    else:
        return f"[{uid}] {date}  {sender[:30]:30}  {subject[:60]}"


# ── Actions ───────────────────────────────────────────────────────────────────

def execute(args: dict) -> str:
    action  = args.get("action", "list")
    account = args.get("account", "DEFAULT")

    try:
        host, user, passw, port = _get_credentials(account)
    except ValueError as e:
        return f"[imap] {e}"

    try:
        conn = _connect(host, user, passw, port)
    except ConnectionError as e:
        return f"[imap] {e}"

    try:
        return _dispatch(conn, action, args)
    except Exception as e:
        return f"[imap error] {e}"
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _dispatch(conn: imaplib.IMAP4_SSL, action: str, args: dict) -> str:
    folder      = args.get("folder", "INBOX")
    max_results = int(args.get("max_results", 10))
    query       = args.get("query", "")
    uid         = args.get("uid", "")
    destination = args.get("destination", "")

    match action:

        case "list_folders":
            _, data = conn.list()
            folders = []
            for item in data:
                if item:
                    parts = item.decode().split('"/"')
                    name  = parts[-1].strip().strip('"')
                    folders.append(name)
            return "\n".join(folders) if folders else "(no folders)"

        case "list":
            conn.select(folder, readonly=True)
            _, data = conn.uid("search", None, "ALL")
            uids = data[0].split()
            if not uids:
                return f"[imap] No messages in {folder}."
            # Most recent first
            recent = uids[-max_results:][::-1]
            return _fetch_headers(conn, recent)

        case "search":
            conn.select(folder, readonly=True)
            criteria = _translate_query(query)
            _, data  = conn.uid("search", None, criteria)
            uids = data[0].split()
            if not uids:
                return f"[imap] No messages matching '{query}'."
            recent = uids[-max_results:][::-1]
            return _fetch_headers(conn, recent)

        case "read":
            if not uid:
                return "[imap] 'uid' is required for read."
            conn.select(folder, readonly=True)
            _, data = conn.uid("fetch", uid, "(RFC822)")
            if not data or not data[0]:
                return f"[imap] Message {uid} not found."
            raw = data[0][1]
            msg = email.message_from_bytes(raw)
            return _format_message(uid, msg, full=True)

        case "mark_read":
            if not uid:
                return "[imap] 'uid' is required for mark_read."
            conn.select(folder)
            conn.uid("store", uid, "+FLAGS", "\\Seen")
            return f"[imap] Message {uid} marked as read."

        case "mark_unread":
            if not uid:
                return "[imap] 'uid' is required for mark_unread."
            conn.select(folder)
            conn.uid("store", uid, "-FLAGS", "\\Seen")
            return f"[imap] Message {uid} marked as unread."

        case "move":
            if not uid or not destination:
                return "[imap] 'uid' and 'destination' are required for move."
            conn.select(folder)
            conn.uid("copy", uid, destination)
            conn.uid("store", uid, "+FLAGS", "\\Deleted")
            conn.expunge()
            return f"[imap] Message {uid} moved to {destination}."

        case "delete":
            if not uid:
                return "[imap] 'uid' is required for delete."
            conn.select(folder)
            conn.uid("store", uid, "+FLAGS", "\\Deleted")
            conn.expunge()
            return f"[imap] Message {uid} deleted."

        case _:
            return f"[imap] Unknown action: {action}"


def _fetch_headers(conn: imaplib.IMAP4_SSL, uids: list[bytes]) -> str:
    """Fetch and format a list of message headers."""
    lines = []
    for uid in uids:
        try:
            _, data = conn.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            if data and data[0]:
                raw = data[0][1]
                msg = email.message_from_bytes(raw)
                lines.append(_format_message(uid.decode(), msg))
        except Exception:
            lines.append(f"[{uid.decode()}] (error fetching)")
    return "\n".join(lines) if lines else "(no messages)"
