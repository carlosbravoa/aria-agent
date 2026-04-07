"""
aria/tools/schedule.py — Schedule a task for the supervisor to execute.

Allows the agent to proactively create follow-up work, schedule reminders,
or set up periodic checks — all without requiring the user to be present.

Examples the agent might use this for:
  - "I'll check on that ticket tomorrow morning"
  - "Schedule a weekly email digest every Monday"
  - "Run the reflection pass in 2 hours"
"""

from __future__ import annotations

DEFINITION = {
    "name": "schedule",
    "description": (
        "Schedule a task for autonomous execution by the supervisor. "
        "The supervisor will run the prompt through the agent at the specified time "
        "and optionally send the result via Telegram. "
        "Use this to create follow-up tasks, reminders, or periodic checks."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "The task to execute — written as a clear instruction.",
            },
            "run_after": {
                "type": "string",
                "description": (
                    "When to run, as ISO datetime: 2026-04-10T08:00:00. "
                    "Leave empty to run as soon as possible."
                ),
            },
            "notify": {
                "type": "boolean",
                "description": "Send the result to Telegram when done. Default: true.",
                "default": True,
            },
            "priority": {
                "type": "integer",
                "description": "Priority 1 (urgent) to 10 (low). Default: 5.",
                "default": 5,
            },
            "max_retries": {
                "type": "integer",
                "description": "How many times to retry on failure. Default: 2.",
                "default": 2,
            },
        },
        "required": ["prompt"],
    },
}


def execute(args: dict) -> str:
    from aria.task import Task, enqueue

    task = Task(
        prompt      = args["prompt"],
        notify      = args.get("notify", True),
        priority    = int(args.get("priority", 5)),
        run_after   = args.get("run_after", ""),
        max_retries = int(args.get("max_retries", 2)),
        source      = "agent",
    )

    try:
        path = enqueue(task)
        when = f" at {task.run_after}" if task.run_after else " as soon as possible"
        return f"[schedule] Task {task.task_id} queued{when}: {task.prompt[:80]}"
    except Exception as exc:
        return f"[schedule error] {exc}"
