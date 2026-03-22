"""
workspace.py — Persistent markdown storage for agent memory, soul, and session notes.

Layout:
  workspace/
    memory/     → long-term facts, user prefs, learned context
    soul/       → agent identity, values, persona config
    tools_registry/ → auto-generated tool docs (informational)
    sessions/   → per-session conversation logs
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path


class Workspace:
    def __init__(self, root: str | Path = "./workspace") -> None:
        self.root = Path(root).expanduser().resolve()
        for sub in ("memory", "soul", "sessions", "tools_registry"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        self._bootstrap()

    # ── Bootstrap defaults ──────────────────────────────────────────────────

    def _bootstrap(self) -> None:
        soul_file = self.root / "soul" / "identity.md"
        if not soul_file.exists():
            soul_file.write_text(
                "# Agent Identity\n\n"
                "You are a lean, precise assistant running on a local LLM.\n"
                "You think step-by-step, prefer short answers, and only call tools when needed.\n"
                "You save important facts to memory automatically.\n",
                encoding="utf-8",
            )
        memory_file = self.root / "memory" / "core.md"
        if not memory_file.exists():
            memory_file.write_text(
                "# Core Memory\n\n_Nothing stored yet._\n",
                encoding="utf-8",
            )

    # ── Soul (system prompt augmentation) ───────────────────────────────────

    def load_soul(self) -> str:
        parts: list[str] = []
        soul_dir = self.root / "soul"
        for f in sorted(soul_dir.glob("*.md")):
            parts.append(f.read_text(encoding="utf-8"))
        return "\n\n---\n\n".join(parts)

    # ── Memory ───────────────────────────────────────────────────────────────

    def load_memory(self) -> str:
        parts: list[str] = []
        mem_dir = self.root / "memory"
        for f in sorted(mem_dir.glob("*.md")):
            parts.append(f.read_text(encoding="utf-8"))
        return "\n\n---\n\n".join(parts)

    def append_memory(self, note: str, filename: str = "core.md") -> None:
        path = self.root / "memory" / filename
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        with path.open("a", encoding="utf-8") as f:
            f.write(f"\n\n<!-- {ts} -->\n{note.strip()}\n")

    # ── Sessions ─────────────────────────────────────────────────────────────

    def new_session_path(self) -> Path:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        return self.root / "sessions" / f"session_{ts}.md"

    def log_session(self, path: Path, role: str, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%H:%M:%S")
        with path.open("a", encoding="utf-8") as f:
            f.write(f"\n\n**[{ts}] {role.upper()}**\n\n{content.strip()}\n")

    # ── Tools registry docs ──────────────────────────────────────────────────

    def update_tools_registry(self, schemas: list[dict]) -> None:
        out = self.root / "tools_registry" / "available_tools.md"
        lines = ["# Available Tools\n"]
        for t in schemas:
            fn = t.get("function", t)
            lines.append(f"## `{fn['name']}`\n{fn.get('description','')}\n")
            params = fn.get("parameters", {}).get("properties", {})
            if params:
                lines.append("**Parameters:**\n")
                for k, v in params.items():
                    req = k in fn.get("parameters", {}).get("required", [])
                    lines.append(
                        f"- `{k}` ({'required' if req else 'optional'}): {v.get('description','')}\n"
                    )
            lines.append("")
        out.write_text("\n".join(lines), encoding="utf-8")
