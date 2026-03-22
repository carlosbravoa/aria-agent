# Aria Agent

A lean agent that runs against any OpenAI-compatible LLM endpoint — local
(Ollama, LM Studio, llama.cpp) or cloud (Anthropic, OpenAI) — with persistent
markdown workspace, pluggable tools, Telegram support, and a first-run setup wizard.

---

## Install

```bash
# Terminal interface only
pip install -e .

# With Telegram bot support
pip install -e ".[telegram]"
```

> **Tip — if pip defaults to user install and things break:**
> ```bash
> pip install -e . --break-system-packages
> # or use a virtualenv:
> python3 -m venv .venv && source .venv/bin/activate && pip install -e .
> ```

---

## First run

On first launch Aria detects there is no config and runs a setup wizard
that creates `~/.aria/` with a `.env` template, then exits with instructions:

```bash
aria
# ~/.aria has been created — edit ~/.aria/.env then run aria again
```

---

## Configure

Edit `~/.aria/.env`. Aria searches for `.env` in this order:

1. `$ARIA_ENV` — explicit path override
2. `~/.aria/.env` — default ← **put it here**
3. `./.env` — current directory (dev convenience)

### Recommended models

Aria uses a plain-text `TOOL:` / `INPUT:` protocol that requires solid
instruction-following. These models work well:

| Provider | Model | Notes |
|----------|-------|-------|
| Ollama | `llama3.2`, `mistral`, `qwen2.5` | Best local option |
| Anthropic | `claude-haiku-4-5`, `claude-sonnet-4-5` | Cloud, reliable |
| OpenAI | `gpt-4o-mini`, `gpt-4o` | Cloud, reliable |

> **Avoid:** on-device runtimes like MediaPipe/Gemma — limited context window
> and no reliable structured output support.

### Ollama example

```bash
ollama pull llama3.2
```

```ini
LLM_BASE_URL=http://localhost:11434/v1
LLM_API_KEY=ollama
LLM_MODEL=llama3.2
AGENT_NAME=Aria
```

### Anthropic example

```ini
LLM_BASE_URL=https://api.anthropic.com/v1
LLM_API_KEY=sk-ant-...
LLM_MODEL=claude-haiku-4-5-20251001
AGENT_NAME=Aria
```

### OpenAI example

```ini
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=sk-...
LLM_MODEL=gpt-4o-mini
AGENT_NAME=Aria
```

### All config variables

| Variable           | Default             | Description                          |
|--------------------|---------------------|--------------------------------------|
| `LLM_BASE_URL`     | *(required)*        | OpenAI-compatible endpoint           |
| `LLM_API_KEY`      | `local`             | API key (any string for local LLMs)  |
| `LLM_MODEL`        | `llama3.2`          | Model name                           |
| `AGENT_NAME`       | `Agent`             | Display name in the terminal         |
| `ARIA_WORKSPACE`   | `~/.aria/workspace` | Override workspace location          |
| `ARIA_TOOLS_DIR`   | `~/.aria/tools`     | Directory for user-added tools       |
| `TELEGRAM_TOKEN`   | —                   | Telegram bot token                   |
| `TELEGRAM_ALLOWED` | —                   | Comma-separated allowed chat IDs     |
| `GMAIL_CLI`        | `gog`               | Gmail CLI binary name                |

---

## Usage

```bash
aria                                 # interactive REPL
aria "how do I list hidden files?"   # single-shot, then exit
aria-telegram                        # Telegram bot
```

### REPL commands

| Command        | Description                      |
|----------------|----------------------------------|
| `/memory`      | Print current memory             |
| `/tools`       | List available tools             |
| `/clear`       | Clear conversation history       |
| `/save <note>` | Append a note directly to memory |
| `/quit`        | Exit                             |

---

## Tool protocol

Aria does not use the OpenAI function-calling API — instead it uses a plain-text
protocol that works with any LLM:

```
TOOL: file_access
INPUT: {"action": "list", "path": "~"}

RESULT: Documents Downloads ...
```

The agent also supports a `REMEMBER:` marker for saving facts to memory:

```
REMEMBER: User prefers dark mode.
```

Both are intercepted by the Python layer — the model never needs to know about
file paths or API calls.

---

## Workspace

All state is plain markdown, fully human-editable:

```
~/.aria/
├── .env                          ← config
├── tools/                        ← drop custom tool .py files here
└── workspace/
    ├── memory/
    │   └── core.md               ← long-term memory (agent writes here)
    ├── soul/
    │   └── identity.md           ← agent persona (edit to customise)
    ├── sessions/
    │   └── session_YYYYMMDD_HHMMSS.md   ← per-session logs
    └── tools_registry/
        └── available_tools.md    ← auto-generated tool docs
```

---

## Built-in tools

| Tool          | Description                                              |
|---------------|----------------------------------------------------------|
| `file_access` | Read, write, append, list, delete local files            |
| `shell_run`   | Run shell commands (requires confirmation before running)|
| `web_fetch`   | Fetch and extract text from a web page                   |
| `gmail`       | List, read, search, send email via `gog` CLI             |

---

## Adding tools

Drop a `.py` file into `~/.aria/tools/`. Two things required:

```python
DEFINITION = {
    "name": "my_tool",
    "description": "What this tool does.",
    "parameters": {
        "type": "object",
        "properties": {
            "input": {"type": "string", "description": "The input value."}
        },
        "required": ["input"],
    },
}

def execute(args: dict) -> str:
    return f"Result: {args['input']}"
```

Restart `aria` — the tool is auto-discovered.

---

## Telegram

1. Create a bot via [@BotFather](https://t.me/BotFather) and copy the token.
2. Get your chat ID by messaging [@userinfobot](https://t.me/userinfobot).
3. Add to `~/.aria/.env`:

```ini
TELEGRAM_TOKEN=<your bot token>
TELEGRAM_ALLOWED=<your chat ID>
```

4. Run:

```bash
aria-telegram
```

Each Telegram chat gets its own isolated agent (separate history and session log).
Available bot commands: `/start`, `/memory`, `/tools`, `/clear`, `/save <note>`.

---

## Gmail setup

```bash
gog auth login     # authenticate once
```

Set `GMAIL_CLI=gog` in `~/.aria/.env`.

---

## Project structure

```
aria_pkg/
├── pyproject.toml
├── README.md
└── src/
    └── aria/
        ├── __init__.py
        ├── agent.py          ← ReAct loop, streaming, tool dispatch
        ├── config.py         ← path resolution, .env loading
        ├── main.py           ← terminal REPL entry point
        ├── setup.py          ← first-run wizard
        ├── telegram_bot.py   ← Telegram interface
        ├── workspace.py      ← markdown persistence (memory, soul, sessions)
        └── tools/
            ├── __init__.py   ← auto-loader & dispatcher
            ├── file_access.py
            ├── gmail.py
            ├── shell_run.py
            └── web_fetch.py
```
