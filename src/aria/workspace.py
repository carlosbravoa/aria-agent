"""
aria/workspace.py — Persistent markdown storage.

Layout (all under the configured root, default ~/.aria/workspace/):
  memory/           long-term facts, conversation window, patterns, notify feed
  soul/             agent identity and persona
  sessions/         per-session conversation logs (chmod 700, files 600)
  tools_registry/   auto-generated tool docs
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
import threading
from datetime import datetime
from pathlib import Path

# Defaults only — the live values are read from the environment at use time
# (_window_messages()/_window_msg_chars()), because this module is imported
# before config.load() has applied ~/.aria/.env in some entry points.
_WINDOW_MESSAGES  = 15
_WINDOW_MSG_CHARS = 300


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _window_messages() -> int:
    return _env_int("ARIA_WINDOW_MESSAGES", _WINDOW_MESSAGES)


def _window_msg_chars() -> int:
    return _env_int("ARIA_WINDOW_MSG_CHARS", _WINDOW_MSG_CHARS)

# Placeholder lines that are structurally present but are NOT real facts, so
# list/forget/search must skip them (matches core_is_empty's own carve-out).
_MEMORY_PLACEHOLDERS = {"_nothing stored yet._"}

# forget_memory safety: substring queries shorter than this, or matching more
# entries than this, are refused (unless they exactly equal a stored entry).
_FORGET_MIN_CHARS   = 4
_FORGET_MAX_MATCHES = 3


class ForgetTooBroad(ValueError):
    """forget_memory refused an over-broad query. str() lists the matches so a
    tool wrapper that reports the exception text hands the model the candidates."""

    def __init__(self, query: str, matches: list[str]) -> None:
        self.query, self.matches = query, matches
        shown = "\n".join(f"  {m}" for m in matches[:20])
        more  = f"\n  … and {len(matches) - 20} more" if len(matches) > 20 else ""
        super().__init__(
            f"Refused: {query!r} is too broad — it matches {len(matches)} "
            f"entr{'y' if len(matches) == 1 else 'ies'}. Nothing was removed. "
            f"Repeat with the exact entry text or a longer, more specific phrase "
            f"(>= {_FORGET_MIN_CHARS} chars, <= {_FORGET_MAX_MATCHES} matches).\n"
            f"Matches:\n{shown}{more}")


def _fact_key(line: str) -> str:
    """Normalised entry text for exact-match comparison ('- ' bullet optional)."""
    s = line.strip().lower()
    return s[2:].strip() if s.startswith("- ") else s

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
    """Atomically write a 0600 file: unique sibling temp (mkstemp, created 0600),
    fsync, then os.replace. A crash mid-write can no longer truncate the
    destination, and concurrent writers never share (and clobber) one temp."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)   # atomic on the same filesystem (sibling temp guarantees it)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# ── Cross-process file locking ────────────────────────────────────────────────
# Memory files are read-modify-written by several processes (REPL, Telegram,
# WhatsApp, supervisor, aria-reflect). An atomic replace alone still loses
# updates when two writers interleave, so every RMW runs under an exclusive
# flock on a sibling `.<name>.lock` file. Re-entrant per thread (flock on a
# second fd of the same file would self-deadlock); no-op where fcntl is missing.

try:
    import fcntl as _fcntl
except ImportError:          # pragma: no cover — Windows
    _fcntl = None

_held_locks = threading.local()


@contextlib.contextmanager
def file_lock(path: Path):
    """Exclusive advisory lock guarding read-modify-write of `path`."""
    lock_path = path.with_name(f".{path.name}.lock")
    held = getattr(_held_locks, "paths", None)
    if held is None:
        held = _held_locks.paths = set()
    key = str(lock_path)
    if _fcntl is None or key in held:
        yield
        return
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        _fcntl.flock(fd, _fcntl.LOCK_EX)
        held.add(key)
        try:
            yield
        finally:
            held.discard(key)
            _fcntl.flock(fd, _fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ── Conversation window helpers ───────────────────────────────────────────────
_ENTRY_SEP = "\n---\n"


def _parse_window(text: str) -> list[str]:
    """Split window file into individual entries."""
    return [e.strip() for e in text.split(_ENTRY_SEP) if e.strip()]


def _format_entry(role: str, content: str, agent_name: str) -> str:
    label = "User" if role == "user" else agent_name
    full  = content.strip()
    cap   = _window_msg_chars()
    if len(full) > cap:
        # The window stores only an excerpt to bound context cost. Mark it
        # explicitly so that when this turn is reloaded as history next session
        # the model understands the rest was DELIVERED at the time — not cut off
        # or left unfinished. Without this, a truncated long reply ending in "…"
        # reads as an incomplete/undelivered answer and the model re-does work.
        snippet = (
            full[:cap]
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
        clean = note.strip()
        with file_lock(path):
            # Write-time dedup: core.md has no line cap (facts are permanent, so we
            # can't blind-drop the oldest), which made repeated identical facts the
            # single biggest long-run context-cost growth. Skip an exact duplicate;
            # reflection does the smarter semantic consolidation later.
            if path.exists():
                existing = {l.strip() for l in path.read_text(encoding="utf-8").splitlines()}
                if clean in existing:
                    return
            ts = datetime.now().strftime("%Y-%m-%d %H:%M")
            with path.open("a", encoding="utf-8") as f:
                f.write(f"\n<!-- {ts} -->\n{clean}\n")
            path.chmod(0o600)

    # ── Memory management (list / forget / search / consolidate) ───────────────

    def _memory_path(self, filename: str) -> Path:
        return self.root / "memory" / filename

    @staticmethod
    def _is_fact_line(line: str) -> bool:
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("<!--"):
            return False
        return s.lower() not in _MEMORY_PLACEHOLDERS

    def list_memory_facts(self, filename: str = "core.md") -> list[str]:
        """Return the stored fact/entry lines of a memory file (no header, no
        <!-- timestamp --> comments, no blanks)."""
        path = self._memory_path(filename)
        if not path.exists():
            return []
        return [l.strip() for l in path.read_text(encoding="utf-8").splitlines()
                if self._is_fact_line(l)]

    def forget_memory(self, query: str, filename: str = "core.md") -> int:
        """Remove every entry whose text contains `query` (case-insensitive),
        along with its preceding <!-- timestamp --> comment. Rewrites the file
        atomically. Returns the number of entries removed.

        Guard against over-broad queries (e.g. "e" would wipe nearly everything):
        a query shorter than _FORGET_MIN_CHARS, or one matching more than
        _FORGET_MAX_MATCHES entries, removes nothing unless it EXACTLY equals an
        entry (then only the exact entries go) — ForgetTooBroad is raised with
        the candidate list so the caller can ask for a more specific phrase."""
        q = (query or "").strip().lower()
        path = self._memory_path(filename)
        if not q or not path.exists():
            return 0
        with file_lock(path):
            lines = path.read_text(encoding="utf-8").splitlines()
            facts = [l.strip() for l in lines if self._is_fact_line(l)]
            exact = [f for f in facts if _fact_key(f) == _fact_key(q)]
            matches = [f for f in facts if q in f.lower()]
            if exact:
                doomed = lambda s: _fact_key(s) == _fact_key(q)
            elif len(q) < _FORGET_MIN_CHARS or len(matches) > _FORGET_MAX_MATCHES:
                if not matches:
                    return 0
                raise ForgetTooBroad(q, matches)
            else:
                doomed = lambda s: q in s.lower()
            kept: list[str] = []
            pending_comment: str | None = None
            removed = 0
            for line in lines:
                s = line.strip()
                if s.startswith("<!--"):
                    pending_comment = line          # hold until we know its fact's fate
                    continue
                if self._is_fact_line(line) and doomed(s):
                    removed += 1
                    pending_comment = None          # drop the orphaned comment too
                    continue
                if pending_comment is not None:
                    kept.append(pending_comment)
                    pending_comment = None
                kept.append(line)
            if pending_comment is not None:
                kept.append(pending_comment)
            if removed:
                _secure_write(path, "\n".join(kept).rstrip() + "\n")
            return removed

    def search_memory(self, query: str, max_results: int = 20) -> list[tuple[str, str]]:
        """Substring search (case-insensitive) across all memory stores. Returns
        (source, line) pairs so the agent can recall a fact without the whole
        memory being injected every turn."""
        q = (query or "").strip().lower()
        if not q:
            return []
        results: list[tuple[str, str]] = []
        for fn in ("core.md", "operational_memory.md", "patterns.md", "notify_feed.md"):
            path = self._memory_path(fn)
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                if self._is_fact_line(line) and q in line.lower():
                    results.append((fn, line.strip()))
                    if len(results) >= max_results:
                        return results
        notes_dir = self.root / "memory" / "project_notes"
        if notes_dir.exists():
            for path in sorted(notes_dir.glob("*.md")):
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                    if self._is_fact_line(line) and q in line.lower():
                        results.append((f"project_notes/{path.name}", line.strip()))
                        if len(results) >= max_results:
                            return results
        return results

    def save_core_memory(self, text: str) -> None:
        """Rewrite core.md with a consolidated fact list (used by reflection)."""
        path = self._memory_path("core.md")
        with file_lock(path):
            _secure_write(path, "# Core Memory\n\n" + text.strip() + "\n")

    def load_core_memory(self) -> str | None:
        """Core memory contents with the header stripped, or None if empty."""
        path = self._memory_path("core.md")
        if not path.exists():
            return None
        lines = [l for l in path.read_text(encoding="utf-8").splitlines()
                 if not l.strip().startswith("#")]
        content = "\n".join(lines).strip()
        return content if content else None

    def append_operational_memory(self, note: str) -> None:
        """Append an operational/procedural note to operational_memory.md.
        Capped at ARIA_OPSMEM_MAX_LINES lines — reflection prunes stale entries.
        """
        max_lines = _env_int("ARIA_OPSMEM_MAX_LINES", 40)
        path = self.root / "memory" / "operational_memory.md"
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        with file_lock(path):
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
        cap   = _window_messages()

        with file_lock(path):
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
            entries  = _parse_window(existing)
            entries.append(entry)
            # Self-bound on every append (not just on close) so a crash can never
            # leave an oversized window that reloads as bloated context next session.
            if len(entries) > cap:
                entries = entries[-cap:]
            _secure_write(path, _ENTRY_SEP.join(entries))

    def trim_conversation_window(self) -> None:
        """
        Trim the window to the last ARIA_WINDOW_MESSAGES entries.
        Called on clean exit (close()) — not during the session.
        """
        path = self._window_path()
        cap  = _window_messages()
        with file_lock(path):
            if not path.exists():
                return
            entries = _parse_window(path.read_text(encoding="utf-8"))
            if len(entries) <= cap:
                return
            trimmed = entries[-cap:]
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
        entries = _parse_window(path.read_text(encoding="utf-8"))[-_window_messages():]
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
        with file_lock(path):
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
        with file_lock(path):
            _secure_write(path, entry)

    # ── Notify feed ──────────────────────────────────────────────────────────

    def append_notify_feed(self, message: str) -> None:
        """Record a proactively sent message — kept to last 10 entries."""
        feed_path = self.root / "memory" / "notify_feed.md"
        ts      = datetime.now().strftime("%Y-%m-%d %H:%M")
        marker  = "<!-- " + ts + " -->"
        line    = "- " + message.strip()[:500]
        entry   = chr(10) + marker + chr(10) + line + chr(10)
        with file_lock(feed_path):
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

    # ── Friction log ─────────────────────────────────────────────────────────
    # High-friction turns detected by the harness (many tool errors, repeat-
    # guard hits, hard stops). Written by agent._flag_friction, consumed and
    # cleared by aria-reflect's friction phase. Capped so it can't grow.

    def append_friction_log(self, line: str) -> None:
        path = self.root / "memory" / "friction_log.md"
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = f"- {ts} {line.strip()[:300]}"
        with file_lock(path):
            existing = (path.read_text(encoding="utf-8")
                        if path.exists() else "# Friction Log\n")
            lines = [l for l in existing.splitlines() if l.startswith("- ")][-49:]
            _secure_write(path, "# Friction Log\n" + "\n".join(lines + [entry]) + "\n")

    def load_friction_log(self) -> str | None:
        path = self.root / "memory" / "friction_log.md"
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8").strip() or None

    def clear_friction_log(self, analysed: str | None = None) -> None:
        """Consume the friction log. With `analysed` (the log text that was
        actually reviewed), only those entries are removed — events logged
        while the reflection LLM call was in flight survive for the next pass.
        Without it, the whole log is deleted (legacy behaviour)."""
        path = self.root / "memory" / "friction_log.md"
        with file_lock(path):
            if analysed is None:
                with contextlib.suppress(OSError):
                    path.unlink(missing_ok=True)
                return
            if not path.exists():
                return
            seen = {l for l in analysed.splitlines() if l.startswith("- ")}
            current = [l for l in path.read_text(encoding="utf-8").splitlines()
                       if l.startswith("- ")]
            remaining = [l for l in current if l not in seen]
            if remaining:
                _secure_write(path, "# Friction Log\n" + "\n".join(remaining) + "\n")
            else:
                with contextlib.suppress(OSError):
                    path.unlink(missing_ok=True)

    # ── Session logs ──────────────────────────────────────────────────────────

    def new_session_path(self) -> Path:
        # Microsecond resolution: two channel sessions starting in the same
        # second used to share (and interleave into) one file. The suffix keeps
        # names lexicographically ordered by time — the reflect watermark
        # compares stems as strings, and old-format stems still sort correctly
        # against new ones (the seconds part is compared first).
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        return self.root / "sessions" / f"session_{ts}.md"

    def log_session(self, path: Path, role: str, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.touch(mode=0o600)
        ts = datetime.now().strftime("%H:%M:%S")
        with path.open("a", encoding="utf-8") as f:
            f.write(f"\n\n**[{ts}] {role.upper()}**\n\n{_redact(content.strip())}\n")

    # ── Reflection support ───────────────────────────────────────────────────

    def unanalysed_sessions(self, watermark_file: str = "reflect_watermark",
                            settle_seconds: float = 0) -> list[Path]:
        """Sessions newer than the watermark that haven't been analysed yet.

        With `settle_seconds`, sessions modified more recently than that are
        left out — they may still be receiving turns. They are NOT skipped for
        good: mark_sessions_analysed() never advances the watermark past a
        session that wasn't analysed."""
        import time
        sessions_dir = self.root / "sessions"
        watermark    = self.root / "memory" / watermark_file
        all_sessions = sorted(sessions_dir.glob("session_*.md"))
        if watermark.exists():
            last_ts = watermark.read_text(encoding="utf-8").strip()
            all_sessions = [s for s in all_sessions if s.stem > last_ts]
        done = self._analysed_beyond_watermark(watermark_file)
        out = [s for s in all_sessions if s.stem not in done]
        if settle_seconds > 0:
            cutoff = time.time() - settle_seconds
            settled = []
            for s in out:
                try:
                    if s.stat().st_mtime <= cutoff:
                        settled.append(s)
                except OSError:
                    pass
            out = settled
        return out

    def _analysed_beyond_watermark(self, watermark_file: str) -> set[str]:
        """Stems analysed out of order (a still-active session sat between them
        and the watermark). Stored next to the watermark, one stem per line."""
        path = self.root / "memory" / f"{watermark_file}.done"
        if not path.exists():
            return set()
        return {l.strip() for l in path.read_text(encoding="utf-8").splitlines() if l.strip()}

    def mark_sessions_analysed(self, analysed: list[Path],
                               watermark_file: str = "reflect_watermark") -> None:
        """Record analysed sessions. The watermark advances only across a
        contiguous run of analysed sessions; any analysed after a gap (an
        unsettled session) are remembered in `<watermark>.done` until the gap
        closes, so the skipped session is picked up on a later run."""
        watermark = self.root / "memory" / watermark_file
        done_path = self.root / "memory" / f"{watermark_file}.done"
        with file_lock(watermark):
            last_ts = (watermark.read_text(encoding="utf-8").strip()
                       if watermark.exists() else "")
            done = self._analysed_beyond_watermark(watermark_file)
            done |= {p.stem for p in analysed}
            pending = sorted(s.stem for s in (self.root / "sessions").glob("session_*.md")
                             if s.stem > last_ts)
            new_ts = last_ts
            for stem in pending:
                if stem not in done:
                    break
                new_ts = stem
            if new_ts != last_ts:
                _secure_write(watermark, new_ts)
            rest = sorted(d for d in done if d > new_ts)
            if rest:
                _secure_write(done_path, "\n".join(rest) + "\n")
            else:
                with contextlib.suppress(OSError):
                    done_path.unlink(missing_ok=True)

    def update_watermark(self, session_path: Path, watermark_file: str = "reflect_watermark") -> None:
        watermark = self.root / "memory" / watermark_file
        with file_lock(watermark):
            _secure_write(watermark, session_path.stem)

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
