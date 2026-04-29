"""
aria/task.py — Task data model and queue file operations.

Tasks are stored as JSON files under ~/.aria/tasks/:

  pending/   ← ready to run (or scheduled for later)
  running/   ← being executed right now (crash-safe hand-off)
  done/      ← completed successfully
  failed/    ← failed after retries exhausted

File format (task_<id>.task):
  JSON object — handles any content in prompts without truncation issues.
"""

from __future__ import annotations

import json
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
    recur:       str        = ""            # "", "daily", "weekly", "weekdays", or "<N>m" (every N minutes)

    # ── Serialisation ─────────────────────────────────────────────────────────

    def to_text(self) -> str:
        return json.dumps({
            "prompt":      self.prompt,
            "notify":      self.notify,
            "priority":    self.priority,
            "run_after":   self.run_after,
            "max_retries": self.max_retries,
            "created":     self.created,
            "retries":     self.retries,
            "source":      self.source,
            "id":          self.task_id,
            "recur":       self.recur,
        }, indent=2, ensure_ascii=False)

    @staticmethod
    def from_text(text: str) -> "Task":
        """Parse a task file. Supports both JSON (current) and legacy key: value format."""
        text = text.strip()
        if text.startswith("{"):
            d = json.loads(text)
            return Task(
                prompt      = d.get("prompt", ""),
                notify      = bool(d.get("notify", True)),
                priority    = int(d.get("priority", 5)),
                run_after   = d.get("run_after", ""),
                max_retries = int(d.get("max_retries", 2)),
                created     = d.get("created", _now()),
                retries     = int(d.get("retries", 0)),
                source      = d.get("source", "user"),
                task_id     = d.get("id", uuid.uuid4().hex[:8]),
                recur       = d.get("recur", ""),
            )
        else:
            # Legacy key: value format
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
                recur       = kv.get("recur", ""),
            )

    def next_run_after(self) -> str:
        """
        Compute the next run_after value for a recurring task.
        Returns an ISO datetime string, or "" if not recurring.

        Supported recur values:
          "daily"    — same time tomorrow
          "weekly"   — same time next week
          "weekdays" — same time next weekday (Mon–Fri)
          "<N>m"     — every N minutes (e.g. "60m")
        """
        if not self.recur:
            return ""

        from datetime import timedelta
        base = datetime.fromisoformat(self.run_after) if self.run_after else datetime.now()
        # Strip timezone for consistent naive arithmetic
        if hasattr(base, "tzinfo") and base.tzinfo is not None:
            base = base.replace(tzinfo=None)

        recur = self.recur.strip().lower()

        if recur == "daily":
            nxt = base + timedelta(days=1)
        elif recur == "weekly":
            nxt = base + timedelta(weeks=1)
        elif recur == "weekdays":
            nxt = base + timedelta(days=1)
            while nxt.weekday() >= 5:  # skip Sat=5, Sun=6
                nxt += timedelta(days=1)
        elif recur.endswith("m") and recur[:-1].isdigit():
            nxt = base + timedelta(minutes=int(recur[:-1]))
        else:
            return ""

        return nxt.strftime("%Y-%m-%dT%H:%M:%S")

    def is_due(self) -> bool:
        """Return True if the task is ready to run right now."""
        if not self.run_after:
            return True
        try:
            run_at = datetime.fromisoformat(self.run_after)
            # If run_after has timezone info, compare against aware now.
            # If naive, compare against naive now. Never mix the two.
            if run_at.tzinfo is not None:
                from datetime import timezone
                return datetime.now(timezone.utc) >= run_at
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
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("Skipping malformed task %s: %s", p.name, exc)
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
    """Move a finished task to done/ and requeue if recurring."""
    done_dir = _queue_dir("done")
    d = json.loads(task.to_text())
    d["result"]    = result[:500]
    d["completed"] = _now()
    dest = done_dir / path.name
    dest.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
    path.unlink(missing_ok=True)

    # Auto-requeue recurring tasks with a fresh task_id and updated run_after
    if task.recur:
        next_run = task.next_run_after()
        if next_run:
            import uuid as _uuid
            next_task = Task(
                prompt      = task.prompt,
                notify      = task.notify,
                priority    = task.priority,
                run_after   = next_run,
                max_retries = task.max_retries,
                source      = task.source,
                recur       = task.recur,
                task_id     = _uuid.uuid4().hex[:8],
            )
            enqueue(next_task)


def fail(path: Path, task: Task, error: str) -> None:
    """Either requeue with incremented retry count, or move to failed/."""
    task.retries += 1
    if task.retries <= task.max_retries:
        path.unlink(missing_ok=True)
        enqueue(task)
    else:
        failed_dir = _queue_dir("failed")
        d = json.loads(task.to_text())
        d["error"]     = error[:500]
        d["failed_at"] = _now()
        (failed_dir / path.name).write_text(
            json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        path.unlink(missing_ok=True)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
