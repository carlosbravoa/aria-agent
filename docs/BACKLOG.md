# Improvement Backlog

Parked improvements to pick up later. Each item notes rough value / effort / risk.
Cross-cutting work with its own design doc is linked rather than duplicated.

See also: [`native-function-calling-spec.md`](native-function-calling-spec.md) —
tool-calling upgrade (Step 1 done; Step 2 native function calling planned).

---

## Jira tool (`src/aria/tools/jira.py`)

Context: migrated `search` to `/rest/api/3/search/jql` (Atlassian removed the old
`/search`), added real error surfacing (`_check`), assignee resolution
(`_resolve_account_id`), and an `update` action. Jira Cloud only.

Not yet done — pick any:

- **`delete` issue** — value: med, effort: low, risk: **destructive**. Add only
  behind a guard/confirmation (e.g. require an explicit `confirm: true` arg), since
  tools run without an interactive prompt in background/channel contexts.
- **List comments in `get`** — value: med, effort: low. `get` currently shows the
  description only; fetch `/issue/{key}/comment` (or `?expand=renderedFields`) and
  render recent comments via `_adf_to_text`.
- **`add_worklog` / time tracking** — value: low–med, effort: low. `POST
  /issue/{key}/worklog` with `timeSpent` + optional comment.
- **Issue links & subtasks** — value: med, effort: med. Create links
  (`POST /issueLink`, types blocks/relates-to/duplicates) and subtasks
  (`issuetype: Subtask` + `parent`). Needs a small link-type lookup.
- **Full `search` pagination** — value: low–med, effort: low. The new endpoint
  returns `nextPageToken`; currently only the first page is returned. Follow the
  token when the agent asks for more than one page.
- **Configurable / richer `search` fields** — value: low, effort: low. Output is a
  fixed set (summary/status/type/priority/assignee). Allow requesting extra fields
  (duedate, labels, components, updated) via an arg.
- **Jira Server / Data Center support** — value: situational, effort: **high**.
  Server uses `/rest/api/2` + wiki markup (not ADF) and still has the old
  `/search`. Would need an API-version branch + a markup path. Only if a user
  actually needs it.

---

## Great-CLI-assistant roadmap

A four-part push to take Aria from a solid assistant to a great CLI/coding one.
Items 1–4 are **in progress** (see commits on `development`). The "lighter"
items below are intentionally deferred and tracked here.

### In progress (1–4)
1. **Plan + self-verification loop** — a `plan`/todo tool with a visible REPL
   checklist, plus a system-prompt habit to verify work after editing (run tests,
   re-read). Make multi-step tasks reliable.
2. **Code editing & navigation** — ripgrep-backed `code_search`, a `git` tool
   (status/diff/log/commit/branch), and `file_access` upgrades: multi-edit,
   line-range edits, and per-edit undo/backup. Reduces tool-thrashing.
3. **Project-scoped context** — load a per-repo conventions file (`AGENTS.md` /
   `.aria.md`) and keep per-project memory separate from global facts.
4. **Trust to run autonomously** — a learnable `[y/N/always]` approval flow that
   persists per command-pattern, and optional real sandboxing (firejail/container).

### Deferred — lighter, high value (pick up after 1–4)
- **Image input** — value: high, effort: med. Accept pasted/attached images
  (screenshots, diagrams) as multimodal content. Needs a vision-capable model and
  content-part message shaping; terminal-only attach via `@image.png`.
- **Auto-compact at a token threshold** — value: med, effort: low. We have manual
  `/compact` (`agent.compact`); trigger it automatically when the session crosses
  a configurable token budget so long sessions never degrade.
- **Cheap-model routing** — value: med, effort: med. Route trivial steps
  (classification, "did this succeed?") to a small profile, reasoning to the big
  one. Reuses the existing profile machinery; token-frugal.
- **MCP support / hooks** — value: med, effort: high. Speak Model Context Protocol
  so external MCP servers appear as tools without writing Python; optional
  pre/post-tool hooks.

## Other tools / areas (add as they come up)

- _(none yet — append here)_
