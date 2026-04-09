"""
aria/install.py — Service installation wizard.

Interactively selects features, collects configuration, writes ~/.aria/.env,
creates systemd user service files, enables lingering, starts all services,
and verifies them.

Usage:
  aria-install              # full interactive wizard
  aria-install --dry-run    # show what would be done without changes
  aria-install --uninstall  # stop and remove all services
  aria-install --services   # skip env config, only (re)install services
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


# ── Colours ───────────────────────────────────────────────────────────────────

def _green(s: str)  -> str: return f"\033[32m{s}\033[0m"
def _yellow(s: str) -> str: return f"\033[33m{s}\033[0m"
def _red(s: str)    -> str: return f"\033[31m{s}\033[0m"
def _bold(s: str)   -> str: return f"\033[1m{s}\033[0m"
def _dim(s: str)    -> str: return f"\033[2m{s}\033[0m"

def ok(msg: str)      -> None: print(f"  {_green('✓')} {msg}")
def warn(msg: str)    -> None: print(f"  {_yellow('⚠')}  {msg}")
def err(msg: str)     -> None: print(f"  {_red('✗')} {msg}")
def info(msg: str)    -> None: print(f"    {_dim(msg)}")
def section(t: str)   -> None: print(); print(_bold(f"── {t} "))


# ── Prompt helpers ────────────────────────────────────────────────────────────

def _ask(prompt: str, default: str = "", secret: bool = False,
         required: bool = False, hint: str = "") -> str:
    if hint:
        print(f"    {_dim(hint)}")
    display = "****" if (secret and default) else default
    suffix  = f" [{display}]" if display else ""
    while True:
        raw   = input(f"  {prompt}{suffix}: ").strip()
        value = raw or default
        if required and not value:
            warn("This field is required.")
            continue
        return value


def _ask_bool(prompt: str, default: bool = True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    raw = input(f"  {prompt} {suffix}: ").strip().lower()
    return default if not raw else raw in ("y", "yes")


def _load_existing_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        env[key.strip()] = val.strip().strip('"').strip("'")
    return env


def _write_env(path: Path, values: dict[str, str]) -> None:
    """Write values to .env, preserving template structure and comments."""
    from aria.setup import _ENV_TEMPLATE

    template_lines = _ENV_TEMPLATE.splitlines()
    template_keys: set[str] = set()
    output: list[str] = []

    for line in template_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            output.append(line)
            continue
        key = stripped.split("=")[0].strip()
        template_keys.add(key)
        val = values.get(key, "").strip()
        output.append(f"{key}={val}" if val else f"# {key}=")

    for key, val in values.items():
        if key not in template_keys and val.strip():
            output.append(f"{key}={val}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(output) + "\n", encoding="utf-8")


# ── Feature selection ─────────────────────────────────────────────────────────

FEATURES = {
    "telegram":   "Telegram bot  (aria-telegram + aria --notify)",
    "whatsapp":   "WhatsApp bridge  (aria-whatsapp, needs Node.js)",
    "supervisor": "Autonomous supervisor  (task queue + memory reflection)",
    "gmail":      "Gmail & Calendar  (requires gogcli)",
}


def _select_features(existing: dict[str, str]) -> set[str]:
    """Ask which features to enable. Defaults reflect what's already configured."""
    print()
    print(_bold("Which features do you want to enable?"))
    info("Press Enter to keep the current selection. Space/Enter to toggle.")
    print()

    defaults = {
        "telegram":   bool(existing.get("TELEGRAM_TOKEN")),
        "whatsapp":   bool(existing.get("WHATSAPP_ALLOWED")),
        "supervisor": True,   # always on by default
        "gmail":      bool(existing.get("GOG_ACCOUNT")),
    }

    selected: set[str] = set()
    for key, label in FEATURES.items():
        default = defaults[key]
        if _ask_bool(f"  {label}?", default=default):
            selected.add(key)

    return selected


# ── Env config wizard ─────────────────────────────────────────────────────────

def configure_env(dry_run: bool = False) -> tuple[dict[str, str], set[str]]:
    """
    Interactively collect configuration for selected features.
    Returns (values dict, selected features set).
    """
    env_path = Path.home() / ".aria" / ".env"
    existing = _load_existing_env(env_path)

    def e(key: str) -> str:
        return existing.get(key, "")

    print()
    print(_bold("╭──────────────────────────────────────────────╮"))
    print(_bold("│         Aria Configuration Wizard             │"))
    print(_bold("╰──────────────────────────────────────────────╯"))

    if env_path.exists():
        print()
        print(f"  Existing config found at {_dim(str(env_path))}")
        print(f"  Press {_bold('Enter')} to keep existing values.")

    # ── Feature selection ─────────────────────────────────────────────────────
    features = _select_features(existing)

    values: dict[str, str] = {}

    # ── LLM (always required) ─────────────────────────────────────────────────
    section("LLM (required)")
    info("Any OpenAI-compatible endpoint. Examples:")
    info("  Anthropic: https://api.anthropic.com/v1")
    info("  OpenAI:    https://api.openai.com/v1")
    info("  Ollama:    http://localhost:11434/v1")

    values["LLM_BASE_URL"] = _ask("LLM_BASE_URL", e("LLM_BASE_URL") or "http://localhost:11434/v1", required=True)
    values["LLM_API_KEY"]  = _ask("LLM_API_KEY",  e("LLM_API_KEY")  or "ollama", secret=True,
                                   hint="Use any string for local models (Ollama, LM Studio)")
    values["LLM_MODEL"]    = _ask("LLM_MODEL",    e("LLM_MODEL")    or "llama3.2", required=True,
                                   hint="Model must be available at your endpoint")
    values["AGENT_NAME"]   = _ask("AGENT_NAME",   e("AGENT_NAME")   or "Aria",
                                   hint="Display name shown in terminal and messages")

    # ── Telegram ──────────────────────────────────────────────────────────────
    if "telegram" in features:
        section("Telegram")
        info("Get token from @BotFather — get your chat ID from @userinfobot")
        values["TELEGRAM_TOKEN"]   = _ask("TELEGRAM_TOKEN",   e("TELEGRAM_TOKEN"),   secret=True, required=True)
        values["TELEGRAM_ALLOWED"] = _ask("TELEGRAM_ALLOWED", e("TELEGRAM_ALLOWED"), required=True,
                                           hint="Comma-separated chat IDs allowed to use the bot")
    else:
        values["TELEGRAM_TOKEN"]   = e("TELEGRAM_TOKEN")
        values["TELEGRAM_ALLOWED"] = e("TELEGRAM_ALLOWED")

    # ── WhatsApp ──────────────────────────────────────────────────────────────
    if "whatsapp" in features:
        section("WhatsApp")
        info("Needs Node.js and ~/.aria/whatsapp/bridge.js — see README")
        values["ARIA_WA_PORT"]     = _ask("ARIA_WA_PORT",     e("ARIA_WA_PORT")     or "7532",
                                           hint="Port for Python↔Node.js bridge")
        values["ARIA_WA_SECRET"]   = _ask("ARIA_WA_SECRET",   e("ARIA_WA_SECRET"),  secret=True,
                                           hint="Shared secret between Python and Node.js bridges")
        values["WHATSAPP_ALLOWED"] = _ask("WHATSAPP_ALLOWED", e("WHATSAPP_ALLOWED"),
                                           hint="Your number in international format, no + (e.g. 34612345678)")
    else:
        for k in ("ARIA_WA_PORT", "ARIA_WA_SECRET", "WHATSAPP_ALLOWED"):
            values[k] = e(k)

    # ── Gmail / Calendar ──────────────────────────────────────────────────────
    if "gmail" in features:
        section("Gmail & Calendar")
        info("Setup: gog auth credentials ~/client_secret.json && gog auth add you@gmail.com")
        values["GMAIL_CLI"]   = _ask("GMAIL_CLI",   e("GMAIL_CLI")   or "gog", hint="CLI binary name")
        values["GOG_ACCOUNT"] = _ask("GOG_ACCOUNT", e("GOG_ACCOUNT"), required=True, hint="Your Gmail address")
    else:
        for k in ("GMAIL_CLI", "GOG_ACCOUNT"):
            values[k] = e(k)

    # ── Supervisor ────────────────────────────────────────────────────────────
    if "supervisor" in features:
        section("Supervisor & reflection (optional — press Enter for defaults)")
        values["ARIA_SUPERVISOR_INTERVAL"] = _ask("ARIA_SUPERVISOR_INTERVAL", e("ARIA_SUPERVISOR_INTERVAL") or "30",
                                                   hint="Seconds between task queue polls")
        values["ARIA_REFLECT_EVERY"]       = _ask("ARIA_REFLECT_EVERY",       e("ARIA_REFLECT_EVERY")       or "86400",
                                                   hint="Seconds between reflection runs (0 = disabled, 86400 = 24h)")
        values["ARIA_REFLECT_NOTIFY"]      = _ask("ARIA_REFLECT_NOTIFY",      e("ARIA_REFLECT_NOTIFY")      or "true",
                                                   hint="Send Telegram notification after reflection (true/false)")
    else:
        for k in ("ARIA_SUPERVISOR_INTERVAL", "ARIA_REFLECT_EVERY", "ARIA_REFLECT_NOTIFY"):
            values[k] = e(k)

    # ── Agent behaviour ───────────────────────────────────────────────────────
    section("Agent behaviour (optional — press Enter for defaults)")
    values["ARIA_MAX_LOOPS"]            = _ask("ARIA_MAX_LOOPS",            e("ARIA_MAX_LOOPS")            or "20",  hint="Max tool-call loops per turn")
    values["ARIA_MAX_HISTORY"]          = _ask("ARIA_MAX_HISTORY",          e("ARIA_MAX_HISTORY")          or "60",  hint="Conversation turns kept in context")
    values["ARIA_CHANNEL_IDLE_MINUTES"] = _ask("ARIA_CHANNEL_IDLE_MINUTES", e("ARIA_CHANNEL_IDLE_MINUTES") or "60",  hint="Idle minutes before channel session is summarised")
    values["ARIA_REFLECT_BATCH"]        = _ask("ARIA_REFLECT_BATCH",        e("ARIA_REFLECT_BATCH")        or "10",  hint="Sessions per reflection batch")
    values["ARIA_REFLECT_MAX_LINES"]    = _ask("ARIA_REFLECT_MAX_LINES",    e("ARIA_REFLECT_MAX_LINES")    or "40",  hint="Max bullet points in patterns.md")

    # ── Write ─────────────────────────────────────────────────────────────────
    print()
    if dry_run:
        info(f"[dry-run] would write {env_path}")
    else:
        _write_env(env_path, values)
        ok(f"Config written: {env_path}")

    return values, features


# ── Detection helpers ─────────────────────────────────────────────────────────

def _aria_bin(name: str) -> str | None:
    for candidate in [
        shutil.which(name),
        str(Path.home() / ".local" / "bin" / name),
        str(Path(sys.executable).parent / name),
    ]:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def _node_bin() -> str | None:
    for cmd in ("node", "nodejs"):
        p = shutil.which(cmd)
        if p:
            return p
    return None


def _systemd_available() -> bool:
    r = subprocess.run(["systemctl", "--user", "status"], capture_output=True)
    return r.returncode in (0, 1, 3)


def _linger_enabled() -> bool:
    r = subprocess.run(
        ["loginctl", "show-user", os.environ.get("USER", ""), "--property=Linger"],
        capture_output=True, text=True,
    )
    return "Linger=yes" in r.stdout


# ── Service templates ─────────────────────────────────────────────────────────

def _service(description: str, exec_start: str, env_file: str,
             after: str = "network-online.target", wants: str = "network-online.target",
             requires: str = "") -> str:
    req = f"Requires={requires}\n" if requires else ""
    # PassEnvironment forwards the user's keychain/keyring session so tools
    # like gog can access stored OAuth tokens without extra config.
    return (
        f"[Unit]\nDescription={description}\nAfter={after}\nWants={wants}\n{req}\n"
        f"[Service]\nExecStart={exec_start}\nRestart=on-failure\nRestartSec=10\n"
        f"EnvironmentFile={env_file}\n"
        "PassEnvironment=DBUS_SESSION_BUS_ADDRESS GNOME_KEYRING_CONTROL SSH_AUTH_SOCK\n"
        f"\n[Install]\nWantedBy=default.target\n"
    )


# ── Service installation ──────────────────────────────────────────────────────

def install_services(features: set[str] | None = None, dry_run: bool = False) -> None:
    """
    Install systemd services for the selected features.
    If features is None, infer from the existing .env file.
    """
    section("Checking environment")

    if not _systemd_available():
        err("systemd not available.")
        info("Use nohup instead — see README.")
        sys.exit(1)
    ok("systemd available")

    env_file = Path.home() / ".aria" / ".env"
    if not env_file.exists():
        err(f"Config not found: {env_file}")
        info("Run `aria-install` to create it.")
        sys.exit(1)
    ok(f"Config: {env_file}")

    # Infer features from env if not provided (--services flag path)
    if features is None:
        existing = _load_existing_env(env_file)
        features = set()
        if existing.get("TELEGRAM_TOKEN"):
            features.add("telegram")
        if existing.get("WHATSAPP_ALLOWED"):
            features.add("whatsapp")
        if existing.get("ARIA_SUPERVISOR_INTERVAL") or True:  # supervisor default-on
            features.add("supervisor")

    section("Detecting binaries")

    # Map feature → (service name, description, required binary)
    candidates = [
        ("aria-telegram",   "Aria Telegram Bot",           "telegram"   in features),
        ("aria-supervisor", "Aria Task Supervisor",         "supervisor" in features),
        ("aria-whatsapp",   "Aria WhatsApp Python Bridge",  "whatsapp"   in features),
    ]

    services: dict[str, dict] = {}
    for name, desc, wanted in candidates:
        if not wanted:
            info(f"• {name}: skipped (not selected)")
            continue
        bin_path = _aria_bin(name)
        if bin_path:
            ok(f"{name}: {bin_path}")
            services[name] = {"description": desc, "exec": bin_path}
        else:
            warn(f"{name}: binary not found — skipping")
            info("Run: pip install -e .")

    # WhatsApp Node.js bridge
    if "aria-whatsapp" in services:
        node      = _node_bin()
        wa_bridge = Path.home() / ".aria" / "whatsapp" / "bridge.js"
        if node and wa_bridge.exists():
            ok(f"node: {node}")
            services["aria-whatsapp-node"] = {
                "description": "Aria WhatsApp Node.js Bridge",
                "exec":        f"{node} {wa_bridge}",
                "requires":    "aria-whatsapp.service",
            }
        else:
            if not node:
                warn("node not found — aria-whatsapp-node skipped")
            if not wa_bridge.exists():
                warn(f"bridge.js not found: {wa_bridge}")
                info("See README — WhatsApp section")

    if not services:
        if not features or features == set():
            ok("CLI-only mode — no background services to install.")
        else:
            err("No services to install. Check binaries and .env.")
        return

    section(f"Installing {len(services)} service(s)")
    for name in services:
        info(f"• {name}")

    systemd_dir = Path.home() / ".config" / "systemd" / "user"
    if not dry_run:
        systemd_dir.mkdir(parents=True, exist_ok=True)

    for name, cfg in services.items():
        requires = cfg.get("requires", "")
        content  = _service(
            description = cfg["description"],
            exec_start  = cfg["exec"],
            env_file    = str(env_file),
            after       = "aria-whatsapp.service" if requires else "network-online.target",
            wants       = "" if requires else "network-online.target",
            requires    = requires,
        )
        path = systemd_dir / f"{name}.service"
        if dry_run:
            info(f"[dry-run] would write {path}")
        else:
            path.write_text(content, encoding="utf-8")
            ok(f"Written: {path.name}")

    section("Enabling user lingering")
    if _linger_enabled():
        ok("Already enabled")
    elif dry_run:
        info("[dry-run] loginctl enable-linger")
    else:
        r = subprocess.run(["loginctl", "enable-linger"], capture_output=True)
        ok("Enabled") if r.returncode == 0 else warn("Try: sudo loginctl enable-linger $USER")

    section("Starting services")
    if dry_run:
        info("[dry-run] would start services")
    else:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        ok("Daemon reloaded")
        for name in services:
            r = subprocess.run(
                ["systemctl", "--user", "enable", "--now", name],
                capture_output=True, text=True,
            )
            if r.returncode == 0:
                ok(f"Started: {name}")
            else:
                err(f"Failed: {name}")
                info(r.stderr.strip())

    if not dry_run:
        section("Verifying")
        import time; time.sleep(2)
        all_ok = True
        for name in services:
            r = subprocess.run(["systemctl", "--user", "is-active", name],
                               capture_output=True, text=True)
            status = r.stdout.strip()
            if status == "active":
                ok(f"{name}: active")
            else:
                err(f"{name}: {status}")
                info(f"Logs: journalctl --user -u {name} -n 20")
                all_ok = False

        print()
        msg = "All services running. ✦" if all_ok else "Some services failed — check logs above."
        print((_green if all_ok else _yellow)(_bold(f"  {msg}")))

    print()
    print(_bold("  Useful commands:"))
    for name in services:
        info(f"journalctl --user -fu {name}")
    print()
    info(f"After code update: systemctl --user restart {' '.join(services)}")
    print()


# ── Uninstall ─────────────────────────────────────────────────────────────────

def uninstall() -> None:
    section("Uninstalling Aria services")
    names = ["aria-telegram", "aria-supervisor", "aria-whatsapp", "aria-whatsapp-node"]
    for name in names:
        subprocess.run(["systemctl", "--user", "disable", "--now", name], capture_output=True)
        path = Path.home() / ".config" / "systemd" / "user" / f"{name}.service"
        if path.exists():
            path.unlink()
            ok(f"Removed: {path.name}")
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    ok("Done.")
    print()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="aria-install",
        description="Interactive setup wizard — configure and install Aria services.",
    )
    parser.add_argument("--dry-run",   action="store_true", help="Show what would be done without changes")
    parser.add_argument("--uninstall", action="store_true", help="Stop and remove all services")
    parser.add_argument("--services",  action="store_true", help="Skip env config, only (re)install services")
    args = parser.parse_args()

    if args.uninstall:
        uninstall()
        return

    if args.services:
        # Infer features from existing env
        install_services(features=None, dry_run=args.dry_run)
    else:
        # Full wizard — configure env then install
        _, features = configure_env(dry_run=args.dry_run)
        install_services(features=features, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
