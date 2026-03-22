"""
aria/tools/__init__.py — Tool registry.

Loads built-in tools from this package, then merges any extra tools found in
the user's ~/.aria/tools/ directory (or $ARIA_TOOLS_DIR).
"""

from __future__ import annotations

import importlib
import importlib.util
import pkgutil
import sys
from pathlib import Path
from typing import Any


def load_all(extra_dir: Path | None = None) -> list[dict[str, Any]]:
    """
    Discover and return OpenAI tool schemas for all available tools.
    Built-in tools are loaded first; user tools in extra_dir override by name.
    """
    schemas: dict[str, dict[str, Any]] = {}

    # 1. Built-in tools (this package)
    pkg_dir = Path(__file__).parent
    for _, name, _ in pkgutil.iter_modules([str(pkg_dir)]):
        if name.startswith("_"):
            continue
        mod = importlib.import_module(f"aria.tools.{name}")
        if hasattr(mod, "DEFINITION") and hasattr(mod, "execute"):
            schemas[mod.DEFINITION["name"]] = {"type": "function", "function": mod.DEFINITION}

    # 2. User tools (loose .py files in extra_dir)
    if extra_dir and extra_dir.is_dir():
        for path in sorted(extra_dir.glob("*.py")):
            if path.stem.startswith("_"):
                continue
            spec = importlib.util.spec_from_file_location(f"_user_tool_{path.stem}", path)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = mod
                spec.loader.exec_module(mod)  # type: ignore[union-attr]
                if hasattr(mod, "DEFINITION") and hasattr(mod, "execute"):
                    schemas[mod.DEFINITION["name"]] = {
                        "type": "function",
                        "function": mod.DEFINITION,
                        "_module": mod,
                    }

    return list(schemas.values())


def dispatch(name: str, args: dict, schemas: list[dict] | None = None) -> str:
    """Route a tool call by name."""
    # Check user tools first (they carry a _module reference)
    if schemas:
        for t in schemas:
            if t.get("function", {}).get("name") == name and "_module" in t:
                try:
                    return t["_module"].execute(args)
                except Exception as exc:
                    return f"[tool_error] {name}: {exc}"

    # Fall back to built-in package tools
    try:
        mod = importlib.import_module(f"aria.tools.{name}")
        return mod.execute(args)
    except ModuleNotFoundError:
        return f"[tool_error] Unknown tool: {name}"
    except Exception as exc:
        return f"[tool_error] {name}: {exc}"
