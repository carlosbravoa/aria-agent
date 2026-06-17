"""
Tests for the text tool-call parser: _TOOL_RE capture + _parse_tool_args
(brace-balanced JSON extraction, ARG heredocs, lenient repair).

This is the most fragile part of the agent — it used to truncate at the first
'}', breaking every coding call. These lock in the fix.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def parse(minimal_env):
    from aria.agent import _TOOL_RE, _parse_tool_args

    def _do(response: str):
        m = _TOOL_RE.search(response)
        assert m, f"_TOOL_RE did not match: {response!r}"
        return m.group("tool_name"), _parse_tool_args(m.group("args"))

    return _do


def test_plain_call(parse):
    name, args = parse('TOOL: web_fetch\nINPUT: {"url": "https://x.com", "max_chars": 2000}')
    assert name == "web_fetch"
    assert args == {"url": "https://x.com", "max_chars": 2000}


def test_brace_inside_string_value(parse):
    # The classic killer: a '}' inside a JSON string value.
    name, args = parse('TOOL: shell_run\nINPUT: {"command": "awk \'{print $1}\' f"}')
    assert args["command"] == "awk '{print $1}' f"


def test_nested_object_value(parse):
    name, args = parse(
        'TOOL: jira\nINPUT: {"fields": {"project": {"key": "ABC"}}, "summary": "x"}'
    )
    assert args["fields"]["project"]["key"] == "ABC"
    assert args["summary"] == "x"


def test_single_quotes_and_trailing_comma_repair(parse):
    name, args = parse("TOOL: file_access\nINPUT: {'action': 'list', 'path': '/tmp',}")
    assert args == {"action": "list", "path": "/tmp"}


def test_heredoc_code_payload(parse):
    resp = (
        'TOOL: shell_run\nINPUT: {"action": "run"}\n'
        "ARG script <<<\n"
        'for f in *.py; do\n  echo "{f}" | grep -q x && echo found\ndone\n'
        ">>>"
    )
    name, args = parse(resp)
    assert args["action"] == "run"
    assert "for f in *.py" in args["script"]
    assert "done" in args["script"]
    # braces/quotes preserved verbatim, no escaping
    assert '"{f}"' in args["script"]


def test_heredoc_overrides_json_key(parse):
    resp = (
        'TOOL: file_access\nINPUT: {"action": "write", "path": "/t/a.py", "content": "ignored"}\n'
        'ARG content <<<\nprint("hi {}")\n>>>'
    )
    name, args = parse(resp)
    assert args["content"] == 'print("hi {}")'
    assert args["path"] == "/t/a.py"


def test_unparseable_raises_value_error(minimal_env):
    from aria.agent import _parse_tool_args
    with pytest.raises(ValueError):
        _parse_tool_args("this is not json and has no heredoc")


def test_code_fence_is_stripped(parse):
    resp = 'TOOL: web_fetch\nINPUT:\n```json\n{"url": "https://x.com"}\n```'
    name, args = parse(resp)
    assert args == {"url": "https://x.com"}


def test_extract_json_object_respects_strings(minimal_env):
    from aria.agent import _extract_json_object
    # the closing brace inside the string must not terminate the object
    assert _extract_json_object('{"a": "x}y", "b": 1}') == '{"a": "x}y", "b": 1}'
    assert _extract_json_object("no object here") is None


def test_multiline_string_value_repaired(parse):
    # LLMs emit multi-line argument values with LITERAL newlines (invalid JSON).
    # This is the briefing-not-sent bug: notify's message never parsed.
    name, args = parse('TOOL: notify\nINPUT: {"message": "line one\nline two\n- bullet"}')
    assert args["message"] == "line one\nline two\n- bullet"


def test_escape_ctrl_only_inside_strings(minimal_env):
    import json
    from aria.agent import _escape_ctrl_in_strings
    # literal newline + tab inside a string → escaped + parseable
    assert json.loads(_escape_ctrl_in_strings('{"m": "a\nb\tc"}'))["m"] == "a\nb\tc"
    # newlines OUTSIDE strings (structure/whitespace) left untouched
    assert _escape_ctrl_in_strings('{\n"a": 1\n}') == '{\n"a": 1\n}'
