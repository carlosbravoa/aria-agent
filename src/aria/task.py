"""
aria/task.py — Task data model and queue file operations.

Tasks are plain TOML-like text files stored under ~/.aria/tasks/:

  pending/   ← ready to run (or scheduled for later)
  running/   ← being executed right now (crash-safe hand-off)
  done/      ← completed successfully
  failed/    ← failed after retries exhausted

File format (task_<id>.task):

  prompt: check my unread emails and summarise them
  notify: true
  priority: 5
  run_after: 2026-04-10T08:00:00
  max_retries: 2
  created: 2026-04-09T22:00:00
  retries: 0
  source: cron
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


# ── Task dataclass ────────────────────────────────────────────────────────────

@dataclass
class Task:
    prompt:      str                        # what to ask the agent
    notify:      bool       = True          # send result via Telegram
    priority:    int        = 5             # 1 (highest) – 10 (lowest)
    run_after:   str        = ""            # ISO datetime, empty = run now
    max_retries: int        = 2
    created:     str        = field(default_factory=lambda: _now())
    retries:     int        = 0
    source:      str        = "user"        # cron | agent | user | script
    task_id:     str        = field(default_factory=lambda: uuid.uuid4().hex[:8])

    # ── Serialisation ─────────────────────────────────────────────────────────

    def to_text(self) -> str:
        lines = [
            f"prompt: {self.prompt}",
            f"notify: {str(self.notify).lower()}",
            f"priority: {self.priority}",
            f"run_after: {self.run_after}",
            f"max_retries: {self.max_retries}",
            f"created: {self.created}",
            f"retries: {self.retries}",
            f"source: {self.source}",
            f"id: {self.task_id}",
        ]
        return "\n".join(lines) + "\n"

    @staticmethod
    def from_text(text: str) -> "Task":
        kv: dict[str, str] = {}
        for line in text.splitlines():
            if ":" in line:
                key, _, val = line.partition(":")
                kv[key.strip()] = val.strip()
        return Task(
            prompt      = kv.get("prompt", ""),
            notify      = kv.get("notify", "true").lower() == "true",
            priority    = int(kv.get("priority", "5")),
            run_after   = kv.get("run_after", ""),
            max_retries = int(kv.get("max_retries", "2")),
            created     = kv.get("created", _now()),
            retries     = int(kv.get("retries", "0")),
            source      = kv.get("source", "user"),
            task_id     = kv.get("id", uuid.uuid4().hex[:8]),
        )

    def is_due(self) -> bool:
        """Return True if the task is ready to run right now."""
        if not self.run_after:
            return True
        try:
            run_at = datetime.fromisoformat(self.run_after)
            return datetime.now() >= run_at
        except ValueError:
            return True  # malformed date → run immediately

    def filename(self) -> str:
        # priority prefix so sorted() gives natural execution order
        return f"{self.priority:02d}_{self.task_id}.task"


# ── Queue helpers ─────────────────────────────────────────────────────────────

def tasks_dir() -> Path:
    from aria import config
    return config.workspace_dir().parent / "tasks"


def _queue_dir(state: str) -> Path:
    d = tasks_dir() / state
    d.mkdir(parents=True, exist_ok=True)
    return d


def enqueue(task: Task) -> Path:
    """Write a task file to pending/. Returns the path."""
    path = _queue_dir("pending") / task.filename()
    path.write_text(task.to_text(), encoding="utf-8")
    return path


def list_pending() -> list[tuple[Path, Task]]:
    """Return due tasks from pending/, sorted by priority then creation time."""
    pending = _queue_dir("pending")
    results = []
    for p in sorted(pending.glob("*.task")):
        try:
            task = Task.from_text(p.read_text(encoding="utf-8"))
            if task.is_due():
                results.append((p, task))
        except Exception:
            pass  # skip malformed files
    return results


def claim(path: Path, task: Task) -> Path | None:
    """
    Atomically move a task from pending/ to running/.
    Returns the new path, or None if another process claimed it first.
    """
    dest = _queue_dir("running") / path.name
    try:
        path.rename(dest)
        return dest
    except FileNotFoundError:
        return None  # already claimed by another process


def complete(path: Path, task: Task, result: str) -> None:
    """Move a finished task to done/ and append the result."""
    done_dir = _queue_dir("done")
    text = task.to_text() + f"\nresult: {result[:500]}\ncompleted: {_now()}\n"
    dest = done_dir / path.name
    dest.write_text(text, encoding="utf-8")
    path.unlink(missing_ok=True)


def fail(path: Path, task: Task, error: str) -> None:
    """
    Either requeue with incremented retry count, or move to failed/.
    """
    task.retries += 1
    if task.retries <= task.max_retries:
        # Requeue to pending with updated retry count
        path.unlink(missing_ok=True)
        enqueue(task)
    else:
        failed_dir = _queue_dir("failed")
        text = task.to_text() + f"\nerror: {error[:500]}\nfailed_at: {_now()}\n"
        (failed_dir / path.name).write_text(text, encoding="utf-8")
        path.unlink(missing_ok=True)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
