"""
aria/agent.py — ReAct-style agentic loop for any local LLM.

Tools are invoked via a plain-text protocol:

    TOOL: tool_name
    INPUT: {"arg": "value"}

Memory is saved via:

    REMEMBER: some fact
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any

from openai import OpenAI

from aria import tools
from aria.workspace import Workspace

_TOOL_RE = re.compile(
    r"TOOL:\s*(?P<tool_name>\w+)\s*\nINPUT:\s*(?P<args>\{.*?\})",
    re.DOTALL,
)
_REMEMBER_RE = re.compile(r"REMEMBER:\s*(?P<note>[^\n]+)")
# Both configurable via ~/.aria/.env
_MAX_LOOPS   = int(os.environ.get("ARIA_MAX_LOOPS",   "20"))
_MAX_HISTORY = int(os.environ.get("ARIA_MAX_HISTORY", "60"))


class Agent:
    def __init__(self, output_callback=None) -> None:
        # output_callback(text) receives streamed/final output.
        # Defaults to printing to stdout (terminal interface).
        self._output = output_callback or (lambda t: print(t, end="", flush=True))
        self.client = OpenAI(
            base_url=os.environ["LLM_BASE_URL"],
            api_key=os.environ.get("LLM_API_KEY", "local"),
        )
        self.model: str = os.environ.get("LLM_MODEL", "llama3.2")
        self.name: str  = os.environ.get("AGENT_NAME", "Agent")

        from aria import config
        self.ws = Workspace(config.workspace_dir())
        self.tool_schemas = tools.load_all(config.tools_dir())
        self.ws.update_tools_registry(self.tool_schemas)

        self.system_prompt = self._build_system_prompt()
        self._seed = self._few_shot_examples()
        self.history: list[dict[str, Any]] = list(self._seed)
        self.session_log = self.ws.new_session_path()

    # ── System prompt ────────────────────────────────────────────────────────

    def _build_system_prompt(self) -> str:
        soul     = self.ws.load_soul()
        memory   = self.ws.load_memory()
        tool_docs = self._build_tool_docs()

        return f"""{soul}

## Memory
{memory}

## Tool Protocol
To call a tool, output EXACTLY these two lines with no other text before them:

TOOL: <tool_name>
INPUT: {{"key": "value"}}

The system runs it and replies:

RESULT: <o>

After RESULT, write your final answer in plain text.

{tool_docs}

## Saving to Memory
To save a fact, output this anywhere in your response:

REMEMBER: <the fact>

## Rules
- Use TOOL:/INPUT: for tool calls. No other syntax works.
- Use REMEMBER: to persist facts.
- Never narrate before a tool call. Emit TOOL: immediately.
- After RESULT, answer in plain text.
- You know your available tools from the list above — never call a tool to look them up.
- Be concise.
"""

    def _build_tool_docs(self) -> str:
        """
        Build tool documentation dynamically from loaded schemas.
        Never hardcodes tool names — works with any set of tools.
        """
        if not self.tool_schemas:
            return "_No tools available._"
        lines = ["### Available Tools\n"]
        for t in self.tool_schemas:
            fn       = t["function"]
            props    = fn.get("parameters", {}).get("properties", {})
            required = fn.get("parameters", {}).get("required", [])
            args     = ", ".join(f"{k}{'*' if k in required else '?'}" for k in props)
            # Include full description so the model understands each tool's capabilities
            lines.append(f"#### `{fn['name']}`({args})")
            lines.append(fn["description"] + "\n")
        lines.append("_* required, ? optional_")
        return "\n".join(lines)

    def _few_shot_examples(self) -> list[dict]:
        """
        Minimal examples teaching the TOOL:/INPUT: and REMEMBER: protocols.
        Deliberately tool-agnostic — does not list or name specific tools
        so it stays valid regardless of which tools are installed.
        """
        return [
            # Tool use
            {"role": "user", "content": "List the files in /tmp"},
            {
                "role": "assistant",
                "content": 'TOOL: file_access\nINPUT: {"action": "list", "path": "/tmp"}',
            },
            {"role": "user", "content": "RESULT: notes.txt\nreport.pdf"},
            {"role": "assistant", "content": "/tmp contains: notes.txt, report.pdf."},
            # No-tool
            {"role": "user", "content": "What is 2+2?"},
            {"role": "assistant", "content": "4."},
            # Memory save
            {"role": "user", "content": "My name is Alice."},
            {
                "role": "assistant",
                "content": "REMEMBER: User name is Alice.\nNice to meet you, Alice!",
            },
            # Scheduled task with notify
            {"role": "user", "content": "scheduled task: summarise top stories from https://news.ycombinator.com"},
            {
                "role": "assistant",
                "content": 'TOOL: web_fetch\nINPUT: {"url": "https://news.ycombinator.com", "max_chars": 2000}',
            },
            {"role": "user", "content": "RESULT: 1. Story A\n2. Story B\n3. Story C"},
            {
                "role": "assistant",
                "content": 'TOOL: notify\nINPUT: {"message": "Top HN stories:\\n1. Story A\\n2. Story B\\n3. Story C"}',
            },
            {"role": "user", "content": "RESULT: [notify] Message sent."},
            {"role": "assistant", "content": "Done. Summary sent."},
        ]

    # ── Public interface ─────────────────────────────────────────────────────

    def chat(self, user_input: str) -> None:
        """Send a message; output goes to self._output callback."""
        self.history.append({"role": "user", "content": user_input})
        self.ws.log_session(self.session_log, "user", user_input)
        self._trim_history()
        self._run_loop()

    def chat_collect(self, user_input: str) -> str:
        """Like chat() but captures output and returns it as a string.
        Used by non-terminal interfaces (e.g. Telegram, WhatsApp).
        """
        buf: list[str] = []
        orig = self._output
        self._output = buf.append
        try:
            self.chat(user_input)
        finally:
            self._output = orig
        return "".join(buf).strip()

    def _trim_history(self) -> None:
        """
        Keep history healthy before each turn:
        1. Compress old RESULT: blocks to avoid context overflow.
        2. Drop oldest non-seed turns if total exceeds _MAX_HISTORY.
        """
        seed_len = len(self._seed)
        real = self.history[seed_len:]

        last_asst_idx = None
        for i in range(len(real) - 1, -1, -1):
            if real[i]["role"] == "assistant":
                last_asst_idx = i
                break

        for i, msg in enumerate(real):
            if msg["role"] == "user" and msg["content"].startswith("RESULT:"):
                if i == last_asst_idx:
                    continue
                if len(msg["content"]) > 400:
                    real[i] = {**msg, "content": "RESULT: [output truncated — already processed]"}
                continue
            if msg["role"] != "assistant":
                continue
            if i == last_asst_idx:
                continue

        excess = len(real) - _MAX_HISTORY
        if excess > 0:
            real = real[excess:]

        self.history = self._seed + real

    # ── ReAct loop ───────────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        seen_calls: list[str] = []

        for _ in range(_MAX_LOOPS):
            response = self._stream_response()

            # Persist REMEMBER: lines
            for m in _REMEMBER_RE.finditer(response):
                note = m.group("note").strip()
                if note:
                    self.ws.append_memory(f"- {note}")
                    self._output(f"  \U0001f4be saved: {note}\n")

            tool_match = _TOOL_RE.search(response)

            if not tool_match:
                clean   = re.sub(r"TOOL:.*", "", response, flags=re.DOTALL).strip()
                display = clean or response.strip() or "(no response)"
                self.ws.log_session(self.session_log, self.name, display)
                if self.history and self.history[-1]["role"] == "assistant":
                    self.history[-1]["content"] = display
                return

            tool_name = tool_match.group("tool_name")
            raw_args  = tool_match.group("args")

            # Detect spinning
            call_sig = f"{tool_name}:{raw_args}"
            if call_sig in seen_calls:
                msg = f"(tool {tool_name} called repeatedly with same args — stopping)"
                self._output(f"  ⚠️  {msg}\n")
                self.ws.log_session(self.session_log, self.name, msg)
                if self.history and self.history[-1]["role"] == "assistant":
                    self.history[-1]["content"] = msg
                return
            seen_calls.append(call_sig)

            try:
                args = json.loads(raw_args)
            except json.JSONDecodeError:
                result = f"[agent] Invalid JSON: {raw_args}"
            else:
                result = self._execute_tool(tool_name, args)

            self.ws.log_session(
                self.session_log,
                f"tool:{tool_name}",
                f"**Input:** `{raw_args}`\n\n**Output:**\n```\n{result}\n```",
            )

            # Keep assistant message (TOOL call), inject result as user turn.
            # Ensures conversation always ends on a user turn (required by Anthropic API).
            if not (self.history and self.history[-1]["role"] == "assistant"):
                self.history.append({"role": "assistant", "content": response})
            self.history.append({"role": "user", "content": f"RESULT: {result}"})

        self._output(f"\n[{self.name}] Hit loop limit ({_MAX_LOOPS}).\n")

    # ── Streaming ────────────────────────────────────────────────────────────

    def _stream_response(self) -> str:
        now        = datetime.now(timezone.utc).astimezone()
        time_ctx   = f"Current date and time: {now.strftime('%A, %Y-%m-%d %H:%M %Z')}"
        sys_prompt = self.system_prompt + f"\n\n## Context\n{time_ctx}\n"
        messages   = [{"role": "system", "content": sys_prompt}] + self.history

        # Trim trailing assistant turns — Anthropic API rejects them
        while messages and messages[-1]["role"] == "assistant":
            messages = messages[:-1]

        stream = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            stream=True,
        )

        self._output(f"\n{self.name}: ")

        full_text = ""
        line_buf  = ""
        buffering = False

        for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                token = delta.content
                full_text += token
                if not buffering:
                    if "TOOL:" in full_text or "REMEMBER:" in full_text:
                        buffering = True
                        line_buf += token
                    else:
                        self._output(token)
                else:
                    line_buf += token

        tool_match = _TOOL_RE.search(full_text)
        if tool_match:
            self._output(f"\U0001f527 calling {tool_match.group('tool_name')}...\n")
        elif buffering:
            visible = re.sub(r"REMEMBER:[^\n]*\n?", "", line_buf).strip()
            self._output((visible or "") + "\n")
        else:
            self._output("\n")

        self.history.append({"role": "assistant", "content": full_text})
        return full_text

    # ── Tool execution ────────────────────────────────────────────────────────

    def _execute_tool(self, name: str, args: dict) -> str:
        result = tools.dispatch(name, args, self.tool_schemas)
        if len(result) > 6000:
            result = result[:6000] + "\n\u2026 [truncated]"
        return result
