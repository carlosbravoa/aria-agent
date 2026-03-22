# Local LLM Agent

A lean, streaming agent that runs against any OpenAI-compatible local LLM (Ollama, LM Studio, llama.cpp, etc.) with persistent markdown workspace, pluggable tools, and Gmail support via `gog` CLI.

---

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
$EDITOR .env          # set your LLM endpoint, model, etc.

# 3. Run
python main.py
```

---

## Project Structure

```
agent/
├── main.py              # Entry point & REPL
├── agent.py             # Core agentic loop (streaming + tool-use)
├── workspace.py         # Persistent markdown storage
├── requirements.txt
├── .env.example
│
├── tools/
│   ├── __init__.py      # Auto-loader & dispatcher
│   ├── web_fetch.py     # Fetch & extract web page text
│   ├── shell_run.py     # Run shell commands (with confirmation)
│   ├── file_access.py   # Read/write/list/delete files
│   └── gmail.py         # Gmail via `gog` CLI
│
└── workspace/           # Auto-created on first run
    ├── memory/          # Long-term facts & notes (markdown)
    ├── soul/            # Agent identity & persona
    ├── sessions/        # Per-session conversation logs
    └── tools_registry/  # Auto-generated tool docs
```

---

## Configuration (`.env`)

| Variable        | Default                       | Description                        |
|-----------------|-------------------------------|------------------------------------|
| `LLM_BASE_URL`  | `http://localhost:11434/v1`   | OpenAI-compatible endpoint         |
| `LLM_API_KEY`   | `ollama`                      | API key (any string for local LLMs)|
| `LLM_MODEL`     | `llama3.2`                    | Model name                         |
| `AGENT_NAME`    | `Agent`                       | Display name                       |
| `WORKSPACE_DIR` | `./workspace`                 | Workspace root                     |
| `GMAIL_CLI`     | `gog`                         | Gmail CLI binary name              |

---

## Gmail Setup (`gog`)

The agent uses the `gog` CLI for Gmail. Configure it once:

```bash
gog auth login
```

Then set `GMAIL_CLI=gog` in `.env`. If your binary has a different name, set it accordingly.

---

## Adding Tools

Create a new file in `tools/`, e.g. `tools/calculator.py`:

```python
DEFINITION = {
    "name": "calculator",
    "description": "Evaluate a math expression.",
    "parameters": {
        "type": "object",
        "properties": {
            "expression": {"type": "string", "description": "Math expression to evaluate."}
        },
        "required": ["expression"],
    },
}

def execute(args: dict) -> str:
    try:
        return str(eval(args["expression"], {"__builtins__": {}}, {}))
    except Exception as e:
        return f"[error] {e}"
```

That's it — the tool is auto-discovered on next run.

---

## REPL Commands

| Command         | Description                          |
|-----------------|--------------------------------------|
| `/memory`       | Print current memory contents        |
| `/tools`        | List available tools                 |
| `/clear`        | Clear conversation history           |
| `/save <note>`  | Append a note directly to memory     |
| `/quit`         | Exit                                 |

---

## Single-Shot Mode

```bash
python main.py "What is the weather in Santiago?"
python main.py "List my recent emails"
```

---

## Workspace Files

All agent state is plain markdown — editable with any text editor:

- `workspace/soul/identity.md` — edit to change agent personality/instructions
- `workspace/memory/core.md` — long-term memory (agent writes here automatically)
- `workspace/sessions/` — full conversation logs per session
