"""
aria/channels/vicus/deploy.py — Deploy the Vicus sidecar and check its needs.

The sidecar (`bridge.mjs` + `package.json` + `package-lock.json`) ships in the
repo's top-level `vicus/` directory, outside the pip package, and runs from
`~/.aria/vicus-bridge/` (which also holds its `node_modules/`). The Vicus
client and MLS crate are NOT shipped: they're loaded from the user's Vicus
checkout (VICUS_SOURCE_DIR), which must be built — this module says how when
it isn't. Stdlib only.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from aria.channels.base import Note

_FILES = ("bridge.mjs", "package.json", "package-lock.json")
_CLIENT = Path("packages/client/dist/index.js")
_CRATE = Path("packages/core-crypto/pkg/core_crypto.js")


def dest_dir() -> Path:
    return Path.home() / ".aria" / "vicus-bridge"


def source_dir() -> Path | None:
    """The repo's `vicus/` directory ($ARIA_SOURCE_DIR/vicus, ./vicus, or
    relative to this file), or None."""
    candidates: list[Path] = []
    src = os.environ.get("ARIA_SOURCE_DIR")
    if src:
        candidates.append(Path(src).expanduser() / "vicus")
    candidates.append(Path.cwd() / "vicus")
    # src/aria/channels/vicus/deploy.py → parents[4] == repo root
    candidates.append(Path(__file__).resolve().parents[4] / "vicus")
    for c in candidates:
        if (c / "bridge.mjs").is_file():
            return c
    return None


def checkout_notes() -> list[Note]:
    """What the Vicus checkout still needs, as installer notes."""
    raw = os.environ.get("VICUS_SOURCE_DIR", "").strip()
    if not raw:
        return [("warn", "VICUS_SOURCE_DIR is not set — point it at a Vicus checkout")]
    root = Path(raw).expanduser()
    if not root.is_dir():
        return [("warn", f"VICUS_SOURCE_DIR {root} does not exist")]
    notes: list[Note] = []
    if not (root / _CLIENT).is_file():
        notes.append(("warn", f"Vicus client not built — run: cd {root} && "
                              f"npm --prefix packages/client ci && "
                              f"npm --prefix packages/client run build"))
    if not (root / _CRATE).is_file():
        notes.append(("warn", f"MLS crate not built for Node — run: cargo install wasm-pack "
                              f"&& cd {root}/packages/core-crypto && "
                              f"wasm-pack build --target nodejs"))
    return notes


def install(dry_run: bool = False) -> list[Note]:
    notes: list[Note] = []
    if shutil.which("node") is None:
        notes.append(("warn", "node not found — the Vicus channel needs Node.js 20+"))
    src = source_dir()
    dest = dest_dir()
    if src is None:
        notes.append(("warn", "Vicus bridge source (vicus/bridge.mjs) not found — set "
                              "ARIA_SOURCE_DIR to your aria-agent checkout"))
        return notes + checkout_notes()
    if dry_run:
        notes.append(("info", f"[dry-run] would deploy the Vicus bridge from {src} → {dest}"))
        return notes + checkout_notes()
    dest.mkdir(parents=True, exist_ok=True, mode=0o700)
    copied, package_changed = [], False
    for name in _FILES:
        s, d = src / name, dest / name
        if not s.is_file():
            continue
        if d.exists() and d.read_bytes() == s.read_bytes():
            continue
        shutil.copy2(s, d)
        copied.append(name)
        package_changed |= name != "bridge.mjs"
    if copied:
        notes.append(("ok", f"Deployed Vicus bridge files: {', '.join(copied)} → {dest}"))
    else:
        notes.append(("info", "Vicus bridge files already up to date."))
    if package_changed or not (dest / "node_modules").exists():
        notes.append(("warn" if package_changed else "info", f"Run: cd {dest} && npm ci"))
    return notes + checkout_notes()
