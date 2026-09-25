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
from contextlib import contextmanager
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


def _backup_env(path: Path) -> Path | None:
    """Back up an existing .env before it's overwritten, preserving its 0600
    perms. Returns the backup path, or None if there was nothing to back up.
    Backups are timestamped and never pruned — a botched reconfigure can always
    be recovered by copying one back."""
    if not path.exists():
        return None
    from datetime import datetime
    stamp  = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    # Avoid clobbering a same-second backup on a rapid re-run.
    n = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}.bak-{stamp}.{n}")
        n += 1
    shutil.copy2(path, backup)
    backup.chmod(0o600)                 # an old .env may have been 0644 — backups hold keys too
    return backup


def _existing_env_lines(path: Path) -> dict[str, str]:
    """Map KEY → the raw, verbatim `KEY=value` line of every active setting in
    an existing .env (comments/blank lines skipped, `export ` prefix allowed).
    Later duplicates win, matching python-dotenv."""
    raw: dict[str, str] = {}
    if not path.exists():
        return raw
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if key:
            raw[key] = stripped
    return raw


def _write_env(path: Path, values: dict[str, str]) -> Path | None:
    """Write values to .env, preserving template structure and comments. Backs
    up any existing .env first; returns the backup path (or None if none).

    Only keys in `values` are managed by the wizard. Every other setting already
    in the file (JIRA_*, IMAP_*, LLM_PROFILE*, ARIA_FILE_*_DIRS, custom keys…) is
    kept verbatim — in its template slot if the template lists it, otherwise in
    an "Other settings (preserved)" section — so re-running aria-install never
    silently drops or comments out configuration it didn't ask about."""
    from aria.setup import _ENV_TEMPLATE, write_private

    existing = _existing_env_lines(path)
    template_lines = _ENV_TEMPLATE.splitlines()
    template_keys: set[str] = set()
    output: list[str] = []

    import re
    commented_re = re.compile(r"#\s*([A-Z][A-Z0-9_]*)=")
    for line in template_lines:
        stripped = line.strip()
        m = commented_re.match(stripped)
        if m and m.group(1) in existing and m.group(1) not in values \
                and m.group(1) not in template_keys:
            # Commented example of a key the user has set: show the real value
            # in its documented slot instead of the placeholder.
            template_keys.add(m.group(1))
            output.append(existing[m.group(1)])
            continue
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            output.append(line)
            continue
        key = stripped.split("=")[0].strip()
        if key in template_keys and key not in values:
            output.append(f"# {key}=")      # already emitted from a commented slot
            continue
        template_keys.add(key)
        if key in values:
            val = values[key].strip()
            output.append(f"{key}={val}" if val else f"# {key}=")
        elif key in existing:
            output.append(existing[key])
        else:
            output.append(f"# {key}=")

    for key, val in values.items():
        if key not in template_keys and val.strip():
            output.append(f"{key}={val}")

    preserved = [ln for k, ln in existing.items()
                 if k not in template_keys and k not in values]
    if preserved:
        output.append("")
        output.append("# ── Other settings (preserved) ──────────────────────────────────")
        output.extend(preserved)

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)        # ~/.aria holds secrets + memory
    except OSError:
        pass
    backup = _backup_env(path)
    write_private(path, "\n".join(output) + "\n")
    return backup


# ── Feature selection ─────────────────────────────────────────────────────────

# Non-channel features. Messaging channels (Telegram, WhatsApp, user plugins in
# ~/.aria/channels/) come from the channel plugin registry and are listed
# first in the menu — see _channel_plugins().
FEATURES = {
    "supervisor": "Autonomous supervisor  (task queue + memory reflection)",
    "gmail":      "Gmail & Calendar  (requires gogcli)",
}
_RESERVED = set(FEATURES) | {"browser"}


@contextmanager
def _env_overlay(existing: dict[str, str]):
    """Expose the .env being edited to code that reads os.environ (plugin
    is_configured(), channels.enabled(), ARIA_CHANNELS_DIR, deploy hooks) for
    the duration of the block. Like config.load(): the process environment
    wins over the file. Restored afterwards."""
    added = [k for k in existing if k not in os.environ]
    for k in added:
        os.environ[k] = existing[k]
    try:
        yield
    finally:
        for k in added:
            os.environ.pop(k, None)


def _channel_plugins() -> dict:
    """Discovered channel plugins by name (built-ins first, then user plugins),
    minus any whose name would collide with a non-channel feature."""
    from aria import channels
    plugins = {}
    for name, plugin in channels.discover(refresh=True).items():
        if name in _RESERVED:
            warn(f"Channel plugin '{name}' clashes with a built-in feature name — ignored")
            continue
        plugins[name] = plugin
    return plugins


def _explicit_channels(existing: dict[str, str]) -> list[str] | None:
    raw = os.environ.get("ARIA_CHANNELS", existing.get("ARIA_CHANNELS"))
    if raw is None:
        return None
    return [n.strip().lower() for n in raw.split(",") if n.strip()]


def _title(plugin) -> str:
    """Section/menu name of a channel (its `title`, else the capitalised name)."""
    return plugin.display_name


def _select_features(existing: dict[str, str], plugins: dict | None = None) -> set[str]:
    """Ask which features to enable. Defaults reflect what's already configured:
    a channel is preselected when ARIA_CHANNELS lists it (or, on a pre-plugin
    .env without ARIA_CHANNELS, when its settings are present)."""
    if plugins is None:
        plugins = _channel_plugins()
    print()
    print(_bold("Which features do you want to enable?"))
    info("Press Enter to keep the current selection. Space/Enter to toggle.")
    print()

    explicit = _explicit_channels(existing)
    menu: list[tuple[str, str, bool]] = []
    for name, plugin in plugins.items():
        if explicit is not None:
            default = name in explicit
        else:
            try:
                default = bool(plugin.is_configured())
            except Exception:
                default = False
        menu.append((name, plugin.description or name, default))
    menu.append(("supervisor", FEATURES["supervisor"], True))   # always on by default
    menu.append(("gmail", FEATURES["gmail"], bool(existing.get("GOG_ACCOUNT"))))

    selected: set[str] = set()
    for key, label, default in menu:
        if _ask_bool(f"  {label}?", default=default):
            selected.add(key)

    return selected


def _print_note(note) -> None:
    """Render one install() note: (level, text) with level ok/warn/info, or a
    plain string (shown as a hint)."""
    level, text = note if isinstance(note, tuple) else ("info", note)
    {"ok": ok, "warn": warn}.get(level, info)(text)


def _run_install_hook(plugin, dry_run: bool) -> list:
    try:
        return list(plugin.install(dry_run) or [])
    except Exception as exc:
        warn(f"{plugin.name}: install hook failed: {exc}")
        return []


def _configure_channel(plugin, e, dry_run: bool) -> dict[str, str]:
    """Prompt for a selected channel's settings, then run its install hook."""
    section(_title(plugin))
    for line in (plugin.setup_help or "").splitlines():
        info(line)
    out: dict[str, str] = {}
    for f in plugin.config_fields:
        out[f.key] = _ask(f.prompt or f.key, e(f.key) or f.default,
                          secret=f.secret, required=f.required, hint=f.help)
    if plugin.supports_attached:
        from aria.channels.base import mode_key
        attached = _ask_bool(
            f"  Run {_title(plugin)} only while `aria` is open (attached mode, no "
            f"background service)?", default=(plugin.mode != "service"))
        # "control" (remote control of the terminal session) is an attached
        # flavour — keep it if that's what was configured.
        out[mode_key(plugin.name)] = (
            (plugin.mode if plugin.mode != "service" else "attached")
            if attached else "service")
    for note in _run_install_hook(plugin, dry_run):
        _print_note(note)
    return out


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
    with _env_overlay(existing):
        plugins  = _channel_plugins()
        features = _select_features(existing, plugins)

    values: dict[str, str] = {}

    # ── LLM (always required) ─────────────────────────────────────────────────
    section("LLM (required)")
    info("Any OpenAI-compatible endpoint. Examples:")
    info("  Anthropic: https://api.anthropic.com/v1")
    info("  OpenAI:    https://api.openai.com/v1")
    info("  Ollama:    http://localhost:11434/v1")
    info("")
    info("Aria 2.0 uses native tool calling — your model/endpoint must support it.")
    info("Hosted models (Claude, GPT-4o) do; many local models do too if the")
    info("runtime exposes the tools API. If yours doesn't, use Aria 1.x instead.")

    values["LLM_BASE_URL"] = _ask("LLM_BASE_URL", e("LLM_BASE_URL") or "http://localhost:11434/v1", required=True)
    values["LLM_API_KEY"]  = _ask("LLM_API_KEY",  e("LLM_API_KEY")  or "ollama", secret=True,
                                   hint="Use any string for local models (Ollama, LM Studio)")
    values["LLM_MODEL"]    = _ask("LLM_MODEL",    e("LLM_MODEL")    or "llama3.2", required=True,
                                   hint="Model must be available at your endpoint")
    values["AGENT_NAME"]   = _ask("AGENT_NAME",   e("AGENT_NAME")   or "Aria",
                                   hint="Display name shown in terminal and messages")

    # ── Channels (plugins: Telegram, WhatsApp, ~/.aria/channels/*.py) ────────
    # Unselected channels keep their existing settings untouched.
    selected_channels: list[str] = []
    with _env_overlay(existing):
        for name, plugin in plugins.items():
            if name in features:
                selected_channels.append(name)
                values.update(_configure_channel(plugin, e, dry_run))
            else:
                for f in plugin.config_fields:
                    values[f.key] = e(f.key)
    values["ARIA_CHANNELS"] = ",".join(selected_channels) or "none"

    # ── Gmail / Calendar ──────────────────────────────────────────────────────
    if "gmail" in features:
        section("Gmail & Calendar")
        info("Before continuing, run these once in your terminal:")
        info("  gog auth credentials ~/Downloads/client_secret_....json")
        info("  gog auth keyring file")
        info("  gog auth add you@gmail.com --services gmail,calendar")
        info("  (pick a passphrase when prompted — enter it below)")
        print()
        values["GMAIL_CLI"]   = _ask("GMAIL_CLI",   e("GMAIL_CLI")   or "gog",
                                      hint="CLI binary name (usually 'gog')")
        values["GOG_ACCOUNT"] = _ask("GOG_ACCOUNT", e("GOG_ACCOUNT"), required=True,
                                      hint="Your Gmail address")
        values["GOG_KEYRING_BACKEND"]  = "file"
        values["GOG_KEYRING_PASSWORD"] = _ask(
            "GOG_KEYRING_PASSWORD", e("GOG_KEYRING_PASSWORD"), secret=True, required=True,
            hint="Passphrase you chose when running 'gog auth keyring file'",
        )
    else:
        for k in ("GMAIL_CLI", "GOG_ACCOUNT", "GOG_KEYRING_BACKEND", "GOG_KEYRING_PASSWORD"):
            values[k] = e(k)

    # ── Browser automation ────────────────────────────────────────────────────
    if "browser" in features:
        section("Browser automation")
        info("Requires: pip install websockets")
        info("Start browser with: chromium --remote-debugging-port=9222 --remote-allow-origins=http://localhost")
        values["CHROME_PROFILE_DIR"] = _ask(
            "CHROME_PROFILE_DIR", e("CHROME_PROFILE_DIR") or "~/.config/google-chrome",
            hint="Path to your Chrome profile directory"
        )
        values["CHROME_DEBUG_PORT"]  = _ask(
            "CHROME_DEBUG_PORT", e("CHROME_DEBUG_PORT") or "9222",
            hint="CDP remote debugging port (default 9222)"
        )
        values["ARIA_BROWSER_MAX_LOOPS"] = _ask(
            "ARIA_BROWSER_MAX_LOOPS", e("ARIA_BROWSER_MAX_LOOPS") or "50",
            hint="Max tool-call loops for browser tasks (default 50)"
        )
    else:
        for k in ("CHROME_PROFILE_DIR", "CHROME_DEBUG_PORT", "ARIA_BROWSER_MAX_LOOPS"):
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
    values["ARIA_CHANNEL_IDLE_MINUTES"] = _ask("ARIA_CHANNEL_IDLE_MINUTES", e("ARIA_CHANNEL_IDLE_MINUTES") or "60",  hint="Idle minutes before a channel session is trimmed + dropped")
    values["ARIA_REFLECT_BATCH"]        = _ask("ARIA_REFLECT_BATCH",        e("ARIA_REFLECT_BATCH")        or "10",  hint="Sessions per reflection batch")
    values["ARIA_REFLECT_MAX_LINES"]    = _ask("ARIA_REFLECT_MAX_LINES",    e("ARIA_REFLECT_MAX_LINES")    or "40",  hint="Max bullet points in patterns.md")

    section("Self-update (optional)")
    info("Used by the 'update' tool — lets the agent update itself from source.")
    values["ARIA_SOURCE_DIR"]    = _ask("ARIA_SOURCE_DIR",    e("ARIA_SOURCE_DIR"),
                                        hint="Path to the source directory, e.g. ~/aria-agent")
    values["ARIA_UPDATE_BRANCH"] = _ask("ARIA_UPDATE_BRANCH", e("ARIA_UPDATE_BRANCH") or "main",
                                        hint="Git branch to pull from")

    # ── Write ─────────────────────────────────────────────────────────────────
    print()
    if dry_run:
        info(f"[dry-run] would write {env_path}")
    else:
        backup = _write_env(env_path, values)
        if backup:
            ok(f"Backed up previous config: {backup}")
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
             after: str = "", wants: str = "", requires: str = "") -> str:
    # No network-online.target: a --user manager can't see the system target, so
    # After=/Wants= on it were silent no-ops. Network-not-ready-at-boot is
    # handled by the restart policy below instead.
    deps = "".join(f"{k}={v}\n" for k, v in
                   (("After", after), ("Wants", wants), ("Requires", requires)) if v)
    # PassEnvironment forwards the user's keychain/keyring session so tools
    # like gog can access stored OAuth tokens without extra config.
    # StartLimit* + OnFailure arm the self-update watchdog: if a service
    # crash-loops (5 starts in 5 min) it enters 'failed' and triggers
    # aria-rollback@<unit>.service, which reverts a bad update (only if an
    # update is pending) and then restarts the failed unit after a pause — so a
    # transient failure (network down at boot) is retried forever instead of
    # leaving the service permanently 'failed'.
    # Deliberately no RestartSteps/RestartMaxDelaySec backoff (systemd >= 254):
    # growing delays would spread 5 starts past the 300s window, so a bad update
    # would never trip the start limit and the rollback watchdog would never fire.
    # The pause between burst cycles comes from the recovery unit instead.
    return (
        f"[Unit]\nDescription={description}\n{deps}"
        "StartLimitIntervalSec=300\nStartLimitBurst=5\n"
        "OnFailure=aria-rollback@%n.service\n\n"
        f"[Service]\nExecStart={exec_start}\nRestart=on-failure\nRestartSec=10\n"
        f"EnvironmentFile={env_file}\n"
        "PassEnvironment=DBUS_SESSION_BUS_ADDRESS GNOME_KEYRING_CONTROL SSH_AUTH_SOCK\n"
        f"\n[Install]\nWantedBy=default.target\n"
    )


_ROLLBACK_UNIT = "aria-rollback@.service"
_RETRY_DELAY_SEC = 60


def _rollback_service(rollback_bin: str, env_file: str) -> str:
    """Template oneshot triggered via OnFailure=aria-rollback@%n.service.
    Not enabled — invoked on demand by systemd, not at boot. `%i` is the failed
    unit. Step 1 runs aria-rollback, which reverts ONLY when an update marker
    (~/.aria/update_state.json) is pending inside its confirm window and is a
    no-op otherwise ('-' prefix: its exit code never blocks step 2). Step 2
    waits, clears the start-limit and restarts the failed unit."""
    return (
        "[Unit]\nDescription=Aria auto-rollback / recovery after %i failed\n\n"
        f"[Service]\nType=oneshot\nExecStart=-{rollback_bin}\n"
        f"ExecStart=/bin/sh -c 'sleep {_RETRY_DELAY_SEC}; "
        "systemctl --user reset-failed %i; systemctl --user start %i'\n"
        "TimeoutStartSec=900\n"            # rollback may pip-install; default 90s is too short
        f"EnvironmentFile={env_file}\n"
        "PassEnvironment=DBUS_SESSION_BUS_ADDRESS GNOME_KEYRING_CONTROL SSH_AUTH_SOCK\n"
    )


# ── Service installation ──────────────────────────────────────────────────────

def _resolve_exe(exe: str) -> str | None:
    """A unit's executable: a bare "aria-…" console script via _aria_bin, an
    absolute path if it exists, else a PATH lookup."""
    if exe.startswith("aria-") and "/" not in exe:
        return _aria_bin(exe)
    if os.path.isabs(exe):
        return exe if Path(exe).exists() else None
    return shutil.which(exe)


def _collect_services(features: set[str] | None,
                      dry_run: bool) -> tuple[dict[str, dict], set[str]]:
    """Resolve the units to write: the supervisor plus every selected channel's
    services(). `features=None` (the --services path) infers the channels from
    the registry (ARIA_CHANNELS, else legacy keys). Returns (units, features).
    Caller holds _env_overlay."""
    from aria import channels

    plugins = _channel_plugins()
    if features is None:
        features = {"supervisor"}          # supervisor default-on
        try:
            features |= {p.name for p in channels.enabled() if p.name in plugins}
        except Exception as exc:
            warn(f"Could not read enabled channels: {exc}")

    section("Detecting binaries")
    services: dict[str, dict] = {}

    if "supervisor" in features:
        bin_path = _aria_bin("aria-supervisor")
        if bin_path:
            ok(f"aria-supervisor: {bin_path}")
            services["aria-supervisor"] = {"description": "Aria Task Supervisor",
                                           "exec": bin_path}
        else:
            warn("aria-supervisor: binary not found — skipping")
            info("Run: pip install -e .")
    else:
        info("• aria-supervisor: skipped (not selected)")

    for name, plugin in plugins.items():
        try:
            specs = list(plugin.services())
        except Exception as exc:
            warn(f"{name}: services() failed — skipping ({exc})")
            continue
        if name not in features:
            for spec in specs:
                if not spec.optional:
                    info(f"• {spec.unit}: skipped (not selected)")
            continue
        # Make sure helper files are present/current before wiring the units
        # (a services-only rerun skips the config step that also runs this).
        _run_install_hook(plugin, dry_run)
        if plugin.runs_attached:
            info(f"• {name}: {plugin.mode} mode — no background service "
                 f"(online while `aria` is open; /remote in the REPL)")
            _retire_units([s.unit for s in specs], dry_run)
            continue
        for spec in specs:
            _add_spec(services, spec)
    return services, features


def _retire_units(units: list[str], dry_run: bool) -> None:
    """A channel switched to attached mode must not keep a background unit
    polling the same account — stop, disable and remove it."""
    systemd_dir = Path.home() / ".config" / "systemd" / "user"
    for unit in units:
        path = systemd_dir / f"{unit}.service"
        if not path.exists():
            continue
        if dry_run:
            info(f"[dry-run] would disable and remove {path.name} (attached mode)")
            continue
        subprocess.run(["systemctl", "--user", "disable", "--now", unit],
                       capture_output=True)
        path.unlink(missing_ok=True)
        ok(f"Removed {path.name} — the channel now runs attached to `aria`")


def _add_spec(services: dict[str, dict], spec) -> None:
    unit = spec.unit
    if not spec.exec_start:
        warn(f"{unit}: no ExecStart — skipping")
        return
    if spec.requires and spec.requires.removesuffix(".service") not in services:
        (info if spec.optional else warn)(
            f"• {unit}: skipped ({spec.requires} is not being installed)")
        return
    exe, args = spec.exec_start[0], list(spec.exec_start[1:])
    path = _resolve_exe(exe)
    if not path:
        if spec.optional:
            warn(f"{Path(exe).name} not found — {unit} skipped")
        else:
            warn(f"{unit}: binary not found — skipping")
            if exe.startswith("aria-"):
                info("Run: pip install -e .")
        return
    missing = [a for a in args if os.path.isabs(a) and not Path(a).exists()]
    if missing:
        for a in missing:
            warn(f"{Path(a).name} not found: {a}")
        warn(f"{unit} skipped")
        return
    label = unit if exe.startswith("aria-") else Path(exe).name
    ok(f"{label}: {path}")
    cfg = {"description": spec.description, "exec": " ".join([path, *args])}
    if spec.requires:
        cfg["requires"] = spec.requires
    services[unit] = cfg


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

    existing = _load_existing_env(env_file)
    with _env_overlay(existing):
        services, features = _collect_services(features, dry_run)

    if not services:
        if not features:
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
            after       = requires,          # Requires= always implies ordering after it
            requires    = requires,
        )
        path = systemd_dir / f"{name}.service"
        if dry_run:
            info(f"[dry-run] would write {path}")
        else:
            path.write_text(content, encoding="utf-8")
            ok(f"Written: {path.name}")

    # Auto-rollback watchdog unit (referenced by OnFailure= above).
    rollback_bin = _aria_bin("aria-rollback")
    if rollback_bin:
        rb_path = systemd_dir / _ROLLBACK_UNIT
        if dry_run:
            info(f"[dry-run] would write {rb_path}")
        else:
            rb_path.write_text(_rollback_service(rollback_bin, str(env_file)), encoding="utf-8")
            # Pre-template installs used a plain aria-rollback.service.
            (systemd_dir / "aria-rollback.service").unlink(missing_ok=True)
            ok(f"Written: {_ROLLBACK_UNIT} (auto-rollback + restart watchdog)")
    else:
        warn("aria-rollback binary not found — auto-rollback disabled until you reinstall "
             "(pip install) the new version, then re-run aria-install.")

    section("Enabling user lingering")
    if _linger_enabled():
        ok("Already enabled")
    elif dry_run:
        info("[dry-run] loginctl enable-linger")
    else:
        linger = subprocess.run(["loginctl", "enable-linger"], capture_output=True)
        ok("Enabled") if linger.returncode == 0 else warn("Try: sudo loginctl enable-linger $USER")

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
    try:
        with _env_overlay(_load_existing_env(Path.home() / ".aria" / ".env")):
            for plugin in _channel_plugins().values():
                for spec in plugin.services():
                    if spec.unit not in names:
                        names.append(spec.unit)
    except Exception as exc:
        warn(f"Channel plugins not inspected: {exc}")
    for unit in (_ROLLBACK_UNIT, "aria-rollback.service"):
        path = Path.home() / ".config" / "systemd" / "user" / unit
        if path.exists():
            path.unlink()
            ok(f"Removed: {unit}")
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
