"""
aria/supervisor.py — Autonomous task supervisor.

Responsibilities:
  1. Poll ~/.aria/tasks/pending/ for due tasks and execute them.
  2. Run built-in periodic jobs on a schedule (no crontab needed).

Built-in periodic jobs (all configurable via ~/.aria/.env):
  - Memory reflection: ARIA_REFLECT_EVERY=86400  (seconds, default 24h)

Config:
  ARIA_SUPERVISOR_INTERVAL=30   # poll interval in seconds (default 30)
  ARIA_REFLECT_EVERY=86400      # seconds between reflection runs (0 = disabled)
  ARIA_REFLECT_NOTIFY=true      # send Telegram notification after reflection

Run as a background process:
  nohup aria-supervisor >> ~/.aria/supervisor.log 2>&1 &
  # or: aria-install  (sets up systemd service automatically)
"""

from __future__ import annotations

import logging
import os
import signal
import time
from pathlib import Path

log = logging.getLogger(__name__)

_INTERVAL      = int(os.environ.get("ARIA_SUPERVISOR_INTERVAL", "30"))
_REFLECT_EVERY = int(os.environ.get("ARIA_REFLECT_EVERY",       "86400"))  # 24h
_REFLECT_NOTIFY = os.environ.get("ARIA_REFLECT_NOTIFY", "true").lower() == "true"


# ── Periodic job registry ─────────────────────────────────────────────────────

class _PeriodicJob:
    """Runs a function every `interval` seconds. Skipped if interval is 0."""

    def __init__(self, name: str, interval: int, fn) -> None:
        self.name     = name
        self.interval = interval
        self.fn       = fn
        self._last_run: float = 0.0  # run immediately on first tick

    def tick(self, now: float) -> None:
        if self.interval <= 0:
            return
        if now - self._last_run >= self.interval:
            log.info("Periodic job: %s", self.name)
            try:
                self.fn()
                self._last_run = now
            except Exception as exc:
                log.error("Periodic job %s failed: %s", self.name, exc)
                self._last_run = now  # don't hammer on failure


def _run_reflection() -> None:
    from aria.reflect import run as reflect_run
    result = reflect_run(notify=_REFLECT_NOTIFY)
    log.info("Reflection: %s", result)


# ── Supervisor ────────────────────────────────────────────────────────────────

class Supervisor:
    def __init__(self) -> None:
        self._running = True
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT,  self._handle_signal)

        # Built-in periodic jobs — extend this list to add more
        self._periodic: list[_PeriodicJob] = [
            _PeriodicJob("reflection", _REFLECT_EVERY, _run_reflection),
        ]

    def _handle_signal(self, signum: int, frame: object) -> None:
        log.info("Supervisor shutting down (signal %d)...", signum)
        self._running = False

    def run(self) -> None:
        from aria import config
        from aria.task import list_pending, claim, complete, fail

        config.load()

        log.info("Supervisor started. Poll interval: %ds.", _INTERVAL)
        if _REFLECT_EVERY > 0:
            log.info(
                "Reflection: every %dh%s.",
                _REFLECT_EVERY // 3600,
                " (with Telegram notify)" if _REFLECT_NOTIFY else "",
            )
        else:
            log.info("Reflection: disabled (ARIA_REFLECT_EVERY=0).")
        log.info("Task queue: %s", _tasks_dir())

        while self._running:
            now = time.monotonic()
            try:
                # 1. Built-in periodic jobs
                for job in self._periodic:
                    job.tick(now)
                # 2. Task queue
                self._tick(list_pending, claim, complete, fail)
            except Exception as exc:
                log.error("Supervisor error: %s", exc)
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
                continue

            log.info(
                "Running task %s [priority=%d source=%s]: %s",
                task.task_id, task.priority, task.source, task.prompt[:80],
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

    agent  = Agent()
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
        description="Autonomous task supervisor — executes tasks and runs periodic jobs.",
    )
    parser.add_argument("--once",    action="store_true", help="Process pending tasks once and exit")
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug output")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.once:
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
