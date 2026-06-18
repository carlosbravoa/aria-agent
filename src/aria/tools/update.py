"""
aria/tools/update.py — Self-update the agent from its git source directory.

Designed to be safe to trigger remotely (e.g. from Telegram while travelling,
with no SSH to the box): it validates the new code BEFORE restarting and rolls
back automatically if the new code won't import, so a bad push can never brick
the agent — the worst case is "update declined, old version still running".

Flow: snapshot SHA → fetch + hard-reset to origin/<branch> → pip install →
import-smoke the new code → if it fails, reset + reinstall the old SHA and do
NOT restart → otherwise restart the active systemd --user services.

Required in ~/.aria/.env:
  ARIA_SOURCE_DIR=~/aria-agent    # path to the local git clone
Optional:
  ARIA_UPDATE_BRANCH=main         # branch to deploy (default: main)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import sysconfig
import time
from pathlib import Path

# How long after a restart a new version must stay up to be considered healthy.
# A crash-loop within this window triggers auto-rollback (see rollback_main).
_CONFIRM_WINDOW = int(os.environ.get("ARIA_UPDATE_CONFIRM_SEC", "600"))


def _state_path() -> Path:
    return Path.home() / ".aria" / "update_state.json"


def _append_log(msg: str) -> None:
    try:
        p = Path.home() / ".aria" / "update.log"
        with p.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


def _arm_watchdog(prev_sha: str, src: Path, branch: str) -> None:
    """Record a rollback target before restarting. If the new version crash-loops
    within _CONFIRM_WINDOW, aria-rollback.service (OnFailure) reverts to prev_sha."""
    try:
        sp = _state_path()
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps({
            "pending":  True,
            "prev_sha": prev_sha,
            "src":      str(src),
            "branch":   branch,
            "deadline": time.time() + _CONFIRM_WINDOW,
        }), encoding="utf-8")
    except Exception:
        pass

DEFINITION = {
    "name": "update",
    "description": (
        "Update Aria to the latest version: pull the configured branch, reinstall, "
        "VALIDATE the new code, and restart services — rolling back automatically if "
        "the new code fails to import (so a bad update can't brick the agent). "
        "Reports what changed. Requires ARIA_SOURCE_DIR in ~/.aria/.env."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "restart_services": {
                "type": "boolean",
                "description": "Restart systemd --user services after a validated update. Default: true.",
                "default": True,
            },
            "dry_run": {
                "type": "boolean",
                "description": "Show what would happen without making changes.",
                "default": False,
            },
        },
    },
}

_SERVICES = [
    "aria-telegram",
    "aria-supervisor",
    "aria-whatsapp",
    "aria-whatsapp-node",
]


def _run(argv: list[str], cwd: Path | None = None, timeout: int = 120) -> tuple[int, str, str]:
    """Run an argv list (shell=False). Returns (rc, stdout, stderr)."""
    try:
        r = subprocess.run(
            argv, capture_output=True, text=True,
            cwd=str(cwd) if cwd else None, timeout=timeout,
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except FileNotFoundError as exc:
        return 127, "", str(exc)


def _git(args: list[str], cwd: Path) -> tuple[int, str, str]:
    return _run(["git", *args], cwd=cwd)


def _pip_install(src: Path) -> tuple[int, str, str]:
    """Reinstall from source, handling PEP 668 externally-managed environments."""
    argv = [sys.executable, "-m", "pip", "install", str(src)]
    if (Path(sysconfig.get_path("stdlib")) / "EXTERNALLY-MANAGED").exists():
        argv.append("--break-system-packages")
    return _run(argv, cwd=src, timeout=600)


def _validate_imports() -> tuple[bool, str]:
    """
    Import-smoke the freshly installed package in a fresh subprocess. Catches the
    NameError/missing-symbol-at-import class of breakage (e.g. a deleted def)
    that would otherwise crash-loop every service after restart.
    """
    code = (
        "import importlib, pkgutil, aria\n"
        "for m in pkgutil.walk_packages(aria.__path__, 'aria.'):\n"
        "    if m.name == 'aria.main':\n"        # importing main runs first-run setup
        "        continue\n"
        "    importlib.import_module(m.name)\n"
        "assert hasattr(aria.reflect, 'run'), 'reflect.run missing'\n"
        "assert hasattr(aria.agent.Agent, '_call_model'), 'native loop missing'\n"
        "assert hasattr(aria.tools.remember, 'execute'), 'remember tool missing'\n"
        "print('SMOKE_OK')\n"
    )
    env = dict(os.environ)
    env.setdefault("ARIA_ENV", "/nonexistent")           # skip first-run wizard
    env.setdefault("LLM_BASE_URL", "x")
    env.setdefault("LLM_API_KEY", "x")
    env.setdefault("LLM_MODEL", "x")
    try:
        r = subprocess.run([sys.executable, "-c", code],
                           capture_output=True, text=True, timeout=120, env=env)
    except subprocess.TimeoutExpired:
        return False, "import-smoke timed out"
    ok = r.returncode == 0 and "SMOKE_OK" in r.stdout
    return ok, (r.stderr or r.stdout).strip()[-800:]


def _active_services() -> list[str]:
    active = []
    for name in _SERVICES:
        _, out, _ = _run(["systemctl", "--user", "is-active", name])
        if out == "active":
            active.append(name)
    return active


def execute(args: dict) -> str:
    restart = args.get("restart_services", True)
    dry_run = args.get("dry_run", False)
    branch  = os.environ.get("ARIA_UPDATE_BRANCH", "main").strip() or "main"
    lines: list[str] = []

    source_dir = os.environ.get("ARIA_SOURCE_DIR", "").strip()
    if not source_dir:
        return ("[update] ARIA_SOURCE_DIR not set. Add "
                "ARIA_SOURCE_DIR=~/aria-agent to ~/.aria/.env")

    src = Path(source_dir).expanduser().resolve()
    if not src.exists():
        return (f"[update] Source directory not found: {src} "
                f"(check ARIA_SOURCE_DIR in ~/.aria/.env for typos).")
    if not (src / ".git").exists():
        return f"[update] Not a git repository: {src}"

    lines.append(f"📂 Source: {src}  (branch: {branch})")

    # ── 1. Snapshot current commit (rollback target) ─────────────────────────
    rc, before_sha, err = _git(["rev-parse", "HEAD"], src)
    if rc != 0:
        return f"[update] cannot read current commit: {err}"
    before_short = before_sha[:9]

    # ── 2. Fetch ─────────────────────────────────────────────────────────────
    rc, _, err = _git(["fetch", "origin", branch], src)
    if rc != 0:
        return f"[update] git fetch failed:\n{err}"

    rc, target_sha, err = _git(["rev-parse", f"origin/{branch}"], src)
    if rc != 0:
        return f"[update] unknown branch origin/{branch}: {err}"

    if target_sha == before_sha:
        return "\n".join(lines + ["✅ Already up to date — nothing to do."])

    rc, log_out, _ = _git(["log", "--oneline", f"{before_sha}..{target_sha}"], src)
    lines.append(f"📝 Incoming ({before_short} → {target_sha[:9]}):")
    lines += [f"  {l}" for l in log_out.splitlines()] or ["  (no log)"]

    if dry_run:
        lines.append("\n🔍 Dry run — would reset, reinstall, validate, then restart. No changes made.")
        return "\n".join(lines)

    # ── 3. Hard-reset to the target (deploy clone; avoids merge conflicts) ────
    rc, _, err = _git(["reset", "--hard", target_sha], src)
    if rc != 0:
        return f"[update] git reset failed:\n{err}"

    # ── 4. Reinstall ─────────────────────────────────────────────────────────
    lines.append("\n🔧 Installing…")
    rc, _, perr = _pip_install(src)
    if rc != 0:
        _git(["reset", "--hard", before_sha], src)
        _pip_install(src)
        return "\n".join(lines + [
            f"[update] pip install failed — rolled back to {before_short}, NOT restarting.\n{perr[-600:]}"
        ])

    # ── 5. Validate the new code BEFORE touching the running services ────────
    lines.append("🧪 Validating new code…")
    ok, detail = _validate_imports()
    if not ok:
        _git(["reset", "--hard", before_sha], src)
        _pip_install(src)
        return "\n".join(lines + [
            f"❌ New code failed import-validation — rolled back to {before_short}, "
            f"services NOT restarted (still running the old, working version).\n{detail}"
        ])
    lines.append("  ✅ Imports clean.")

    # ── 6. Restart only on green ─────────────────────────────────────────────
    if restart:
        active = _active_services()
        if active:
            # Arm the post-restart watchdog: a crash-loop within the confirm
            # window auto-rolls-back to the version running right now.
            _arm_watchdog(before_sha, src, branch)
            _append_log(f"updated {before_short} -> {target_sha[:9]}, restarting {','.join(active)}")
            # delay so this report reaches the user before the process dies
            subprocess.Popen(
                ["bash", "-c", f"sleep 3 && systemctl --user restart {' '.join(active)}"]
            )
            lines.append(f"\n🔄 Validated — restarting in ~3s: {', '.join(active)}")
            lines.append(f"   (watchdog armed: auto-rollback to {before_short} if it crash-loops "
                         f"within {_CONFIRM_WINDOW // 60} min)")
        else:
            lines.append("\nℹ️  No active systemd --user services found — restart manually.")

    lines.append(f"\n✅ Updated {before_short} → {target_sha[:9]}.")
    return "\n".join(lines)


def rollback_main() -> None:
    """
    Entry point for aria-rollback.service (triggered via OnFailure= when a
    service crash-loops). If an update is pending and still inside its confirm
    window, revert to the recorded last-known-good commit and restart; otherwise
    do nothing. Race-safe via an atomic rename claim so concurrent OnFailure
    triggers from multiple services roll back only once.
    """
    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    log = logging.getLogger("aria-rollback")

    from aria import config
    config.load()

    sp = _state_path()
    claimed = sp.with_suffix(".claimed")
    try:
        sp.rename(claimed)          # atomic — only one invocation wins the claim
    except FileNotFoundError:
        log.info("aria-rollback: no pending update — nothing to do.")
        return

    try:
        st = json.loads(claimed.read_text(encoding="utf-8"))
    except Exception:
        claimed.unlink(missing_ok=True)
        log.info("aria-rollback: unreadable state — discarded.")
        return

    try:
        if not st.get("pending"):
            return
        if time.time() >= st.get("deadline", 0):
            log.info("aria-rollback: update already survived the confirm window — no rollback.")
            _append_log("failure after confirm window — left as-is (no rollback)")
            return

        src  = Path(st["src"])
        prev = st["prev_sha"]
        log.info(f"aria-rollback: service crash-looping — reverting to {prev[:9]} at {src}")
        _git(["reset", "--hard", prev], src)
        _pip_install(src)
        _append_log(f"ROLLED BACK to {prev[:9]} after post-update crash-loop")
        for name in _SERVICES:
            _run(["systemctl", "--user", "restart", name])
        log.info(f"aria-rollback: reverted to {prev[:9]} and restarted services.")
    finally:
        claimed.unlink(missing_ok=True)
