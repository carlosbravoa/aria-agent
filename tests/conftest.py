"""
Shared pytest fixtures for the Aria test suite.

Tests run fully offline: no real LLM, no network, an isolated tmp workspace and
HOME so nothing touches the developer's ~/.aria.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Make `src/aria` importable without requiring an editable install.
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Guard import-time side effects: aria.main runs setup.is_first_run() at import,
# which is False whenever ARIA_ENV is set. Set a harmless value for the whole
# session so importing any module never triggers the first-run wizard.
os.environ.setdefault("ARIA_ENV", str(_SRC.parent / "tests" / ".pytest_env"))


@pytest.fixture
def minimal_env(tmp_path, monkeypatch):
    """Isolated environment: tmp workspace + HOME, dummy LLM config, no
    background reflection, no real .env / profile state leaking in."""
    ws    = tmp_path / "workspace"
    home  = tmp_path / "home"
    tools = tmp_path / "tools"
    home.mkdir(parents=True, exist_ok=True)
    envf  = tmp_path / ".env"
    envf.write_text("")  # ARIA_ENV set → is_first_run() is False

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ARIA_ENV", str(envf))
    monkeypatch.setenv("ARIA_WORKSPACE", str(ws))
    monkeypatch.setenv("ARIA_TOOLS_DIR", str(tools))
    monkeypatch.setenv("LLM_BASE_URL", "http://test.invalid")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setenv("AGENT_NAME", "Aria")
    monkeypatch.setenv("ARIA_REFLECT_EVERY", "0")  # no background reflect thread
    return ws


@pytest.fixture
def tmp_workspace(minimal_env):
    """A fresh Workspace rooted in the isolated tmp dir."""
    from aria.workspace import Workspace
    return Workspace(minimal_env)


@pytest.fixture
def mock_client():
    """Factory: mock_client(resp1, resp2, ...) returns a stand-in OpenAI client
    whose streaming `chat.completions.create` yields each response in turn (as
    chunked deltas). Assign it to `agent.client`. After the list is exhausted the
    last response repeats (so loop-guard tests terminate)."""
    import re

    class _Delta:
        def __init__(self, c): self.content = c

    class _Choice:
        def __init__(self, c): self.delta = _Delta(c)

    class _Chunk:
        def __init__(self, c): self.choices = [_Choice(c)]

    class _Completions:
        def __init__(self, resps): self._resps = list(resps); self._i = 0

        def create(self, **kwargs):
            if self._i < len(self._resps):
                text = self._resps[self._i]
            else:
                text = self._resps[-1] if self._resps else ""
            self._i += 1
            # chunk per line (newline kept) to exercise the line-buffered parser
            parts = re.findall(r"[^\n]*\n|[^\n]+", text) or [""]
            return iter([_Chunk(p) for p in parts])

    class _Chat:
        def __init__(self, resps): self.completions = _Completions(resps)

    class _Client:
        def __init__(self, resps): self.chat = _Chat(resps)

    def _make(*responses):
        return _Client(responses)

    return _make


@pytest.fixture
def native_client():
    """Factory for a NON-streaming mock OpenAI client matching the native
    tool-calling engine. Each argument is one model turn:

      - a plain string                → a content-only final answer
      - {"content": str|None,
         "tool_calls": [(name, args), ...]}  → an assistant turn with tool calls
        where `args` is a dict (json-encoded for you) or a raw JSON string.

    `chat.completions.create(...)` returns an object exposing
    `.choices[0].message.content` and `.choices[0].message.tool_calls`. After the
    list is exhausted the last turn repeats (so loop-guard tests terminate)."""
    import json
    import itertools

    _ids = itertools.count(1)

    class _Fn:
        def __init__(self, name, arguments):
            self.name = name
            self.arguments = arguments

    class _ToolCall:
        def __init__(self, name, arguments):
            self.id = f"call_{next(_ids)}"
            self.type = "function"
            self.function = _Fn(name, arguments)

    class _Message:
        def __init__(self, content, tool_calls):
            self.content = content
            self.tool_calls = tool_calls or None

    class _Choice:
        def __init__(self, msg): self.message = msg

    class _Resp:
        def __init__(self, msg): self.choices = [_Choice(msg)]

    class _Completions:
        def __init__(self, turns): self._turns = list(turns); self._i = 0

        def create(self, **kwargs):
            turn = (self._turns[self._i] if self._i < len(self._turns)
                    else (self._turns[-1] if self._turns else ""))
            self._i += 1
            if isinstance(turn, str):
                turn = {"content": turn, "tool_calls": []}
            tcs = []
            for name, args in turn.get("tool_calls", []):
                raw = args if isinstance(args, str) else json.dumps(args)
                tcs.append(_ToolCall(name, raw))
            return _Resp(_Message(turn.get("content"), tcs))

    class _Chat:
        def __init__(self, turns): self.completions = _Completions(turns)

    class _Client:
        def __init__(self, turns): self.chat = _Chat(turns)

    def _make(*turns):
        return _Client(turns)

    return _make
