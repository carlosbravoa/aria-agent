"""
aria/tools/schedule.py — Schedule, list, and cancel tasks for the supervisor.

Duplicate guards (recurring tasks used to multiply):
  - create is idempotent: an equivalent queued task (same prompt + recurrence)
    is reported instead of a second copy being enqueued;
  - inside a running supervisor task (ARIA_TASK_ID set) creating a RECURRING
    task is refused — a recurring prompt like "every morning, …" otherwise made
    each run schedule another copy of itself;
  - cancel takes a task id or series id and stops the whole series.
"""

from __future__ import annotations

import os

DEFINITION = {
    "name": "schedule",
    "description": (
        "Manage scheduled tasks for the supervisor. "
        "Actions: "
        "create — schedule a new task; "
        "list — show all pending tasks (use this when the user asks what reminders or tasks are scheduled); "
        "cancel — cancel a task by its ID or series ID (cancelling a recurring "
        "task stops all its future runs)."
        "\n"
        "For recurring tasks use the 'recur' field — the supervisor requeues automatically. "
        "Never reschedule manually inside a task. To change a recurring task, cancel "
        "the old one first, then create the new one."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "list", "cancel"],
                "description": "Operation to perform. Default: create.",
                "default": "create",
            },
            "prompt": {
                "type": "string",
                "description": "The task instruction (required for create).",
            },
            "task_id": {
                "type": "string",
                "description": "Task ID or series ID to cancel (required for cancel). Get it from list.",
            },
            "run_after": {
                "type": "string",
                "description": "When to run, ISO datetime: 2026-04-10T08:00:00. Empty = run now.",
            },
            "recur": {
                "type": "string",
                "description": (
                    "Recurrence: 'daily', 'weekly', 'weekdays', or '<N>m' (e.g. '60m'). "
                    "Empty = one-shot."
                ),
                "default": "",
            },
            "notify": {
                "type": "boolean",
                "description": "Send result to Telegram when done. Default: true.",
                "default": True,
            },
            "priority": {
                "type": "integer",
                "description": "Priority 1 (urgent) to 10 (low). Default: 5.",
                "default": 5,
            },
            "max_retries": {
                "type": "integer",
                "description": "Retry count on failure. Default: 2.",
                "default": 2,
            },
        },
        "required": [],
    },
}


def execute(args: dict) -> str:
    action = args.get("action", "create")

    if action == "list":
        return _list_tasks()
    elif action == "cancel":
        return _cancel_task(args.get("task_id", ""))
    else:
        return _create_task(args)


def _create_task(args: dict) -> str:
    from datetime import datetime
    from aria.task import Task, enqueue, find_duplicate, recur_step

    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        return "[schedule] 'prompt' is required for create."

    recur = (args.get("recur") or "").strip().lower()
    if recur and recur_step(recur) is None:
        return (f"[schedule error] invalid recur {recur!r}. Use 'daily', 'weekly', "
                "'weekdays', or '<N>m' (e.g. '60m' for hourly).")
    run_after = (args.get("run_after") or "").strip()
    if run_after:
        try:
            datetime.fromisoformat(run_after)
        except ValueError:
            return (f"[schedule error] invalid run_after {run_after!r}. "
                    "Use ISO format, e.g. 2026-04-10T08:00:00.")

    running = os.environ.get("ARIA_TASK_ID", "")
    if running and recur:
        return ("[schedule error] this is already a scheduled task (id "
                f"{running}); its recurrence is handled automatically. Do not "
                "create recurring tasks from inside a task.")

    dup = find_duplicate(prompt, recur, run_after)
    if dup is not None:
        ref = f"series {dup.series_id}" if dup.series_id else f"id {dup.task_id}"
        return (f"[schedule] Already scheduled ({ref}, run_after="
                f"{dup.run_after or 'now'}) — not creating a duplicate.")

    try:
        task = Task(
            prompt      = prompt,
            notify      = bool(args.get("notify", True)),
            priority    = min(10, max(1, int(args.get("priority", 5)))),
            run_after   = run_after,
            max_retries = max(0, int(args.get("max_retries", 2))),
            recur       = recur,
            source      = "agent",
        )
    except (TypeError, ValueError) as exc:
        return f"[schedule error] {exc}"
    try:
        enqueue(task)
        recur_str = f", recurs {task.recur}" if task.recur else ""
        when      = f" at {task.run_after}" if task.run_after else " as soon as possible"
        return f"[schedule] Task {task.task_id} queued{when}{recur_str}: {task.prompt[:80]}"
    except Exception as exc:
        return f"[schedule error] {exc}"


def _list_tasks() -> str:
    from aria.task import tasks_dir, Task
    import json

    pending_dir = tasks_dir() / "pending"
    running_dir = tasks_dir() / "running"

    rows = []
    for state, directory in [("pending", pending_dir), ("running", running_dir)]:
        if not directory.exists():
            continue
        for p in sorted(directory.glob("*.task")):
            try:
                task = Task.from_text(p.read_text(encoding="utf-8"))
                when     = task.run_after or "now"
                recur    = (f" [{task.recur}, series={task.series_id}]"
                            if task.recur else "")
                rows.append(
                    f"- [{state}] id={task.task_id} run_after={when}{recur}: {task.prompt[:80]}"
                )
            except Exception:
                rows.append(f"- [{state}] {p.name} (malformed)")

    if not rows:
        return "[schedule] No pending tasks."
    return "\n".join(rows)


def _cancel_task(task_id: str) -> str:
    from aria.task import cancel

    task_id = (task_id or "").strip()
    if not task_id:
        return "[schedule] 'task_id' is required for cancel."

    cancelled = cancel(task_id)
    if not cancelled:
        return f"[schedule] Task {task_id} not found in pending or running."
    if cancelled == [task_id]:
        return f"[schedule] Task {task_id} cancelled."
    return (f"[schedule] Cancelled {len(cancelled)} queued task(s) for {task_id} "
            f"(ids: {', '.join(cancelled)}); the series will not run again.")
