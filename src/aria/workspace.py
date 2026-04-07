"""
aria/workspace.py — Persistent markdown storage.

Layout (all under the configured root, default ~/.aria/workspace/):
  memory/           long-term facts, user prefs, last-session summary
  soul/             agent identity and persona
  sessions/         per-session conversation logs
  tools_registry/   auto-generated tool docs
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path


class Workspace:
    def __init__(self, root: Path | str = "./workspace") -> None:
        self.root = Path(root).expanduser().resolve()
        for sub in ("memory", "soul", "sessions", "tools_registry"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
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
            memory_file.write_text(
                "# Core Memory\n\n_Nothing stored yet._\n",
                encoding="utf-8",
            )

    # ── Soul ─────────────────────────────────────────────────────────────────

    def load_soul(self) -> str:
        parts = [f.read_text(encoding="utf-8") for f in sorted((self.root / "soul").glob("*.md"))]
        return "\n\n---\n\n".join(parts)

    # ── Memory ───────────────────────────────────────────────────────────────

    def load_memory(self) -> str:
        parts = [f.read_text(encoding="utf-8") for f in sorted((self.root / "memory").glob("*.md"))]
        return "\n\n---\n\n".join(parts)

    def append_memory(self, note: str, filename: str = "core.md") -> None:
        path = self.root / "memory" / filename
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        with path.open("a", encoding="utf-8") as f:
            f.write(f"\n<!-- {ts} -->\n{note.strip()}\n")

    # ── Session summaries ─────────────────────────────────────────────────────

    def last_session_summary(self) -> str | None:
        """Return the most recent session summary, or None if none exists."""
        summary_file = self.root / "memory" / "last_session.md"
        if summary_file.exists():
            return summary_file.read_text(encoding="utf-8").strip()
        return None

    def save_session_summary(self, summary: str) -> None:
        """Overwrite the rolling last-session summary file."""
        path = self.root / "memory" / "last_session.md"
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        lines = [
            "# Last Session Summary\n",
            f"<!-- {ts} -->\n\n",
            summary.strip(),
            "\n",
        ]
        path.write_text("".join(lines), encoding="utf-8")

    # ── Session logs ──────────────────────────────────────────────────────────

    def new_session_path(self) -> Path:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        return self.root / "sessions" / f"session_{ts}.md"

    def log_session(self, path: Path, role: str, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%H:%M:%S")
        with path.open("a", encoding="utf-8") as f:
            f.write(f"\n\n**[{ts}] {role.upper()}**\n\n{content.strip()}\n")

    # ── Reflection support ───────────────────────────────────────────────────

    def unanalysed_sessions(self, watermark_file: str = "reflect_watermark") -> list[Path]:
        """
        Return session log files not yet analysed, sorted oldest-first.
        Uses a watermark file to track the last-analysed session timestamp.
        """
        sessions_dir = self.root / "sessions"
        watermark    = self.root / "memory" / watermark_file
        all_sessions = sorted(sessions_dir.glob("session_*.md"))

        if watermark.exists():
            last_ts = watermark.read_text(encoding="utf-8").strip()
            return [s for s in all_sessions if s.stem > last_ts]

        return all_sessions  # first run — analyse everything

    def update_watermark(self, session_path: Path, watermark_file: str = "reflect_watermark") -> None:
        """Advance the watermark to this session so it won't be re-analysed."""
        watermark = self.root / "memory" / watermark_file
        watermark.write_text(session_path.stem, encoding="utf-8")

    def save_patterns(self, patterns: str) -> None:
        """
        Overwrite memory/patterns.md with extracted patterns.
        This file is loaded as part of the memory context on every session.
        """
        path = self.root / "memory" / "patterns.md"
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        path.write_text(
            f"# Observed Patterns\n<!-- last updated {ts} -->\n\n{patterns.strip()}\n",
            encoding="utf-8",
        )

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
