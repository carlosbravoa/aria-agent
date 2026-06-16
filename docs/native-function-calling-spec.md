# Tool Calling Upgrade — Status & Plan

Status doc for the migration of Aria's tool-calling from the text `TOOL:`/`INPUT:`
protocol toward native (provider) function calling. Written so Step 2 can be
resumed cold. Last updated alongside the Step 1 commit.

## Motivation

Aria invokes tools with a **plain-text protocol** so it works on any
OpenAI-compatible endpoint, including models that are **not** "tool-calling
aware" (older / small / local models):

```
TOOL: shell_run
INPUT: {"action": "run", "command": "ls"}
```

This is intentionally lean — it keeps context small and avoids per-request tool
schemas, which is a core project value (low token cost vs. heavyweight agents).

**The problem:** the text protocol is fragile for **coding**. Hand-writing valid
JSON around code (quotes, braces, newlines, `${VAR}`, closing `}`) routinely
broke tool calls, so Aria could not reliably run code.

**Decision (agreed):** two steps.
- **Step 1 — DONE:** fix the text protocol's payload handling so coding works on
  *every* model, with no API changes. This also becomes the permanent fallback.
- **Step 2 — PLANNED:** make capable models use **native function calling**,
  selectable via `.env`, keeping the text protocol as the legacy fallback.

Why this is lower-risk than it looks:
- Tool `DEFINITION` dicts are **already** in OpenAI function-schema shape.
  `tools/__init__.py` wraps each as `{"type":"function","function": DEFINITION}` —
  exactly what the `tools=[...]` API param wants. **No schema migration.**
- `tools.dispatch(name, args_dict, schemas)` and each tool's `execute(args)` are
  **protocol-agnostic**. Native vs. text only changes how `args_dict` is obtained
  and how tools/results are sent on the wire — not tool execution.

---

## Step 1 — DONE: text-protocol payload fix

All in `src/aria/agent.py`.

### Root cause fixed
`_TOOL_RE` used `INPUT:\s*(\{.*?\})` — the non-greedy `\{.*?\}` stopped at the
**first `}`**, truncating any argument that contained a brace. That was the #1
reason coding failed. It now captures the tool name and **everything after
`INPUT:` to end of message**:

```python
_TOOL_RE = re.compile(
    r"TOOL:\s*(?P<tool_name>\w+)[ \t]*\r?\nINPUT:[ \t]*(?P<args>.+)",
    re.DOTALL,
)
```

### New parser: `_parse_tool_args(raw) -> dict`
Order of operations:
1. **`_extract_heredocs(raw)`** — pulls every `ARG <field> <<< … >>>` block out.
   Content between `<<<` and `>>>` is passed **verbatim** (no JSON escaping), so
   code/scripts/multi-line text go here. Heredoc fields **override** JSON keys of
   the same name. Regex: `_HEREDOC_RE`.
2. **`_strip_fences(text)`** — drops a wrapping ` ```json … ``` ` fence if present.
3. **`_extract_json_object(text)`** — returns the first **brace-balanced** `{...}`,
   tracking string literals + escapes so a `}` inside a string value never
   terminates the object. Fixes nested objects and braces-in-strings.
4. **`_loads_with_repair(obj)`** — `json.loads` → `ast.literal_eval` (single
   quotes / Python literals) → trailing-comma removal.

Raises `ValueError` when nothing parseable is found. In `_run_loop`, that is
caught and a RESULT is fed back hinting the model to retry with a heredoc.

### Heredoc syntax (what the model emits)
```
TOOL: shell_run
INPUT: {"action": "run"}
ARG script <<<
for f in *.py; do awk '{print $1}' "$f"; done
>>>
```
Multiple `ARG` blocks allowed. Small scalar values stay inline in the JSON.

### Where the model learns it
- `_build_system_prompt()` → `## Tool Protocol` → "Passing code or multi-line
  values" subsection documents the heredoc.
- `_few_shot_examples()` includes a `shell_run` heredoc example, rendered into the
  system prompt by `_protocol_examples_block()` (examples live in the system
  prompt, NOT in `history`).

### Verified
- Unit: braces-in-string, heredoc code (braces/quotes/`$1`/`{print $1}`), nested
  JSON, single-quotes+trailing-comma, plain calls, heredoc+JSON merge,
  unparseable→ValueError.
- End-to-end: a brace/quote-heavy `shell_run` heredoc call streams → detects →
  parses → dispatches with the script intact.

---

## Step 2 — PLANNED: native function calling

### Goal
When the model/endpoint supports it, send tools via the provider API and read
structured `tool_calls`, eliminating hand-written JSON entirely. Keep the text
protocol as the fallback for non-tool-aware models.

### Config
- New env var **`LLM_NATIVE_TOOLS = auto | on | off`** (default `auto`).
  - `off` → today's text protocol (legacy).
  - `on` → always native (`tools=[...]`).
  - `auto` → native; fall back to text for the session if the endpoint rejects
    the `tools` param or never emits a tool call when one is clearly needed.
- **Per-profile override:** `LLM_PROFILE<N>_NATIVE_TOOLS=off` (a local llama via
  ollama may not support tools while the default Claude profile does). Resolve the
  effective value when a profile is active (see `switch_profile`/`list_profiles`).

### Implementation map (files / functions)
1. **`_build_system_prompt()`** — branch on native mode: **omit** the
   `## Tool Protocol` section, `_build_tool_docs()` dump, and the tool examples
   (the model gets schemas via `tools=`). KEEP `REMEMBER:`/`LEARN:` instructions
   and the memory system — those are text sentinels, orthogonal to tool calling.
   Net effect: native mode is **leaner** in the system prompt (offsets schema
   tokens on the request; aligns with the low-cost goal).
2. **Sending tools** — pass `tools=self.tool_schemas` (already correct shape) and
   `tool_choice="auto"` to `client.chat.completions.create(...)`.
3. **Reading the reply** — use `message.tool_calls` (list of
   `{id, type:"function", function:{name, arguments}}`). `arguments` is a JSON
   string the provider guarantees valid → `json.loads` cleanly (no `_parse_tool_args`).
4. **Feeding results back** — append the assistant message **with** `tool_calls`,
   then one `{"role":"tool", "tool_call_id": <id>, "content": <result>}` per call.
   This replaces the text `RESULT:` user-turn for native mode.
5. **History representation** — keep `self.history` neutral where possible and
   serialize to the right wire format at send time; in native mode you MUST carry
   the `tool_call_id` so the tool result references it. `_trim_history` and the
   `RESULT:` compaction logic assume text — branch them by mode.
   - The conversation **window** only stores user/assistant display *text*
     (`append_conversation_window`), so tool plumbing does NOT pollute it — keep
     that as-is for both modes.
6. **Memory markers** — `REMEMBER:`/`LEARN:` still appear in `message.content`
   text; keep running `_REMEMBER_RE`/`_LEARN_RE` on the content. No change.
7. **`dispatch()` / `execute()`** — unchanged.
8. **Streaming** — native `tool_calls` arrive as fragmented `delta.tool_calls`
   chunks (accumulate `index`, `id`, `function.name`, `function.arguments`).
   See phasing below.
9. **`auto` detection + fallback** — wrap the native call; on a provider error
   that indicates `tools` is unsupported (400 / specific messages), set a session
   flag to use the text path and retry. Cache the decision per (profile, model).

### Phasing (de-risk in order)
- **Phase 1 — native, non-streaming** (`stream=False`). Simplest correct path for
  message-format + `tool_calls` handling. Loses token-by-token streaming of the
  final answer (acceptable to start).
- **Phase 2 — streaming.** Assemble `delta.tool_calls` fragments; render the final
  text answer through the existing `rich.Live` path. Restores the REPL UX.
- **Phase 3 — parallel tool calls.** Execute all `tool_calls` returned in one
  assistant turn (native APIs can return several), append one `tool` message each.
  Today's loop is strictly one-at-a-time.

### Downsides / risks (why the text fallback stays)
1. Abandons non-tool-aware models — exactly the constituency the text protocol
   serves. Hence keep, don't retire, the legacy path.
2. OpenAI-compat endpoints are inconsistent (ollama/llama.cpp/vLLM partial or
   quirky `tools` support). `auto` is best-effort, not perfect.
3. Streaming `tool_calls` assembly is fiddlier than line-based text → risk of
   regressing the rich.Live UX. (Phase 1 sidesteps it.)
4. Two wire formats to maintain (text `RESULT:` vs `tool`/`tool_call_id`).
5. Parallel tool calls = more loop complexity.
6. Schema tokens per request (offset by the leaner native system prompt + provider
   prompt caching).

### Alternatives considered
- **Fix the text protocol only** (Step 1) and defer native indefinitely — chosen
  for Step 1; native is the strategic follow-up for capable models.
- Sentinel/heredoc-only args (no JSON at all) — heredoc already covers the painful
  case; full removal of JSON is unnecessary.

### Test plan for Step 2
- Native happy path (single tool call) round-trips: assistant `tool_calls` →
  `tool` result → final answer.
- Multi-step (browser-style) sequences under native mode.
- `REMEMBER:`/`LEARN:` still saved + stripped when content accompanies tool_calls.
- `auto` fallback: simulate an endpoint that 400s on `tools=` → drops to text.
- Per-profile override resolves correctly on `switch_profile`.
- Parallel tool calls (Phase 3).
- Regression: `off` mode behaves exactly like today's text protocol.

### Open questions
- Exact `auto` detection signal per provider (error shape vs. empty tool_calls).
- Whether to keep tool docs in the prompt for native mode as a hint, or fully drop
  them (lean wins; start by dropping).
- `tool_choice` default (`"auto"` vs forcing) for small models that over/under-call.
