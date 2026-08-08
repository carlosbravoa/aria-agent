"""
aria/attachments.py — Inbound file storage for channels (Telegram, WhatsApp…).

Incoming filenames are attacker-controlled: a chat user can name a file
`../../.ssh/authorized_keys` and, without sanitising, a download would write
straight through the inbox. Every name here is reduced to a safe basename
before it touches the filesystem.

Files land under the workspace, which is always in file_access's read AND
write allow-list and is already chmod 700 — so the agent can read what you
send with no extra configuration, and nothing outside the workspace is
reachable.

The inbox self-prunes on every save so a chatty user can't fill the disk:
entries older than ARIA_INBOX_KEEP_DAYS are removed, then oldest-first until
the tree is under ARIA_INBOX_MAX_MB.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

INBOX = "inbox"

_MAX_NAME    = 120          # chars, extension preserved
_MAX_EXT     = 12
_FALLBACK    = "attachment.bin"

# Capability notes surfaced to the agent alongside the file, so it tells the
# user the truth instead of promising to "look at" something it cannot read.
_KIND_NOTES = {
    "photo": ("Aria cannot see image content — only the file itself is stored. "
              "Say so plainly if the user asks what the image shows."),
    "voice": ("Voice transcription is not enabled on this deployment, so the "
              "audio cannot be read. Tell the user, and offer to help if they "
              "type the message instead."),
    "audio": ("Audio transcription is not enabled on this deployment, so the "
              "contents cannot be read."),
    "video": ("Aria cannot watch video content — only the file itself is stored."),
    "video_note": ("Aria cannot watch video content — only the file itself is stored."),
    "animation": ("Aria cannot see image or video content — only the file itself "
                  "is stored."),
}


def _keep_days() -> int:
    try:
        return max(0, int(os.environ.get("ARIA_INBOX_KEEP_DAYS", "14")))
    except ValueError:
        return 14


def _max_bytes() -> int:
    try:
        return max(0, int(os.environ.get("ARIA_INBOX_MAX_MB", "200"))) * 1024 * 1024
    except ValueError:
        return 200 * 1024 * 1024


def safe_name(raw: str | None, fallback: str = _FALLBACK) -> str:
    """Reduce an untrusted filename to a plain, safe basename.

    Strips directory components (path traversal), control and non-printable
    characters, and leading/trailing dots (so '..' and hidden files can't be
    produced), then caps the length while preserving the extension.
    """
    name = Path(raw or "").name              # drops any ../ or /abs/ prefix
    name = "".join(
        ch for ch in name
        if ch.isprintable() and ch not in ('/', '\\', '\x00')
    )
    name = name.strip().strip(".").strip()
    if not name:
        return fallback

    if len(name) > _MAX_NAME:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= _MAX_EXT:
            name = stem[: _MAX_NAME - len(ext) - 1] + "." + ext
        else:
            name = name[:_MAX_NAME]
    return name or fallback


def inbox_dir(channel: str, user_id: str) -> Path:
    """Per-channel, per-user inbox directory inside the workspace."""
    from aria import config
    return (config.workspace_dir() / INBOX
            / safe_name(channel, "channel") / safe_name(str(user_id), "user"))


def destination(channel: str, user_id: str, filename: str | None) -> Path:
    """Create the inbox directory and return a free, timestamped path in it."""
    from aria import config
    directory = inbox_dir(channel, user_id)
    directory.mkdir(parents=True, exist_ok=True)

    # 700 on the inbox root and every level below it — user-sent files can be
    # sensitive, and the workspace root itself is not restricted.
    root = config.workspace_dir() / INBOX
    for d in (root, directory.parent, directory):
        try:
            d.chmod(0o700)
        except OSError:
            pass

    base      = f"{time.strftime('%Y%m%d_%H%M%S')}_{safe_name(filename)}"
    candidate = directory / base
    n = 1
    while candidate.exists():
        p = Path(base)
        candidate = directory / f"{p.stem}_{n}{p.suffix}"
        n += 1
    return candidate


def finalize(path: Path) -> None:
    """Lock down a freshly downloaded file and prune the inbox."""
    try:
        path.chmod(0o600)
    except OSError:
        pass
    try:
        prune()
    except Exception as exc:                  # never fail a download over cleanup
        log.warning("inbox prune failed: %s", exc)


def prune() -> int:
    """Drop aged-out then over-quota files. Returns how many were removed."""
    from aria import config
    root = config.workspace_dir() / INBOX
    if not root.is_dir():
        return 0

    files = [p for p in root.rglob("*") if p.is_file()]
    removed = 0

    keep_days = _keep_days()
    if keep_days:
        cutoff = time.time() - keep_days * 86400
        for p in list(files):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    files.remove(p)
                    removed += 1
            except OSError:
                pass

    max_bytes = _max_bytes()
    if max_bytes:
        try:
            sized = sorted(((p, p.stat()) for p in files),
                           key=lambda t: t[1].st_mtime)
        except OSError:
            return removed
        total = sum(st.st_size for _, st in sized)
        for p, st in sized:
            if total <= max_bytes:
                break
            try:
                p.unlink()
                total -= st.st_size
                removed += 1
            except OSError:
                pass
    return removed


def human_size(size: int | None) -> str:
    if not size:
        return "unknown size"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def describe(path: Path, *, channel: str, kind: str,
             original_name: str | None = None, mime: str | None = None,
             size: int | None = None, caption: str = "") -> str:
    """Build the message handed to the agent when a user sends a file.

    Factual, plus a capability note for media Aria genuinely cannot interpret,
    so it never claims to have looked at an image or listened to a voice note.
    """
    lines = [
        f"[The user sent a file via {channel}]",
        f"saved_to: {path}",
        f"filename: {original_name or path.name}",
        f"type: {mime or 'unknown'} ({kind})",
        f"size: {human_size(size)}",
    ]
    note = _KIND_NOTES.get(kind)
    if note:
        lines.append(f"note: {note}")

    body = "\n".join(lines)
    if caption.strip():
        return f"{body}\n\nThe user's message with the file:\n{caption.strip()}"
    return (f"{body}\n\nNo message accompanied the file. Read or inspect it if "
            f"that is useful, then briefly say what it is and offer next steps.")
