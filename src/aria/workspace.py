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
    # key = value / key: value forms
    (?:password|passwd|secret|token|api[_\-]?key|auth[_\-]?key|
       access[_\-]?key|private[_\-]?key|client[_\-]?secret|bearer)
    \s*[=:]\s*\S+
    | (?:AKIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|ASIA)[A-Z0-9]{16}    # AWS access key id
    | sk-[a-zA-Z0-9_\-]{20,}                                     # OpenAI/Anthropic (sk-, sk-ant-, sk-proj-)
    | (?:sk|rk)_(?:live|test)_[a-zA-Z0-9]{10,}                   # Stripe secret/restricted keys
    | gh[opusr]_[a-zA-Z0-9]{36,}                                 # GitHub tokens (ghp_/gho_/ghu_/ghs_/ghr_)
    | github_pat_[a-zA-Z0-9_]{20,}                               # GitHub fine-grained PAT
    | glpat-[a-zA-Z0-9_\-]{20,}                                  # GitLab PAT
    | AIza[a-zA-Z0-9_\-]{35}                                     # Google API key
    | xox[baprse]-[a-zA-Z0-9\-]+                                 # Slack tokens
    | eyJ[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+       # JWT
    | -----BEGIN[A-Z0-9\s]*PRIVATE\sKEY-----[\s\S]*?-----END[A-Z0-9\s]*PRIVATE\sKEY-----  # PEM private key
    | (?<=://)[^/\s:@]+:[^/\s:@]+(?=@)                           # URL basic-auth user:pass@
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
    label = "User" if role == "user" else agent_name
    full  = content.strip()
    if len(full) > _WINDOW_MSG_CHARS:
        # The window stores only an excerpt to bound context cost. Mark it
        # explicitly so that when this turn is reloaded as history next session
        # the model understands the rest was DELIVERED at the time — not cut off
        # or left unfinished. Without this, a truncated long reply ending in "…"
        # reads as an incomplete/undelivered answer and the model re-does work.
        snippet = (
            full[:_WINDOW_MSG_CHARS]
            + f" […excerpt: {len(full)} chars total, full reply was delivered "
              "at the time; this is a trimmed history record]"
        )
    else:
        snippet = full
    return f"**{label}:** {snippet}"


class Workspace:
    def __init__(self, root: Path | str = "./workspace") -> None:
        self.root = Path(root).expanduser().resolve()
        for sub in ("memory", "soul", "sessions", "tools_registry"):
            d = self.root / sub
            d.mkdir(parents=True, exist_ok=True)
        for sub in ("memory", "soul", "sessions"):
            (self.root / sub).chmod(0o700)
        # Conversation window is keyed per channel/user so REPL, each Telegram
        # user, each WhatsApp user, and the supervisor each resume their own
        # context. Set via set_window_key(); defaults to the local REPL.
        self._window_key = "repl"
        self._bootstrap(agent_name=os.environ.get("AGENT_NAME", "Agent"))

    # ── Conversation window key ───────────────────────────────────────────────

    def set_window_key(self, key: str | None) -> None:
        """
        Select which per-channel conversation window this workspace reads/writes.

        `key` is typically "<channel>:<user_id>" (e.g. "telegram:12345"), "repl",
        or "supervisor". It is sanitised for use in a filename. The first time a
        non-legacy key is selected, an existing legacy conversation_window.md is
        migrated to the "repl" window so terminal continuity is not lost.
        """
        self._window_key = key or "repl"
        self._migrate_legacy_window()

    def _safe_window_key(self) -> str:
        return re.sub(r"[^A-Za-z0-9._-]", "_", self._window_key) or "repl"

    def _window_path(self) -> Path:
        return self.root / "memory" / f"conversation_window__{self._safe_window_key()}.md"

    def _migrate_legacy_window(self) -> None:
        """Rename a pre-per-channel conversation_window.md into the repl window."""
        legacy = self.root / "memory" / "conversation_window.md"
        repl   = self.root / "memory" / "conversation_window__repl.md"
        if legacy.exists() and not repl.exists():
            try:
                legacy.rename(repl)
            except OSError:
                pass

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
        excluded = {"notify_feed.md", "operational_memory.md"}
        parts = [
            f.read_text(encoding="utf-8")
            for f in sorted((self.root / "memory").glob("*.md"))
            if f.name not in excluded and not f.name.startswith("conversation_window")
        ]
        return "\n\n---\n\n".join(parts)

    def core_is_empty(self) -> bool:
        """
        True when core memory holds no real user facts yet (fresh user).
        Ignores the header and the '_Nothing stored yet._' placeholder and
        strips <!-- timestamp --> comments. Drives first-contact onboarding.
        """
        path = self.root / "memory" / "core.md"
        if not path.exists():
            return True
        text = re.sub(r"<!--.*?-->", "", path.read_text(encoding="utf-8"), flags=re.S)
        for token in ("# Core Memory", "_Nothing stored yet._"):
            text = text.replace(token, "")
        return not text.strip()

    def append_memory(self, note: str, filename: str = "core.md") -> None:
        path = self.root / "memory" / filename
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        with path.open("a", encoding="utf-8") as f:
            f.write(f"\n<!-- {ts} -->\n{note.strip()}\n")
        path.chmod(0o600)

    def append_operational_memory(self, note: str) -> None:
        """Append an operational/procedural note to operational_memory.md.
        Capped at ARIA_OPSMEM_MAX_LINES lines — reflection prunes stale entries.
        """
        import os
        max_lines = int(os.environ.get("ARIA_OPSMEM_MAX_LINES", "40"))
        path = self.root / "memory" / "operational_memory.md"
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        if not path.exists():
            _secure_write(path, "# Operational Memory\n")
        with path.open("a", encoding="utf-8") as f:
            f.write(f"\n<!-- {ts} -->\n{note.strip()}\n")
        path.chmod(0o600)
        # Trim if over limit
        content = path.read_text(encoding="utf-8")
        lines = [l for l in content.splitlines() if l.strip()]
        entries = [l for l in lines if not l.startswith(("#", "<!--"))]
        if len(entries) > max_lines:
            # Keep header + last max_lines entries
            header = "# Operational Memory"
            trimmed = "\n".join([header] + entries[-max_lines:])
            _secure_write(path, trimmed + "\n")

    def load_operational_memory(self) -> str | None:
        """Return operational memory contents, or None if empty."""
        path = self.root / "memory" / "operational_memory.md"
        if not path.exists():
            return None
        text = path.read_text(encoding="utf-8").strip()
        # Strip header line for cleaner injection
        lines = [l for l in text.splitlines() if not l.startswith("#")]
        content = "\n".join(lines).strip()
        return content if content else None

    # ── Conversation window ───────────────────────────────────────────────────

    def append_conversation_window(self, role: str, content: str, agent_name: str) -> None:
        """
        Append a message to the rolling conversation window.
        Written in real time after every exchange so nothing is lost on crash.
        """
        path  = self._window_path()
        entry = _format_entry(role, _redact(content), agent_name)

        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        entries  = _parse_window(existing)
        entries.append(entry)
        # Self-bound on every append (not just on close) so a crash can never
        # leave an oversized window that reloads as bloated context next session.
        if len(entries) > _WINDOW_MESSAGES:
            entries = entries[-_WINDOW_MESSAGES:]
        _secure_write(path, _ENTRY_SEP.join(entries))

    def trim_conversation_window(self) -> None:
        """
        Trim the window to the last ARIA_WINDOW_MESSAGES entries.
        Called on clean exit (close()) — not during the session.
        """
        path = self._window_path()
        if not path.exists():
            return
        entries = _parse_window(path.read_text(encoding="utf-8"))
        if len(entries) <= _WINDOW_MESSAGES:
            return
        trimmed = entries[-_WINDOW_MESSAGES:]
        _secure_write(path, _ENTRY_SEP.join(trimmed))

    def load_conversation_window(self) -> str | None:
        """Return the conversation window content, or None if empty."""
        path = self._window_path()
        if not path.exists():
            return None
        text = path.read_text(encoding="utf-8").strip()
        return text if text else None

    def load_conversation_window_messages(self) -> list[dict[str, str]]:
        """
        Return the conversation window as a list of {role, content} messages.

        Reconstructs the role from each entry's **User:**/**<agent>:** label so
        a restarted session resumes with the real prior turns in the message
        history — not merely as a low-salience system-prompt memory block. This
        is what lets "what were my last messages?" answer from actual context.
        Capped to the last ARIA_WINDOW_MESSAGES entries.
        """
        path = self._window_path()
        if not path.exists():
            return []
        entries = _parse_window(path.read_text(encoding="utf-8"))[-_WINDOW_MESSAGES:]
        msgs: list[dict[str, str]] = []
        for entry in entries:
            if entry.startswith("**") and ":**" in entry:
                label, content = entry[2:].split(":**", 1)
                role    = "user" if label.strip().lower() == "user" else "assistant"
                content = content.strip()
            else:
                role, content = "assistant", entry.strip()
            if content:
                msgs.append({"role": role, "content": content})
        return msgs

    def rewind_window_to_before_last_user(self) -> None:
        """Drop trailing entries back through (and including) the last User entry.
        Used by /retry so re-asking doesn't leave the old question+answer behind."""
        path = self._window_path()
        if not path.exists():
            return
        entries = _parse_window(path.read_text(encoding="utf-8"))
        last_user = None
        for i, e in enumerate(entries):
            if e.startswith("**User:**"):
                last_user = i
        if last_user is None:
            return
        _secure_write(path, _ENTRY_SEP.join(entries[:last_user]))

    def reset_conversation_window(self, summary: str, agent_name: str) -> None:
        """Replace the window with a single summary entry (used by /compact)."""
        path  = self._window_path()
        entry = _format_entry("assistant", _redact(summary), agent_name)
        _secure_write(path, entry)

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
