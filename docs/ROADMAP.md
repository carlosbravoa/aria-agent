# Roadmap

Improvements identified in the September 2026 codebase review that were **not**
part of the bug-fix pass (branch `fix/recurring-tasks-and-bugs`). Bugs found in
that review were fixed there; this file tracks modernisation, refactors and
features. Tool-specific parked items (Jira etc.) stay in
[`BACKLOG.md`](BACKLOG.md).

Each item: **value** / **effort** / notes. Ordered by priority within sections.

---

## 1. Documentation — ✅ done

The local CLAUDE.md (kept outside git) was rewritten from the code (native tool loop, full layout, task
series/dedupe, per-conversation plans/profiles, security model, update/rollback).
README gained an "Upgrading" section and fixed stale counts/filenames. The
native function calling spec is marked implemented.

## 2. Tooling & packaging — ✅ done

No hosted CI: the project is clone + `pip install`. The quality gate is
local: `.pre-commit-config.yaml` runs ruff, mypy and an import smoke test on
every commit (`pip install ".[dev]" && pre-commit install`), plus `pytest`.
Dependencies
have major-version upper bounds; `[dev]` includes ruff, mypy and pre-commit.
`py.typed` was added, and `whatsapp/package-lock.json` is deployed (`npm ci`).

Follow-ups:
- **Tighten mypy** — med / med. It passes with default settings. Next, turn on
  `check_untyped_defs`, then `disallow_untyped_defs` module by module
  (`task.py`, `context.py` and `usage.py` first).
- **Wider ruff rules** — low / low. Only bug-finding rules are on (F, B, UP,
  E4/7/9, W). Consider `I` (import sorting) and `SIM` once someone accepts the
  churn. `ruff format` is deliberately not used, to keep the aligned-`=` style.

## 3. Architecture / refactors

- **Typed settings object** — high / med. Config is read ad hoc through
  `os.environ` across ~20 modules, often at import time, before
  `config.load()`. That has caused real "`.env` ignored" bugs. Replace it with
  one settings object (a dataclass) loaded once, read lazily, and documented in
  one place.
- ✅ **Split `Agent._run_loop`** — done. The guards live in `_TurnGuard`, and
  the loop delegates to `_handle_repeat`, `_run_batch`, `_append_tool_results`
  and `_deliver`. A golden-trace test (`tests/golden/run_loop.json`) pins the
  loop's observable behaviour.
- **Declare side effects explicitly** — med / low.
  - `_classify_side_effect_tools` guesses from keywords in descriptions, and
    "post" matches "postgres". Add a `DELIVERS = True` module flag, like the
    existing `PARALLEL_SAFE`.
  - The browser loop-limit check looks for the word "browser" in the user's
    text. Base it on actual browser tool calls instead.
- ✅ **Shared `gog` runner** — done (`tools/_gog.py`, 81 characterization
  tests). Still open, because they change behaviour: building argv lists
  directly instead of quote-then-split (error messages echo the quoted string),
  drive `read`'s separate bytes-mode call, and the "run, then join output"
  code in shell_run, git, update and code_search.
- **Consistent tool result format** — med / med. Results currently vary:
  `[x error]`, `[x]`, `[git error]`, plain text.
  - Standardise on one error prefix (or a small result type), so
    `_looks_like_error` and friction detection don't depend on a heuristic.
- ✅ **Deduplicate helpers** — done. `_record_feed` and allow-list parsing moved to
  `channel_util.py`, and profile scanning to `_env_profiles()`. `/model` handling
  stays per channel: each channel's output differs, and unifying them would
  change what users see.
- **Spinner verbs** — low / low. `_FILE_VERBS` lacks edit/undo/replace_lines,
  so those show "Accessing" (a cosmetic change). The earlier review got two
  things wrong: the `_output` swap in `chat_collect`/`chat_yield` is not dead,
  because it stops friendly errors printing to stdout in channel services. The
  `web_search` verb also isn't dead, since it labels a custom tool of that
  name.
- ✅ **No side effects on import** — done. `main()` runs the wizard, then
  `config.load()`, then imports Agent, in the same order as before.
- **`build_env()`** — low / low. Parse `.env` with `dotenv_values` instead of the
  hand-rolled parser, which has no `export` and no inline comments.
- **Reflection watermark per channel** — low / med. Only needed for
  multi-user setups.
- **IMAP header fetch** — low / low. It fetches one UID per round-trip; batch
  it into one `FETCH` of a UID set.

## 4. Channels

✅ **Channel plugins** are done. Telegram and WhatsApp are built-in plugins;
custom channels go in `~/.aria/channels/` (`docs/channel-plugins.md`). Legacy
installs work unchanged. Follow-ups:
- ✅ **Attached mode (A)** — done. `ARIA_CHANNEL_MODE_<NAME>=attached` runs a
  channel inside the `aria` CLI only while it's open, with `/remote on|off`. A
  run lock prevents double polling. Telegram supports it.
- ✅ **Remote control of the REPL session (B)** — done. `/remote control` or
  `ARIA_CHANNEL_MODE_<NAME>=control`: phone messages run in the terminal's own
  session, the prompt is interrupted with the typed text preserved, and local
  turns are mirrored.
- **Confirmations to the phone** — med / med. Remote-control turns use the
  unattended shell policy. They could instead ask on the phone (Telegram inline
  buttons), which ties in with the approval-buttons item above.
- **WhatsApp attached mode** — low / med. It would spawn and supervise the Node
  bridge as a child process.
- **Shared slash commands in the host** — med / low. `/clear`, `/model`,
  `/memory` are still implemented separately in each channel. A host-level
  command handler would give custom channels these for free.
- **pip entry-point plugins** — low / low. An `aria.channels` entry-point group,
  for sharing plugins as packages.

- **Telegram concurrency** — high / med. Updates are handled one at a time, so
  one long turn blocks every other chat and command. Enable
  `concurrent_updates`, with one lock per chat so a single chat stays ordered.
- **`/stop` command** — high / med. Cancel a running turn from Telegram or the
  REPL. This needs a cancellation flag that the loop checks between tool calls.
- **Approval buttons for risky tools** — high / med. `shell_run` asks for
  confirmation in the REPL, but channels and the supervisor reject risky
  commands outright. Other risky tools, such as gmail send, drive/calendar
  delete, git push and `update`, have no approval step at all.
  - Add Telegram inline-keyboard approval, reusing the shell confirm flow.
  - Show an "approval needed" notification for supervisor tasks.
- **WhatsApp push delivery** — med / med. Replies still use one synchronous
  HTTP call per turn; the fix pass only raised the timeout. Send them through
  the existing push path so long turns can't time out.
  - Add multi-message replies (Telegram already sends one message per
    response).
  - Add attachment support, as on Telegram.
- **Streaming drafts on Telegram** — med / med. Edit a draft message as text
  arrives. This needs streaming model calls, which are currently always
  non-streaming.

## 4b. Follow-ups from the bug-fix pass

- **Cap the systemd recovery loop** — low / low. A permanent misconfiguration,
  such as a missing token, now makes `aria-rollback@` restart the unit about
  every 2 minutes, forever. Stop after N cycles and send a notification.
- **`code_search` read allow-list** — low / low. It now refuses blocked paths
  (`.env`, `~/.ssh`, …), but unlike `file_access` it doesn't enforce
  `ARIA_FILE_READ_DIRS`. Decide whether coding sessions in arbitrary repos
  should need `authorize`.
- **Validate stored grants** — low / low. `authorized_dirs.json` entries made
  before the new `authorize` checks (e.g. write access to all of `~`) are still
  honoured. The blocked list still wins, but consider warning about them at
  startup.
- **`ARIA_TASK_ID` for all "unattended" signals** — low / low. The flag could
  also gate other tools that shouldn't act on their own inside a scheduled
  task.

## 5. Features

- **`web_search` tool (free backends only)** — med / low. Only `web_fetch`
  exists. Paid search APIs (Brave, Tavily, Serper, …) are ruled out: every one
  charges. Free options:
  - **SearXNG** (self-hosted, JSON API): free and private. Only enabled when
    `SEARXNG_URL` is set; the user runs it (one Docker container).
  - **DuckDuckGo HTML endpoint** (`html.duckduckgo.com`): no key and no cost,
    but it is scraping, so it is rate-limited and can break. Only viable as a
    best-effort fallback.
  - **Already possible today:** the `browser` tool can open a search page in the
    user's own Chromium. Make that a documented pattern (or a thin `search`
    action on the browser tool) instead of a new dependency.
- **Auto compact-and-retry** — high / low. When a request fails on context
  length, compact and retry once automatically. It currently shows a friendly
  error that suggests `/compact`.
- **Vision and voice** — med / med.
  - Pass images to vision-capable models; `attachments.py` currently tells the
    model it can't see them.
  - Transcribe voice notes (e.g. Whisper via an OpenAI-compatible endpoint).
- **Gmail / Calendar depth** — med / med.
  - Gmail: reply in thread, drafts, attachments, archive/labels.
  - Calendar: free/busy queries.
- **Generic `http_request` tool** — med / low. Behind the existing network
  guard, for APIs that have no dedicated tool.
- **`file_access`** — low / low. Move, copy and glob.
- **`git`** — low / low. Stash, restore a file, and open a PR via `gh`.
- **Task queue UX** — med / low.
  - Add `schedule pause/resume` for a series.
  - Give `list` a `done`/`failed` history view.
  - Add cron-like recurrence (`"mon,wed 09:00"`, `"1st of month"`) alongside
    `daily`/`weekly`/`weekdays`/`<N>m`.
- **Real sandboxing for `shell_run`** — med / high. Optional firejail,
  bubblewrap or container execution for unattended contexts (already listed
  under "Trust to run autonomously" in BACKLOG.md).

## 6. Tests

- **Reflection phases** — med / low. `test_reflect.py` covers only the
  "no sessions" path. Test extraction and consolidation against the mock
  client, plus the watermark progression.
- **Concurrent workspace writers** — med / med. Run a multi-process test for the
  file locks.
- **install.py systemd unit content** — low / low.
- **bridge.js** — low / med. There are no JS tests at all.
- **Time-sensitive tests** — low / low. About 22 tests use `datetime.now` or
  sleeps. Audit them for failures at midnight or on second boundaries, and
  freeze the clock where needed.
