"""
aria/channels/output.py — Fitting replies to the surface they're read on.

A terminal renders anything (rich Markdown, tables, long output). A chat app
renders a small, fixed subset, usually on a phone. Claude Code can leave this
to its own client; Aria's channels are other people's apps, so it's handled
on both sides:

  1. the model is told, EVERY TURN, where its reply will be read and what
     that surface can show (surface_note — goes in the per-turn context, so
     a remote-control turn from the phone gets it and a terminal turn doesn't)
  2. at delivery, whatever still doesn't fit is converted (adapt): tables →
     lists, headings → bold, Markdown → the channel's own dialect, and a very
     long reply → the first part plus the full text as a file.

Each channel declares its ChannelFormat (plugin attribute `output`).
"""

from __future__ import annotations

import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ChannelFormat:
    """What a channel can show.

    markdown   "commonmark" — full Markdown (Vicus, a web client)
               "basic"      — bold, italic, inline code, code blocks, lists (Telegram)
               "whatsapp"   — *bold* _italic_ ~strike~ ```mono``` and lists
               "plain"      — none; Markdown is stripped
    tables / headings       rendered by the client? (else converted)
    long_reply_chars        above this, a reply goes out as its first part plus
                            the whole text as a file (channels with send_file);
                            0 = never
    surface                 how the model should picture it, e.g. "a chat app
                            on a phone"
    """
    markdown: str = "basic"
    tables: bool = False
    headings: bool = False
    long_reply_chars: int = 3000
    surface: str = "a chat app, usually on a phone"


DEFAULT = ChannelFormat()


# ── 1. What the model is told ─────────────────────────────────────────────────

_SHOWS = {
    "commonmark": "full Markdown",
    "basic": "bold, italic, inline code, code blocks and bullet lists",
    "whatsapp": "bold, italic, strikethrough, monospace and bullet lists",
    "plain": "plain text only (no Markdown at all)",
}


def surface_note(channel: str, fmt: ChannelFormat) -> str:
    """The per-turn instruction for a reply that will be read on `channel`."""
    shows = _SHOWS.get(fmt.markdown, _SHOWS["basic"])
    lacks = [w for w, ok in (("tables", fmt.tables), ("headings", fmt.headings)) if not ok]
    lines = [f"You are replying on {channel.capitalize()}, {fmt.surface}. It shows {shows}."]
    if lacks:
        lines.append(f"It does NOT show {' or '.join(lacks)}: use short bullet lists "
                     f"and **bold** labels instead.")
    lines.append("Write like a chat message: lead with the answer, short paragraphs, "
                 "no filler. Skip long preambles and step-by-step narration of what "
                 "you did.")
    if fmt.long_reply_chars:
        lines.append(f"Keep a reply under about {fmt.long_reply_chars} characters. "
                     "If the full answer is genuinely long (a report, a big table, "
                     "code), give a short summary and offer to send the full version "
                     "as a file (send_file) instead of pasting it.")
    return "## Reply surface\n" + " ".join(lines)


# ── 2. What's converted on the way out ────────────────────────────────────────

_FENCE = re.compile(r"(```.*?```)", re.S)
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")


def _cells(row: str) -> list[str]:
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    return [c.strip() for c in row.split("|")]


def _table_to_list(header: list[str], rows: list[list[str]]) -> list[str]:
    """Each row becomes one bullet: the first column as a bold label, the
    rest as "Column: value" pairs."""
    out = []
    for r in rows:
        r = r + [""] * (len(header) - len(r))
        label = r[0] or "—"
        rest = [f"{h}: {v}" for h, v in zip(header[1:], r[1:], strict=False) if v]
        out.append(f"• **{label}**" + (f" — {', '.join(rest)}" if rest else ""))
    return out


def _convert_prose(block: str, fmt: ChannelFormat) -> str:
    lines = block.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # a GFM table: header row, separator row, body rows
        if (not fmt.tables and "|" in line and i + 1 < len(lines)
                and _TABLE_SEP.match(lines[i + 1])):
            header = _cells(line)
            j = i + 2
            rows = []
            while j < len(lines) and "|" in lines[j] and lines[j].strip():
                rows.append(_cells(lines[j]))
                j += 1
            out.extend(_table_to_list(header, rows))
            i = j
            continue
        m = _HEADING.match(line)
        if m and not fmt.headings:
            out.append(f"**{m.group(2)}**")
        elif _RULE.match(line) and fmt.markdown != "commonmark":
            out.append("")
        else:
            out.append(line)
        i += 1
    text = "\n".join(out)
    if fmt.markdown == "whatsapp":
        text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
        text = re.sub(r"__(.+?)__", r"_\1_", text)
        text = re.sub(r"~~(.+?)~~", r"~\1~", text)
        text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"\1 (\2)", text)
    elif fmt.markdown == "plain":
        text = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), text)
        text = re.sub(r"(?<!\w)[*_](\S[^*_\n]*?)[*_](?!\w)", r"\1", text)
        text = re.sub(r"`([^`\n]+)`", r"\1", text)
        text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"\1 (\2)", text)
    elif fmt.markdown == "basic":
        text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"\1 (\2)", text)
    return text


def convert(text: str, fmt: ChannelFormat) -> str:
    """Rewrite what the channel can't show; code blocks are left alone."""
    parts = _FENCE.split(text)
    for idx in range(0, len(parts), 2):             # even = prose, odd = fenced code
        parts[idx] = _convert_prose(parts[idx], fmt)
    return re.sub(r"\n{3,}", "\n\n", "".join(parts)).strip()


def _cut(text: str, limit: int) -> str:
    """The longest head of `text` under `limit` ending at a paragraph (else a
    line, else a sentence) boundary, never inside a code block."""
    head = text[:limit]
    if head.count("```") % 2:                        # don't end inside a fence
        head = head[:head.rfind("```")]
    for sep in ("\n\n", "\n", ". "):
        k = head.rfind(sep)
        if k > limit // 3:
            return head[:k + (1 if sep == ". " else 0)].rstrip()
    return head.rstrip()


def adapt(text: str, fmt: ChannelFormat, can_attach: bool) -> tuple[str, str | None]:
    """(what to send as the message, full Markdown to attach as a file or None)."""
    converted = convert(text, fmt)
    limit = fmt.long_reply_chars
    if not limit or len(converted) <= limit or not can_attach:
        return converted, None
    head = _cut(converted, limit - 80)
    return f"{head}\n\n📎 The full reply is attached as a file.", text


def write_attachment(text: str, channel: str) -> Path:
    """The full reply as a private .md file, for send_file."""
    d = Path(tempfile.gettempdir()) / f"aria-replies-{__import__('os').getuid()}"
    d.mkdir(mode=0o700, exist_ok=True)
    path = d / f"{channel}-reply-{time.strftime('%Y%m%d-%H%M%S')}.md"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


def fmt_for(channel: str) -> ChannelFormat:
    from aria import channels
    plugin = channels.get(channel)
    return getattr(plugin, "output", None) or DEFAULT
