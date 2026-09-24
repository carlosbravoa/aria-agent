"""
aria/whatsapp_deploy.py — Deploy the Node WhatsApp bridge files.

`bridge.js` + `package.json` ship in the repo's `whatsapp/` directory, which
lives OUTSIDE the pip package — so a plain `pip install` / self-update never
carries them to where they run (`~/.aria/whatsapp/`). That directory also holds
the `npm install` output (`node_modules/`) and the persistent WhatsApp login
state (`.wwebjs_auth/`); this module copies ONLY `bridge.js` + `package.json`
and never touches those.

Used by the installer (`aria-install`) and the self-update tool so the Node side
tracks the Python side automatically instead of silently running a stale bridge.
Stdlib only.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

# package-lock.json pins the exact whatsapp-web.js build (its upstream breaks
# often); `npm ci` installs exactly that.
_FILES = ("bridge.js", "package.json", "package-lock.json")


def dest_dir() -> Path:
    """Where the bridge runs from."""
    return Path.home() / ".aria" / "whatsapp"


def source_dir() -> Path | None:
    """Locate the repo's `whatsapp/` source directory, or None if not found.

    Tries, in order: $ARIA_SOURCE_DIR/whatsapp (the git checkout the update tool
    tracks), ./whatsapp (running `aria-install` from a checkout), and the repo
    root relative to this file (source / editable install)."""
    candidates: list[Path] = []
    src = os.environ.get("ARIA_SOURCE_DIR")
    if src:
        candidates.append(Path(src).expanduser() / "whatsapp")
    candidates.append(Path.cwd() / "whatsapp")
    # src/aria/whatsapp_deploy.py → parents[2] == repo root
    candidates.append(Path(__file__).resolve().parents[2] / "whatsapp")
    for c in candidates:
        if (c / "bridge.js").is_file():
            return c
    return None


def _same(a: Path, b: Path) -> bool:
    try:
        return a.read_bytes() == b.read_bytes()
    except OSError:
        return False


def deploy(dest: Path | None = None) -> dict:
    """Copy `bridge.js` + `package.json` into `dest` (default `~/.aria/whatsapp/`),
    creating it if needed. Existing `node_modules/` and `.wwebjs_auth/` are left
    untouched; a file already identical to the source is skipped.

    Returns a dict: `source` (Path|None), `dest` (Path), `copied`
    (list[str] of filenames actually written), `package_changed` (bool),
    `error` (str|None)."""
    dest = dest or dest_dir()
    result: dict = {"source": None, "dest": dest, "copied": [],
                    "package_changed": False, "error": None}
    src = source_dir()
    if src is None:
        result["error"] = (
            "WhatsApp bridge source not found — set ARIA_SOURCE_DIR to your "
            "aria-agent checkout, or copy whatsapp/bridge.js manually."
        )
        return result
    result["source"] = src
    dest.mkdir(parents=True, exist_ok=True)
    for name in _FILES:
        s = src / name
        if not s.is_file():
            continue
        d = dest / name
        if d.exists() and _same(s, d):
            continue
        shutil.copy2(s, d)
        result["copied"].append(name)
        if name in ("package.json", "package-lock.json"):
            result["package_changed"] = True
    return result
