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

## Other tools / areas (add as they come up)

- _(none yet — append here)_
