"""
Import-smoke + symbol-existence guards.

These catch the class of regression this codebase has hit repeatedly: a
module-level constant or a `def` line that gets deleted/renamed so the symbol
is referenced but never defined. `ast.parse` and a plain syntax check do NOT
catch these — only an actual import + attribute lookup does.

History this guards against: missing `reflect.run`, `_LEARN_RE`, the `Path`
import, `_PROFILE_STATE`.
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

import pytest

# Modules whose *import* has side effects or needs a TTY; imported explicitly
# in a controlled way rather than in the bulk sweep.
_BULK_SKIP = {"aria.main"}


def _all_library_modules():
    import aria
    pkg_root = Path(aria.__file__).resolve().parent
    names = []
    for mod in pkgutil.walk_packages([str(pkg_root)], prefix="aria."):
        if mod.name in _BULK_SKIP:
            continue
        names.append(mod.name)
    return names


def test_all_modules_import(minimal_env):
    """Every module under aria.* imports cleanly (no NameError/ImportError)."""
    failures = []
    for name in _all_library_modules():
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - we want the full set
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
    assert not failures, "modules failed to import:\n" + "\n".join(failures)


def test_main_module_imports(minimal_env):
    """aria.main imports without triggering the first-run wizard."""
    importlib.import_module("aria.main")


@pytest.mark.parametrize("module, symbol", [
    # The exact regressions that have shipped before:
    ("aria.reflect", "run"),
    ("aria.agent", "_wrap_untrusted"),
    ("aria.agent", "_looks_like_error"),
    ("aria.agent", "_UNTRUSTED_OPEN"),
    ("aria.agent", "_PROFILE_STATE"),
    ("aria.agent", "_MAX_HISTORY"),
    ("aria.agent", "Agent"),
    ("aria.workspace", "Workspace"),
    ("aria.reflect", "main"),
    ("aria.supervisor", "main"),
    # Native tool engine + memory tools (the 2.0 rewrite surface):
    ("aria.tools.remember", "execute"),
    ("aria.tools.learn", "execute"),
])
def test_required_symbol_exists(minimal_env, module, symbol):
    mod = importlib.import_module(module)
    assert hasattr(mod, symbol), f"{module}.{symbol} is missing"


@pytest.mark.parametrize("method", [
    "_call_model", "_run_one_call", "_execute_call", "_assistant_msg",
    "_wire_schemas", "_render_answer", "_render_tool", "_console",
    "_trim_history", "_run_calls_concurrent", "_stream_call",
    "_accumulate_tool_frags", "_assemble_streamed",
    "_record_usage", "_finalize_interrupt",
    "_make_diff", "_render_diff", "_file_edit_target", "_read_text_safe",
])
def test_native_agent_methods_exist(minimal_env, method):
    """Guards the native loop's method surface — a deleted/renamed helper here
    is the 2.0 equivalent of the `_LEARN_RE`/`_PROFILE_STATE` class of bug."""
    from aria.agent import Agent
    assert callable(getattr(Agent, method, None)), f"Agent.{method} missing"


def test_reflect_run_is_callable_with_notify(minimal_env):
    """reflect.run must accept notify= — all four callers pass it."""
    import inspect
    from aria import reflect
    sig = inspect.signature(reflect.run)
    assert "notify" in sig.parameters


def test_every_tool_has_definition_and_execute(minimal_env):
    """Auto-discovered tools must each export a well-formed DEFINITION + execute."""
    from aria import tools
    schemas = tools.load_all()
    assert schemas, "no tools discovered"
    for t in schemas:
        fn = t["function"]
        assert fn.get("name"), f"tool missing name: {t}"
        assert "parameters" in fn, f"{fn.get('name')} missing parameters schema"
        # execute() must be dispatchable
        import aria.tools as _t
        mod = importlib.import_module(f"aria.tools.{fn['name']}")
        assert callable(getattr(mod, "execute", None)), f"{fn['name']}.execute not callable"
