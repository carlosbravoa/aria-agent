"""
aria/workspace.py — Persistent markdown storage.

Layout (all under the configured root, default ~/.aria/workspace/):
  memory/           long-term facts, conversation window, patterns, notify feed
  soul/             agent identity and persona
  sessions/         per-session conversation logs (chmod 700, files 600)
  tools_registry/   auto-generated tool docs
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

_WINDOW_MESSAGES  = int(os.environ.get("ARIA_WINDOW_MESSAGES",  "15"))
_WINDOW_MSG_CHARS = int(os.environ.get("ARIA_WINDOW_MSG_CHARS", "300"))

# ── Secret redaction ──────────────────────────────────────────────────────────
_SECRET_RE = re.compile(
    r"""(?ix)
    (?:password|passwd|secret|token|api[_\-]?key|auth[_\-]?key|
       access[_\-]?key|private[_\-]?key|bearer)
    \s*[=:]\s*\S+
    | (?:AKIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|ASIA)[A-Z0-9]{16}
    | sk-[a-zA-Z0-9]{20,}
    | ghp_[a-zA-Z0-9]{36}
    | xox[baprs]-[a-zA-Z0-9\-]+
    """
)


def _redact(text: str) -> str:
    return _SECRET_RE.sub("[REDACTED]", text)


def _secure_write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)


# ── Conversation window helpers ───────────────────────────────────────────────
_ENTRY_SEP = "\n---\n"


def _parse_window(text: str) -> list[str]:
    """Split window file into individual entries."""
    return [e.strip() for e in text.split(_ENTRY_SEP) if e.strip()]


def _format_entry(role: str, content: str, agent_name: str) -> str:
    label   = "User" if role == "user" else agent_name
    snippet = content.strip()[:_WINDOW_MSG_CHARS]
    if len(content.strip()) > _WINDOW_MSG_CHARS:
        snippet += "…"
    return f"**{label}:** {snippet}"


class Workspace:
    def __init__(self, root: Path | str = "./workspace") -> None:
        self.root = Path(root).expanduser().resolve()
        for sub in ("memory", "soul", "sessions", "tools_registry"):
            d = self.root / sub
            d.mkdir(parents=True, exist_ok=True)
        for sub in ("memory", "soul", "sessions"):
            (self.root / sub).chmod(0o700)
        self._bootstrap(agent_name=os.environ.get("AGENT_NAME", "Agent"))

    # ── Bootstrap ────────────────────────────────────────────────────────────

    def _bootstrap(self, agent_name: str = "Agent") -> None:
        soul_file = self.root / "soul" / "identity.md"
        if not soul_file.exists():
            soul_file.write_text(
                "# Agent Identity\n\n"
                f"You are {agent_name}, a lean and precise assistant running on a local LLM.\n"
                "You think step-by-step and only call tools when needed.\n"
                "\n"
                "## Channels\n\n"
                "You are reachable through multiple interfaces:\n"
                "- Terminal: interactive REPL and single-shot queries.\n"
                "- Telegram: users message you via the Telegram bot.\n"
                "- WhatsApp: users message your WhatsApp number directly.\n"
                "- Scheduled tasks: use --notify flag to push results to Telegram.\n"
                "\n"
                "All channels share the same memory and tools. "
                "Conversation history is isolated per channel and user.\n",
                encoding="utf-8",
            )
        memory_file = self.root / "memory" / "core.md"
        if not memory_file.exists():
            _secure_write(memory_file, "# Core Memory\n\n_Nothing stored yet._\n")

    # ── Soul ─────────────────────────────────────────────────────────────────

    def load_soul(self) -> str:
        parts = [f.read_text(encoding="utf-8") for f in sorted((self.root / "soul").glob("*.md"))]
        return "\n\n---\n\n".join(parts)

    # ── Memory ───────────────────────────────────────────────────────────────

    def load_memory(self) -> str:
        excluded = {"conversation_window.md", "notify_feed.md"}
        parts = [
            f.read_text(encoding="utf-8")
            for f in sorted((self.root / "memory").glob("*.md"))
            if f.name not in excluded
        ]
        return "\n\n---\n\n".join(parts)

    def append_memory(self, note: str, filename: str = "core.md") -> None:
        path = self.root / "memory" / filename
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        with path.open("a", encoding="utf-8") as f:
            f.write(f"\n<!-- {ts} -->\n{note.strip()}\n")
        path.chmod(0o600)

    # ── Conversation window ───────────────────────────────────────────────────

    def append_conversation_window(self, role: str, content: str, agent_name: str) -> None:
        """
        Append a message to the rolling conversation window.
        Written in real time after every exchange so nothing is lost on crash.
        """
        path  = self.root / "memory" / "conversation_window.md"
        entry = _format_entry(role, _redact(content), agent_name)

        if path.exists():
            existing = path.read_text(encoding="utf-8")
        else:
            existing = ""

        content_new = (existing + _ENTRY_SEP + entry) if existing else entry
        _secure_write(path, content_new)

    def trim_conversation_window(self) -> None:
        """
        Trim the window to the last ARIA_WINDOW_MESSAGES entries.
        Called on clean exit (close()) — not during the session.
        """
        path = self.root / "memory" / "conversation_window.md"
        if not path.exists():
            return
        entries = _parse_window(path.read_text(encoding="utf-8"))
        if len(entries) <= _WINDOW_MESSAGES:
            return
        trimmed = entries[-_WINDOW_MESSAGES:]
        _secure_write(path, _ENTRY_SEP.join(trimmed))

    def load_conversation_window(self) -> str | None:
        """Return the conversation window content, or None if empty."""
        path = self.root / "memory" / "conversation_window.md"
        if not path.exists():
            return None
        text = path.read_text(encoding="utf-8").strip()
        return text if text else None

    # ── Notify feed ──────────────────────────────────────────────────────────

    def append_notify_feed(self, message: str) -> None:
        """Record a proactively sent message — kept to last 10 entries."""
        feed_path = self.root / "memory" / "notify_feed.md"
        ts      = datetime.now().strftime("%Y-%m-%d %H:%M")
        marker  = "<!-- " + ts + " -->"
        line    = "- " + message.strip()[:500]
        entry   = chr(10) + marker + chr(10) + line + chr(10)
        existing = feed_path.read_text(encoding="utf-8") if feed_path.exists() else "# Recent Proactive Messages" + chr(10)
        sep    = chr(10) + "<!-- "
        parts  = existing.split(sep)
        header = parts[0]
        recent = parts[1:][-9:]
        content = header + (sep.join([""] + recent) if recent else "") + entry
        _secure_write(feed_path, content)

    def load_notify_feed(self) -> str | None:
        feed_path = self.root / "memory" / "notify_feed.md"
        if not feed_path.exists():
            return None
        return feed_path.read_text(encoding="utf-8").strip()

    # ── Session logs ──────────────────────────────────────────────────────────

    def new_session_path(self) -> Path:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        return self.root / "sessions" / f"session_{ts}.md"

    def log_session(self, path: Path, role: str, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.touch(mode=0o600)
        ts = datetime.now().strftime("%H:%M:%S")
        with path.open("a", encoding="utf-8") as f:
            f.write(f"\n\n**[{ts}] {role.upper()}**\n\n{_redact(content.strip())}\n")

    # ── Reflection support ───────────────────────────────────────────────────

    def unanalysed_sessions(self, watermark_file: str = "reflect_watermark") -> list[Path]:
        sessions_dir = self.root / "sessions"
        watermark    = self.root / "memory" / watermark_file
        all_sessions = sorted(sessions_dir.glob("session_*.md"))
        if watermark.exists():
            last_ts = watermark.read_text(encoding="utf-8").strip()
            return [s for s in all_sessions if s.stem > last_ts]
        return all_sessions

    def update_watermark(self, session_path: Path, watermark_file: str = "reflect_watermark") -> None:
        watermark = self.root / "memory" / watermark_file
        watermark.write_text(session_path.stem, encoding="utf-8")

    def save_patterns(self, patterns: str) -> None:
        path = self.root / "memory" / "patterns.md"
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        _secure_write(path, "# Observed Patterns\n" + f"<!-- last updated {ts} -->\n\n" + f"{patterns.strip()}\n")

    def load_patterns(self) -> str | None:
        path = self.root / "memory" / "patterns.md"
        return path.read_text(encoding="utf-8").strip() if path.exists() else None

    # ── Tools registry ────────────────────────────────────────────────────────

    def update_tools_registry(self, schemas: list[dict]) -> None:
        out = self.root / "tools_registry" / "available_tools.md"
        lines = ["# Available Tools\n"]
        for t in schemas:
            fn = t.get("function", t)
            lines.append(f"## `{fn['name']}`\n{fn.get('description', '')}\n")
            for k, v in fn.get("parameters", {}).get("properties", {}).items():
                req = k in fn.get("parameters", {}).get("required", [])
                lines.append(f"- `{k}` ({'required' if req else 'optional'}): {v.get('description', '')}\n")
            lines.append("")
        out.write_text("\n".join(lines), encoding="utf-8")
