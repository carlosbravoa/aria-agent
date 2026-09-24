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
  ARIA_TASK_TIMEOUT=900         # per-task wall-clock ceiling AND running/ lease (0 = off)
  ARIA_TASK_RETRY_BASE=60       # retry backoff base in seconds (exponential)
  ARIA_TASK_RETRY_MAX=3600      # retry backoff cap in seconds
  ARIA_TZ=Europe/Madrid         # IANA timezone for scheduling (default: system local)

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
    """Runs a function every `interval` seconds (wall-clock). Skipped if interval
    is 0. The last-run timestamp is persisted so the schedule survives process
    restarts — previously it used time.monotonic() with last_run=0, so the job
    fired on EVERY restart regardless of the interval."""

    def __init__(self, name: str, interval: int, fn) -> None:
        from pathlib import Path
        self.name      = name
        self.interval  = interval
        self.fn        = fn
        self._state    = Path.home() / ".aria" / f".periodic_{name}"
        self._last_run = self._load_last_run()

    def _load_last_run(self) -> float:
        try:
            return float(self._state.read_text(encoding="utf-8").strip())
        except Exception:
            return 0.0  # never run → fire on first tick

    def _save_last_run(self, ts: float) -> None:
        try:
            self._state.parent.mkdir(parents=True, exist_ok=True)
            self._state.write_text(str(ts), encoding="utf-8")
        except Exception:
            pass

    def tick(self, now: float) -> None:
        if self.interval <= 0:
            return
        if now - self._last_run >= self.interval:
            log.info("Periodic job: %s", self.name)
            try:
                self.fn()
            except Exception as exc:
                log.error("Periodic job %s failed: %s", self.name, exc)
            self._last_run = now           # update even on failure — don't hammer
            self._save_last_run(now)


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
            now = time.time()           # wall-clock: comparable across restarts
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
        # Crash recovery first: requeue/fail tasks a previous process left
        # orphaned in running/ before claiming any new work.
        from aria.task import reap_running, dedupe_pending
        for note in reap_running():
            log.warning("Reaper: %s", note)
        for note in dedupe_pending():
            log.warning("Dedupe: %s", note)

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


# ── Killable per-task execution ────────────────────────────────────────────────

# Grace period (seconds) between SIGTERM (terminate) and SIGKILL (kill) when a
# task overruns its timeout. Kept short — a well-behaved child dies on SIGTERM
# almost immediately; this only backstops one that ignores it.
_KILL_GRACE = 3


def _process_entry(queue, target, args) -> None:
    """Child-process trampoline: run `target(*args)` and marshal the outcome
    back through `queue` as a 3-tuple ``(status, payload, traceback)``:

      ("ok",    result,    "")   — success
      ("error", exception, tb)   — target raised; the exception is pickled so
                                    the parent can re-raise the original type

    Runs as a module-level function so it survives both fork and spawn/
    forkserver start methods (a closure or lambda could not be pickled). We
    exit with os._exit after flushing the queue so a forked child never runs
    the parent's atexit handlers (e.g. pytest teardown)."""
    import os
    import pickle
    import traceback as _tb

    try:
        result = target(*args)
        payload = ("ok", result, "")
    except BaseException as exc:  # noqa: BLE001 — marshal anything back
        tb = _tb.format_exc()
        try:
            pickle.dumps(exc)                    # ensure the parent can unpickle it
            payload = ("error", exc, tb)
        except Exception:                        # unpicklable exception → stringify
            payload = ("error", RuntimeError(f"{type(exc).__name__}: {exc}"), tb)

    try:
        queue.put(payload)
        queue.close()
        queue.join_thread()                      # flush the pipe before exiting
    finally:
        os._exit(0)


def run_with_timeout(target, args: tuple = (), timeout: int | None = None):
    """Run ``target(*args)`` under a REAL, killable wall-clock timeout.

    Unlike a joined daemon thread (which Python cannot kill — a hung task would
    merely be abandoned while it keeps running), the target runs in a separate
    process. On overrun the child is sent SIGTERM (``terminate()``), then
    SIGKILL (``kill()``) if it does not die within ``_KILL_GRACE`` seconds, and
    ``TimeoutError`` is raised. ``timeout`` <= 0 (or None) runs the target
    inline and unbounded — parity with how the reaper reads ARIA_TASK_TIMEOUT.

    `target` must be a module-level callable and `args` picklable so this works
    under fork, spawn and forkserver (the default start method varies by
    platform and Python version). A worker exception is marshalled back and
    re-raised here so its traceback never silently vanishes.
    """
    if not timeout or timeout <= 0:
        return target(*args)

    import multiprocessing as mp
    import queue as _queue

    q: mp.Queue = mp.Queue()
    proc = mp.Process(
        target=_process_entry, args=(q, target, args), daemon=True,
    )
    proc.start()
    try:
        try:
            outcome = q.get(timeout=timeout)
        except _queue.Empty:
            outcome = None

        if outcome is None:
            if proc.is_alive():
                proc.terminate()                 # SIGTERM
                proc.join(_KILL_GRACE)
                if proc.is_alive():
                    proc.kill()                  # SIGKILL — refused to die
                    proc.join()
                raise TimeoutError(
                    f"task exceeded ARIA_TASK_TIMEOUT ({timeout}s)"
                )
            # Child exited without producing a result (crash / OOM-kill / segfault).
            raise RuntimeError(
                f"task process exited without a result (exit code {proc.exitcode})"
            )
    finally:
        proc.join(_KILL_GRACE)
        if proc.is_alive():
            proc.kill()
            proc.join()
        q.close()
        q.cancel_join_thread()

    status, payload, tb = outcome
    if status == "error":
        if tb:
            log.debug("task worker traceback:\n%s", tb)
        raise payload
    return payload


def _run_agent_task(prompt: str, task_id: str = "") -> str:
    """Module-level worker executed in the child process: re-init config + Agent
    and run the prompt. It shares no in-memory state with the parent (under
    spawn/forkserver nothing is inherited), so it loads config itself.

    ARIA_TASK_ID marks "inside a scheduled task" for the tools (the schedule
    tool refuses to create recurring tasks there). It is restored afterwards
    because with ARIA_TASK_TIMEOUT<=0 this runs inline in the supervisor."""
    from aria import config
    config.load()
    prev = os.environ.get("ARIA_TASK_ID")
    os.environ["ARIA_TASK_ID"] = task_id or "unknown"
    try:
        from aria.agent import Agent
        from aria.tools import plan as _plan
        # Each task starts with a clean plan: the supervisor's plan is per task,
        # never a leftover from a previous (different) task.
        _plan.clear("supervisor")
        agent = Agent(window_key="supervisor", terminal=False)
        try:
            return agent.chat_collect(prompt)
        finally:
            agent.close()
    finally:
        if prev is None:
            os.environ.pop("ARIA_TASK_ID", None)
        else:
            os.environ["ARIA_TASK_ID"] = prev


def _execute(task) -> str:
    """Run the task prompt through the agent (bounded by ARIA_TASK_TIMEOUT)
    and optionally notify.

    The agent call runs in a separate process (see ``run_with_timeout``) so a
    hung or looping task is *actually terminated* on timeout rather than
    abandoned — the old daemon-thread approach could not kill the worker, so a
    stuck task kept running and its late result was discarded. The running/
    reaper (reap_running) remains the backstop for the case where the whole
    supervisor/child process dies (crash), which no in-process timeout can
    cover."""
    # Wrap the prompt so the agent knows notification is handled externally.
    # This prevents the agent from calling the notify tool itself, which would
    # cause double-notification and pollute the result with tool call noise.
    wrapped = (
        f"{task.prompt}\n\n"
        "(This is an automated task. Do NOT call the notify tool — "
        "your response will be delivered automatically when you are done. "
        "Do NOT call the schedule tool to create, re-create or reschedule this "
        "task — if it recurs, the supervisor requeues it automatically.)"
    )

    from aria.task import task_timeout
    timeout = task_timeout()
    result = run_with_timeout(_run_agent_task, (wrapped, task.task_id), timeout)

    if task.notify and result:
        try:
            from aria.telegram_notify import send
            send(result)
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
        from aria.task import list_pending, claim, complete, fail, reap_running, dedupe_pending
        config.load()
        for note in reap_running() + dedupe_pending():
            print(f"! {note}")
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
