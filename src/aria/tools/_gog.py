"""
aria/tools/_gog.py — Shared runner for the gog-backed tools (gmail, calendar,
drive). Underscore prefix: a helper, not auto-loaded as a tool.

Each tool keeps its own message wording; only the mechanics are shared.
"""

from __future__ import annotations

import shlex
import subprocess

from aria.tools._env import build_env, gog_keyring_hint


def run(cmd: str, *, tag: str, no_account: str, not_found: str) -> str:
    """Run a gog command line and return its stripped stdout or an error string.

    `cmd` is a shell-quoted string (split with shlex, never run via a shell);
    it is echoed verbatim in exit/timeout errors. `tag` is the error prefix
    (e.g. "gmail" → "[gmail error] ..."). `no_account` / `not_found` are the
    full messages for a missing GOG_ACCOUNT / missing binary.
    """
    env = build_env()
    # Ensure GOG_ACCOUNT is set — gog requires it
    if "GOG_ACCOUNT" not in env:
        return no_account
    try:
        result = subprocess.run(
            shlex.split(cmd),
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        out = result.stdout.strip()
        err = result.stderr.strip()
        if result.returncode != 0:
            details = err or out or "no output"
            return (f"[{tag} error] exit={result.returncode}\ncmd: {cmd}\n{details}"
                    + gog_keyring_hint(details))
        return out or "(no output)"
    except FileNotFoundError:
        return not_found
    except subprocess.TimeoutExpired:
        return f"[{tag} error] command timed out: {cmd}"
    except Exception as exc:
        return f"[{tag} error] {exc}"
