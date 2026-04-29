"""
aria/agent.py — ReAct-style agentic loop for any local LLM.

Tools are invoked via a plain-text protocol:

    TOOL: tool_name
    INPUT: {"arg": "value"}

Memory is saved via:

    REMEMBER: some fact

Session continuity: agent.close() summarises the conversation and saves it
to workspace/memory/last_session.md. Next session, that summary is loaded
into the system prompt for lightweight continuity without replaying history.
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
        # Default output: plain print for streaming tokens.
        # Rich is used for status lines (tool calls, memory saves) via console.print.
        self._output = output_callback or (lambda t: print(t, end="", flush=True))
        self._is_terminal = output_callback is None
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
        soul         = self.ws.load_soul()
        memory       = self.ws.load_memory()
        tool_docs    = self._build_tool_docs()
        last_session = self.ws.last_session_summary()
        prev_block   = last_session if last_session else "_No previous session._"

        return (
            f"{soul}\n\n"
            "## Memory\n"
            f"{memory}\n\n"
            "## Previous Session\n"
            f"{prev_block}\n\n"
            "## Tool Protocol\n"
            "To call a tool, output EXACTLY these two lines with no other text before them:\n\n"
            "TOOL: <tool_name>\n"
            'INPUT: {"key": "value"}\n\n'
            "The system runs it and replies:\n\n"
            "RESULT: <o>\n\n"
            "After RESULT, write your final answer in plain text.\n\n"
            f"{tool_docs}\n\n"
            "## Saving to Memory\n"
            "To save a fact, output this anywhere in your response:\n\n"
            "REMEMBER: <the fact>\n\n"
            "## Rules\n"
            "- Use TOOL:/INPUT: for tool calls. No other syntax works.\n"
            "- Use REMEMBER: to persist facts.\n"
            "- Never narrate before a tool call. Emit TOOL: immediately.\n"
            "- After RESULT, answer in plain text.\n"
            "- You know your available tools from the list above — never call a tool to look them up.\n"
            "- Be concise.\n"
        )

    def _build_tool_docs(self) -> str:
        """Build tool docs dynamically — never hardcodes tool names."""
        if not self.tool_schemas:
            return "_No tools available._"
        lines = ["### Available Tools\n"]
        for t in self.tool_schemas:
            fn       = t["function"]
            props    = fn.get("parameters", {}).get("properties", {})
            required = fn.get("parameters", {}).get("required", [])
            args     = ", ".join(f"{k}{'*' if k in required else '?'}" for k in props)
            lines.append(f"#### `{fn['name']}`({args})")
            lines.append(fn["description"] + "\n")
        lines.append("_* required, ? optional_")
        return "\n".join(lines)

    def _few_shot_examples(self) -> list[dict]:
        """
        Protocol examples — tool-agnostic except for the file_access demo
        which is needed to show the TOOL:/INPUT: format concretely.
        """
        return [
            {"role": "user", "content": "List the files in /tmp"},
            {"role": "assistant", "content": 'TOOL: file_access\nINPUT: {"action": "list", "path": "/tmp"}'},
            {"role": "user", "content": "RESULT: notes.txt\nreport.pdf"},
            {"role": "assistant", "content": "/tmp contains: notes.txt, report.pdf."},
            {"role": "user", "content": "What is 2+2?"},
            {"role": "assistant", "content": "4."},
            {"role": "user", "content": "My name is <username>."},
            {"role": "assistant", "content": "REMEMBER: User name is <username>.\nNice to meet you, <username>!"},
            {"role": "user", "content": "scheduled task: summarise top stories from https://news.ycombinator.com"},
            {"role": "assistant", "content": 'TOOL: web_fetch\nINPUT: {"url": "https://news.ycombinator.com", "max_chars": 2000}'},
            {"role": "user", "content": "RESULT: 1. Story A\n2. Story B\n3. Story C"},
            {"role": "assistant", "content": 'TOOL: notify\nINPUT: {"message": "Top HN stories:\\n1. Story A\\n2. Story B\\n3. Story C"}'},
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
        """
        Run a chat turn and return only the final response text.
        Suppresses all status output (tool calls, memory saves, name prefix)
        — safe for Telegram, WhatsApp, supervisor tasks.
        """
        # Capture everything but discard it — we only want the final answer
        # which _run_loop writes to self.history[-1]["content"].
        buf: list[str] = []
        orig = self._output
        self._output = buf.append
        try:
            self.chat(user_input)
        finally:
            self._output = orig
        # Extract the clean final answer directly from history,
        # bypassing all the status noise captured in buf.
        seed_len = len(self._seed)
        real = self.history[seed_len:]
        # Find the last assistant message that isn't a tool call
        for msg in reversed(real):
            if msg["role"] == "assistant":
                content = msg.get("content", "").strip()
                # Skip raw tool call lines — the final answer never starts with TOOL:
                if not content.startswith("TOOL:"):
                    return content
        return ""

    def _trim_history(self) -> None:
        """Compress old RESULT blocks and drop oldest turns to stay within limits."""
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
            if msg["role"] != "assistant" or i == last_asst_idx:
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

            for m in _REMEMBER_RE.finditer(response):
                note = m.group("note").strip()
                if note:
                    self.ws.append_memory(f"- {note}")
                    if self._is_terminal:
                        from rich.console import Console
                        Console().print(f"  [dim]💾 saved: {note}[/dim]")
                    else:
                        self._output(f"  💾 saved: {note}\n")

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

            call_sig = f"{tool_name}:{raw_args}"
            if call_sig in seen_calls:
                msg = f"(tool {tool_name} called repeatedly with same args — stopping)"
                self._output(f"  \u26a0\ufe0f  {msg}\n")
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

            if not (self.history and self.history[-1]["role"] == "assistant"):
                self.history.append({"role": "assistant", "content": response})
            self.history.append({"role": "user", "content": f"RESULT: {result}"})

        if self._is_terminal:
            from rich.console import Console
            Console().print(f"\n  [yellow]⚠ Hit loop limit ({_MAX_LOOPS}).[/yellow]")
        else:
            self._output(f"[{self.name}] Hit loop limit ({_MAX_LOOPS}).\n")

    # ── Streaming ────────────────────────────────────────────────────────────

    def _stream_response(self) -> str:
        now        = datetime.now(timezone.utc).astimezone()
        time_ctx   = f"Current date and time: {now.strftime('%A, %Y-%m-%d %H:%M %Z')}"
        from aria import __version__
        sys_prompt = self.system_prompt + f"\n\n## Context\n{time_ctx}\nVersion: {__version__}\n"
        messages   = [{"role": "system", "content": sys_prompt}] + self.history

        while messages and messages[-1]["role"] == "assistant":
            messages = messages[:-1]

        stream = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            stream=True,
        )

        if self._is_terminal:
            from rich.console import Console
            Console(highlight=False).print(f"\n  [bold green]{self.name}[/bold green] ", end="")
        else:
            self._output(f"\n{self.name}: ")

        full_text    = ""
        line_buf     = ""    # accumulates tokens until a newline is received
        in_tool_call = False

        for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                token      = delta.content
                full_text += token

                if in_tool_call:
                    continue  # suppressing — just accumulate into full_text

                line_buf += token

                # Process only when we have complete lines (split on \n)
                while "\n" in line_buf:
                    line, line_buf = line_buf.split("\n", 1)
                    if line.startswith("TOOL:"):
                        in_tool_call = True
                        line_buf = ""  # discard rest
                        break
                    if line.startswith("REMEMBER:"):
                        pass  # suppress silently
                    else:
                        self._output(line + "\n")

        # Stream ended — flush any remaining partial line
        if line_buf and not in_tool_call:
            if not line_buf.startswith("REMEMBER:") and not line_buf.startswith("TOOL:"):
                self._output(line_buf)

        tool_match = _TOOL_RE.search(full_text)
        if tool_match:
            if self._is_terminal:
                from rich.console import Console
                Console().print(f"  [dim]⚙ calling [bold]{tool_match.group('tool_name')}[/bold]...[/dim]")
            else:
                self._output(f"⚙ calling {tool_match.group('tool_name')}...\n")
        else:
            self._output("\n")

        self.history.append({"role": "assistant", "content": full_text})
        return full_text

    # ── Session continuity ────────────────────────────────────────────────────

    def summarise_session(self) -> str | None:
        """
        One-shot (non-streaming) LLM call producing a 3-5 bullet summary.
        Skips RESULT: blocks to keep the transcript compact.
        Returns None if there were no real exchanges this session.
        """
        seed_len = len(self._seed)
        real = self.history[seed_len:]
        if not real:
            return None

        transcript_lines = []
        for msg in real:
            role    = msg["role"]
            content = msg.get("content") or ""
            if role == "user" and content.startswith("RESULT:"):
                continue
            if role in ("user", "assistant"):
                prefix  = "User" if role == "user" else self.name
                snippet = content[:300] + ("\u2026" if len(content) > 300 else "")
                transcript_lines.append(f"{prefix}: {snippet}")

        if not transcript_lines:
            return None

        # ── Short session heuristic ───────────────────────────────────────────
        # Less than 3 exchanges (6 lines) is likely just a greeting or a very
        # brief question. Skip the LLM call entirely:
        #   - If there is only 1 user message, save it as a minimal context hook
        #     so the next session knows what the last thing asked was.
        #   - If there are 2-3 exchanges, do the same — not worth summarising.
        # This avoids saving the LLM's meta-commentary ("nothing to summarise").
        _MIN_EXCHANGES = 4  # lines = 2 user + 2 assistant turns minimum
        if len(transcript_lines) < _MIN_EXCHANGES:
            user_msgs = [l for l in transcript_lines if l.startswith("User:")]
            if user_msgs:
                last = user_msgs[-1].removeprefix("User:").strip()
                return f"- Last message: {last[:200]}"
            return None

        transcript = "\n".join(transcript_lines)
        prompt = (
            "Summarise this conversation in 3-5 bullet points. "
            "Focus only on decisions made, facts learned, and tasks completed. "
            "If there is nothing meaningful to summarise, respond with exactly: SKIP\n\n"
            "Be brief — this summary will be used as context for the next session.\n\n"
            + transcript
        )

        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                stream=False,
            )
            result = resp.choices[0].message.content.strip()
            # Model signalled nothing worth saving
            if result.upper().startswith("SKIP"):
                return None
            return result
        except Exception:
            return None  # best-effort, never block on failure

    def close(self) -> None:
        """Summarise this session and persist it.
        Always writes to last_session.md so the model never recalls
        a stale older session after a series of trivial ones.
        """
        summary = self.summarise_session()
        if summary:
            self.ws.save_session_summary(summary)
        else:
            # Nothing meaningful happened but we still record a timestamp
            # so the next session knows this was a short/empty interaction.
            self.ws.save_session_summary(
                f"- Brief or empty session — nothing significant to summarise."
            )

    # ── Tool execution ────────────────────────────────────────────────────────

    def _execute_tool(self, name: str, args: dict) -> str:
        result = tools.dispatch(name, args, self.tool_schemas)
        if len(result) > 6000:
            result = result[:6000] + "\n\u2026 [truncated]"
        return result
