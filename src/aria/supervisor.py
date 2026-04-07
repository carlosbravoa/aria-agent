"""
aria/supervisor.py — Autonomous task supervisor.

Polls ~/.aria/tasks/pending/ for due tasks, executes them through the agent,
and routes results to Telegram or stdout.

Run as a background process:
  nohup aria-supervisor >> ~/.aria/supervisor.log 2>&1 &

Or as a systemd user service (see README).

Config via ~/.aria/.env:
  ARIA_SUPERVISOR_INTERVAL=30   # seconds between polls (default 30)
  ARIA_SUPERVISOR_WORKERS=2     # max concurrent tasks (default 1, sequential)

The supervisor is intentionally single-threaded by default. Most tasks involve
LLM calls which are already I/O-bound; parallelism adds complexity without
meaningful throughput gains for a personal agent.
"""

from __future__ import annotations

import logging
import os
import signal
import time
from pathlib import Path

log = logging.getLogger(__name__)

_INTERVAL = int(os.environ.get("ARIA_SUPERVISOR_INTERVAL", "30"))


class Supervisor:
    def __init__(self) -> None:
        self._running = True
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT,  self._handle_signal)

    def _handle_signal(self, signum: int, frame: object) -> None:
        log.info("Supervisor shutting down (signal %d)...", signum)
        self._running = False

    def run(self) -> None:
        from aria import config
        from aria.task import list_pending, claim, complete, fail

        config.load()

        log.info("Supervisor started. Polling every %ds.", _INTERVAL)
        log.info("Task queue: %s", _tasks_dir())

        while self._running:
            try:
                self._tick(list_pending, claim, complete, fail)
            except Exception as exc:
                log.error("Supervisor tick error: %s", exc)
            time.sleep(_INTERVAL)

        log.info("Supervisor stopped.")

    def _tick(self, list_pending, claim, complete, fail) -> None:
        pending = list_pending()
        if not pending:
            return

        log.info("%d task(s) due.", len(pending))

        for path, task in pending:
            running_path = claim(path, task)
            if running_path is None:
                continue  # claimed by another process (future multi-worker support)

            log.info(
                "Running task %s [priority=%d source=%s]: %s",
                task.task_id, task.priority, task.source,
                task.prompt[:80],
            )

            try:
                result = _execute(task)
                complete(running_path, task, result)
                log.info("Task %s done: %s", task.task_id, result[:120])
            except Exception as exc:
                log.error("Task %s failed: %s", task.task_id, exc)
                fail(running_path, task, str(exc))


def _execute(task) -> str:
    """Run the task prompt through the agent and optionally notify."""
    from aria.agent import Agent

    agent = Agent()
    result = agent.chat_collect(task.prompt)
    agent.close()

    # Strip the "<name>: " prefix chat_collect includes
    prefix = f"\n{agent.name}: "
    if result.startswith(prefix.strip()):
        result = result[len(prefix.strip()):].strip()

    if task.notify and result:
        try:
            from aria.telegram_notify import send
            send(f"📋 Task result:\n{result}")
        except Exception as exc:
            log.warning("Telegram notify failed: %s", exc)

    return result


def _tasks_dir() -> Path:
    from aria import config
    return config.workspace_dir().parent / "tasks"


def main() -> None:
    """CLI entry point: aria-supervisor"""
    import argparse

    from aria.setup import is_first_run, run as setup_run
    if is_first_run():
        setup_run()

    parser = argparse.ArgumentParser(
        prog="aria-supervisor",
        description="Autonomous task supervisor — polls and executes pending tasks.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process pending tasks once and exit (useful for cron)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show debug output",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.once:
        # Single-pass mode — run due tasks and exit
        from aria import config
        from aria.task import list_pending, claim, complete, fail
        config.load()
        for path, task in list_pending():
            running_path = claim(path, task)
            if running_path:
                try:
                    result = _execute(task)
                    complete(running_path, task, result)
                    print(f"✓ {task.task_id}: {result[:120]}")
                except Exception as exc:
                    fail(running_path, task, str(exc))
                    print(f"✗ {task.task_id}: {exc}")
    else:
        Supervisor().run()


if __name__ == "__main__":
    main()
