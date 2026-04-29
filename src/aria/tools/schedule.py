"""
aria/tools/schedule.py — Schedule, list, and cancel tasks for the supervisor.
"""

from __future__ import annotations

DEFINITION = {
    "name": "schedule",
    "description": (
        "Manage scheduled tasks for the supervisor. "
        "Actions: "
        "create — schedule a new task; "
        "list — show all pending tasks (use this when the user asks what reminders or tasks are scheduled); "
        "cancel — cancel a pending task by its ID."
        "\n"
        "For recurring tasks use the 'recur' field — the supervisor requeues automatically. "
        "Never reschedule manually inside a task."
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
                "description": "Task ID to cancel (required for cancel). Get it from list.",
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
    from aria.task import Task, enqueue

    prompt = args.get("prompt", "").strip()
    if not prompt:
        return "[schedule] 'prompt' is required for create."

    task = Task(
        prompt      = prompt,
        notify      = args.get("notify", True),
        priority    = int(args.get("priority", 5)),
        run_after   = args.get("run_after", ""),
        max_retries = int(args.get("max_retries", 2)),
        recur       = args.get("recur", ""),
        source      = "agent",
    )
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
                recur    = f" [{task.recur}]" if task.recur else ""
                rows.append(
                    f"- [{state}] id={task.task_id} run_after={when}{recur}: {task.prompt[:80]}"
                )
            except Exception:
                rows.append(f"- [{state}] {p.name} (malformed)")

    if not rows:
        return "[schedule] No pending tasks."
    return "\n".join(rows)


def _cancel_task(task_id: str) -> str:
    from aria.task import tasks_dir

    if not task_id:
        return "[schedule] 'task_id' is required for cancel."

    for state in ("pending", "running"):
        directory = tasks_dir() / state
        if not directory.exists():
            continue
        for p in directory.glob("*.task"):
            if task_id in p.name:
                cancelled_dir = tasks_dir() / "cancelled"
                cancelled_dir.mkdir(exist_ok=True)
                p.rename(cancelled_dir / p.name)
                return f"[schedule] Task {task_id} cancelled."

    return f"[schedule] Task {task_id} not found in pending or running."
