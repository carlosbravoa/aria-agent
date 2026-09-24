"""
aria/task.py — Task data model and queue file operations.

Tasks are stored as JSON files under ~/.aria/tasks/:

  pending/   ← ready to run (or scheduled for later)
  running/   ← being executed right now (crash-safe hand-off)
  done/      ← completed successfully
  failed/    ← failed after retries exhausted
  cancelled/ ← cancelled by the user

Recurring tasks form a *series*: every occurrence carries the same series_id
(stable across requeues) and a scheduled_for slot time, so cancelling by series
stops future runs, and a retried/late occurrence never shifts the schedule.

File format (task_<id>.task):
  JSON object — handles any content in prompts without truncation issues.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


# ── Timezone handling ─────────────────────────────────────────────────────────
# All new timestamps are stored as timezone-aware ISO strings so DST can't
# shift recurring tasks. The zone is ARIA_TZ (IANA name, e.g. Europe/Madrid),
# falling back to the system local timezone. Legacy naive timestamps in
# existing task files are interpreted as local time on parse — never a crash.

def _tz():
    """Active timezone: ARIA_TZ if set and valid, else the system local zone."""
    name = os.environ.get("ARIA_TZ", "").strip()
    if name:
        try:
            return ZoneInfo(name)
        except Exception:
            logging.getLogger(__name__).warning(
                "Invalid ARIA_TZ %r — falling back to system local timezone", name
            )
    return datetime.now().astimezone().tzinfo


def _now_dt() -> datetime:
    """Timezone-aware 'now' in the active timezone."""
    return datetime.now(_tz())


def _parse_dt(value: str) -> datetime:
    """Parse an ISO timestamp; naive (legacy) values are treated as local time."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz())
    return dt


def recur_step(recur: str) -> timedelta | None:
    """The interval for a recurrence spec, or None if it is not a valid one.
    Valid: "daily", "weekly", "weekdays", "<N>m" with N > 0."""
    r = (recur or "").strip().lower()
    if r in ("daily", "weekdays"):
        return timedelta(days=1)
    if r == "weekly":
        return timedelta(weeks=1)
    if r.endswith("m") and r[:-1].isdigit() and int(r[:-1]) > 0:
        return timedelta(minutes=int(r[:-1]))
    return None


def task_timeout() -> int:
    """Per-task wall-clock ceiling AND running/ lease, in seconds
    (ARIA_TASK_TIMEOUT, default 900). <=0 disables both."""
    return int(os.environ.get("ARIA_TASK_TIMEOUT", "900"))


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
    started_at:  str        = ""            # ISO datetime stamped by claim() (running/ lease start)
    series_id:   str        = ""            # stable id shared by every occurrence of a recurring task
    scheduled_for: str      = ""            # the slot this occurrence belongs to (retries don't move it)

    def __post_init__(self) -> None:
        if self.recur and not self.series_id:
            self.series_id = self.task_id

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
            "started_at":  self.started_at,
            "series_id":   self.series_id,
            "scheduled_for": self.scheduled_for,
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
                started_at  = d.get("started_at", ""),
                series_id   = d.get("series_id", ""),
                scheduled_for = d.get("scheduled_for", ""),
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
                started_at  = kv.get("started_at", ""),
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

        # Arithmetic is always done timezone-aware: adding a timedelta to an
        # aware ZoneInfo datetime is wall-clock arithmetic (the UTC offset is
        # re-derived), so "daily at 08:00" stays 08:00 across a DST change.
        # Legacy naive bases are localized for the maths but keep a naive
        # output so old task files stay format-stable.
        # The base is the occurrence's slot (scheduled_for), NOT run_after: a
        # retry pushes run_after forward by its backoff, and basing the next
        # occurrence on it made "daily at 08:00" drift to 08:01, 08:03, …
        naive_base = False
        base = None
        anchor = self.scheduled_for or self.run_after
        if anchor:
            try:
                base = datetime.fromisoformat(anchor)
            except ValueError:
                base = None
            else:
                if base.tzinfo is None:
                    naive_base = True
                    base = base.replace(tzinfo=_tz())
        if base is None:
            base = _now_dt()

        recur = self.recur.strip().lower()
        step = recur_step(recur)
        if step is None:
            return ""

        # Advance strictly past 'now'. Without this, a task whose run_after is in
        # the past (e.g. the supervisor was down for a while) would schedule a
        # next time that is ALSO in the past, re-firing repeatedly to "catch up"
        # — a burst of runs + duplicate notifications. The task still runs once
        # on resume (it was due); this only schedules the NEXT occurrence ahead.
        now = _now_dt()
        nxt = base + step
        while nxt <= now:
            nxt += step
        if recur == "weekdays":
            while nxt.weekday() >= 5:  # land on a weekday (skip Sat=5, Sun=6)
                nxt += timedelta(days=1)

        if naive_base:
            return nxt.replace(tzinfo=None).isoformat(timespec="seconds")
        return nxt.isoformat(timespec="seconds")

    def is_due(self) -> bool:
        """Return True if the task is ready to run right now."""
        if not self.run_after:
            return True
        try:
            # Aware vs aware, always: legacy naive values are localized by
            # _parse_dt, so the comparison never mixes naive and aware.
            return _now_dt() >= _parse_dt(self.run_after)
        except ValueError:
            return True  # malformed date → run immediately

    def next_occurrence(self) -> "Task | None":
        """The next occurrence of this recurring task (same series, fresh id and
        retry budget), or None if it does not recur."""
        next_run = self.next_run_after()
        if not next_run:
            return None
        return Task(
            prompt        = self.prompt,
            notify        = self.notify,
            priority      = self.priority,
            run_after     = next_run,
            max_retries   = self.max_retries,
            source        = self.source,
            recur         = self.recur,
            series_id     = self.series_id or self.task_id,
            scheduled_for = next_run,
        )

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
    except FileNotFoundError:
        return None  # already claimed by another process
    # Stamp the lease start so the reaper can detect an orphaned task after a
    # crash. Best-effort: if the rewrite fails, the file mtime (set by the
    # rename) still serves as the reaper's fallback.
    task.started_at = _now()
    if not task.scheduled_for:
        # First claim of this occurrence: pin its slot so retries (which move
        # run_after) can't shift the recurrence.
        task.scheduled_for = task.run_after or task.started_at
    try:
        dest.write_text(task.to_text(), encoding="utf-8")
    except OSError:
        pass
    return dest


def reap_running() -> list[str]:
    """
    Crash recovery: scan running/ for tasks whose lease has expired — a crash
    mid-execution orphans the file there forever otherwise. Any task older than
    ARIA_TASK_TIMEOUT is handed to fail(): retries left → requeued to pending/
    with backoff; exhausted → moved to failed/ with a note. The lease start is
    the started_at stamp written by claim(); files without one (pre-2.5) fall
    back to the file mtime. Returns human-readable notes for logging.
    """
    timeout = task_timeout()
    if timeout <= 0:
        return []
    notes: list[str] = []
    now = _now_dt()
    for p in sorted(_queue_dir("running").glob("*.task")):
        try:
            task = Task.from_text(p.read_text(encoding="utf-8"))
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "Reaper: skipping malformed running task %s: %s", p.name, exc
            )
            continue
        started = None
        if task.started_at:
            try:
                started = _parse_dt(task.started_at)
            except ValueError:
                pass
        if started is None:
            try:
                started = datetime.fromtimestamp(p.stat().st_mtime, tz=_tz())
            except OSError:
                continue  # vanished mid-scan — finished by its owner
        age = int((now - started).total_seconds())
        if age < timeout:
            continue
        fail(p, task, f"reaped: stale in running/ for {age}s (lease {timeout}s)")
        outcome = "requeued" if task.retries <= task.max_retries else "moved to failed/"
        notes.append(f"task {task.task_id} stale in running/ for {age}s → {outcome}")
    return notes


def complete(path: Path, task: Task, result: str) -> None:
    """Move a finished task to done/ and requeue if recurring.

    If the running file vanished while the task ran, it was cancelled
    (schedule cancel moves it to cancelled/) — the series is NOT requeued."""
    cancelled = not path.exists()
    done_dir = _queue_dir("done")
    d = json.loads(task.to_text())
    d["result"]    = result[:500]
    d["completed"] = _now()
    dest = done_dir / path.name
    dest.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
    path.unlink(missing_ok=True)

    if not cancelled:
        _requeue_series(task)


def _requeue_series(task: Task) -> None:
    """Enqueue the next occurrence of a recurring task — unless that series
    already has an occurrence pending or running (e.g. a reaper and the owner
    both finishing the same task), which would fork it into two copies."""
    nxt = task.next_occurrence()
    if nxt is None:
        return
    if find_series(nxt.series_id, states=("pending", "running")):
        logging.getLogger(__name__).warning(
            "Series %s already queued — not requeuing a duplicate", nxt.series_id
        )
        return
    enqueue(nxt)


def fail(path: Path, task: Task, error: str) -> None:
    """
    Either requeue with incremented retry count, or move to failed/.

    Requeues get exponential backoff: run_after = now + base * 2^(attempt-1)
    seconds (base ARIA_TASK_RETRY_BASE, default 60; capped at
    ARIA_TASK_RETRY_MAX, default 3600). Without it a deterministic failure
    burns every retry back-to-back within one supervisor tick.
    """
    task.retries += 1
    task.started_at = ""                    # lease is over either way
    if task.retries <= task.max_retries:
        if not path.exists():
            return                          # cancelled while running — don't resurrect it
        base = int(os.environ.get("ARIA_TASK_RETRY_BASE", "60"))
        cap  = int(os.environ.get("ARIA_TASK_RETRY_MAX",  "3600"))
        delay = min(base * (2 ** (task.retries - 1)), cap)
        task.run_after = (_now_dt() + timedelta(seconds=delay)).isoformat(timespec="seconds")
        path.unlink(missing_ok=True)
        enqueue(task)
    else:
        failed_dir = _queue_dir("failed")
        d = json.loads(task.to_text())
        d["error"]     = error[:500]
        d["failed_at"] = _now()
        cancelled = not path.exists()
        (failed_dir / path.name).write_text(
            json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        path.unlink(missing_ok=True)
        # One bad occurrence must not end a recurring series forever: the next
        # slot is scheduled with a fresh retry budget.
        if not cancelled:
            _requeue_series(task)


def _now() -> str:
    return _now_dt().isoformat(timespec="seconds")


# ── Lookup / dedupe / cancel ──────────────────────────────────────────────────

def _iter_state(state: str):
    d = tasks_dir() / state
    if not d.exists():
        return
    for p in sorted(d.glob("*.task")):
        try:
            yield p, Task.from_text(p.read_text(encoding="utf-8"))
        except Exception:
            continue


def find_series(series_id: str,
                states: tuple[str, ...] = ("pending", "running")) -> list[tuple[Path, Task]]:
    """Queued occurrences belonging to a recurring series."""
    if not series_id:
        return []
    return [(p, t) for st in states for p, t in _iter_state(st)
            if (t.series_id or t.task_id) == series_id]


def _norm_prompt(text: str) -> str:
    return " ".join((text or "").lower().split())


def _slot_key(recur: str, when: str) -> str:
    """The recurring slot a time belongs to: "HH:MM" for daily/weekdays,
    "<weekday> HH:MM" for weekly, "" for minute intervals. The same prompt at
    08:00 and 20:00 daily is two legitimate jobs, not a duplicate."""
    r = (recur or "").strip().lower()
    if not when or (r.endswith("m") and r[:-1].isdigit()):
        return ""
    try:
        dt = _parse_dt(when)
    except ValueError:
        return when
    hm = dt.strftime("%H:%M")
    return f"{dt.weekday()} {hm}" if r == "weekly" else hm


def _task_slot(t: Task) -> str:
    return _slot_key(t.recur, t.scheduled_for or t.run_after)


def find_duplicate(prompt: str, recur: str = "",
                   run_after: str = "") -> Task | None:
    """An already-queued task equivalent to the one about to be created: same
    (whitespace/case-normalised) prompt, same recurrence, and the same slot
    (time of day for recurring tasks, exact run_after for one-shots)."""
    want = _norm_prompt(prompt)
    rec = (recur or "").strip().lower()
    slot = _slot_key(rec, run_after) if rec else (run_after or "")
    for st in ("pending", "running"):
        for _, t in _iter_state(st):
            if _norm_prompt(t.prompt) != want or t.recur.strip().lower() != rec:
                continue
            if (_task_slot(t) if rec else (t.run_after or "")) == slot:
                return t
    return None


def cancel(ident: str) -> list[str]:
    """Cancel by task id OR series id. Cancelling any occurrence of a recurring
    task cancels the whole series (every pending/running occurrence) — the old
    id-per-occurrence scheme meant yesterday's id no longer matched anything
    and the series lived on. Returns the ids of the cancelled task files."""
    ident = (ident or "").strip()
    if not ident:
        return []
    series = ""
    for st in ("pending", "running"):
        for _, t in _iter_state(st):
            if t.task_id == ident and t.series_id:
                series = t.series_id
    done: list[str] = []
    dest = _queue_dir("cancelled")
    for st in ("pending", "running"):
        for p, t in _iter_state(st):
            # Filename is "<priority>_<task_id>.task"; match the id EXACTLY
            # (from the file or its name), never as a substring.
            file_id = p.stem.split("_", 1)[1] if "_" in p.stem else p.stem
            if (ident in (t.task_id, file_id)
                    or (t.series_id and t.series_id in (ident, series))):
                try:
                    p.rename(dest / p.name)
                    done.append(file_id)
                except FileNotFoundError:
                    pass      # finished/claimed in the meantime
    return done


def dedupe_pending() -> list[str]:
    """Collapse duplicate recurring tasks already in pending/: tasks with the
    same normalised prompt, recurrence and slot (time of day) are one logical
    job. The earliest
    occurrence is kept, the rest move to cancelled/. Cleans up queues that
    multiplied before the create-time guards existed. Returns log notes."""
    seen: dict[tuple[str, str, str], Task] = {}
    notes: list[str] = []
    rows = sorted(_iter_state("pending"),
                  key=lambda pt: (pt[1].run_after or "", pt[1].created))
    for p, t in rows:
        if not t.recur:
            continue
        key = (_norm_prompt(t.prompt), t.recur.strip().lower(), _task_slot(t))
        if key not in seen:
            seen[key] = t
            continue
        try:
            p.rename(_queue_dir("cancelled") / p.name)
        except FileNotFoundError:
            continue
        notes.append(f"task {t.task_id} duplicates series "
                     f"{seen[key].series_id or seen[key].task_id} → cancelled")
    return notes
