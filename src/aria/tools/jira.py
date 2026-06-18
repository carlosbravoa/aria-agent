"""
aria/tools/jira.py — Jira issue management via the Jira REST API.

Uses httpx (already a project dependency) — no extra binary or library needed.

Setup: add to ~/.aria/.env:
  JIRA_BASE_URL=https://yourcompany.atlassian.net
  JIRA_EMAIL=you@yourcompany.com
  JIRA_API_TOKEN=your-api-token   # from https://id.atlassian.com/manage-profile/security/api-tokens
  JIRA_DEFAULT_PROJECT=PROJ       # optional default project key

Jira Cloud only — uses REST API v3 (/rest/api/3) and Atlassian Document Format.
Jira Server / Data Center (which uses /rest/api/2 and wiki markup) is NOT
supported.
"""

from __future__ import annotations

import os

# Stateless REST calls over httpx, no shared local state — a batch may run
# concurrently.
PARALLEL_SAFE = True

DEFINITION = {
    "name": "jira",
    "description": (
        "Manage Jira issues (Jira Cloud). "
        "Actions: create (new issue), update (edit summary/description/priority/"
        "labels/components of an existing issue), get (issue details), search "
        "(JQL query), comment (add a comment), transition (change status), assign "
        "(assign to user), list_projects (show available projects).\n"
        "Useful JQL patterns for search:\n"
        "  - My open tickets: assignee = currentUser() AND statusCategory != Done\n"
        "  - Recent activity: assignee = currentUser() ORDER BY updated DESC\n"
        "  - Bugs in project: project = PROJ AND issuetype = Bug AND status != Done\n"
        "  - Unassigned in project: project = PROJ AND assignee is EMPTY\n"
        "  - Due soon: duedate <= 7d AND statusCategory != Done\n"
        "currentUser() always resolves to the authenticated account."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "update", "get", "search", "comment", "transition", "assign", "list_projects"],
                "description": "Jira action to perform.",
            },
            "project": {
                "type": "string",
                "description": "Project key (e.g. PROJ). Uses JIRA_DEFAULT_PROJECT if not set.",
            },
            "issue_key": {
                "type": "string",
                "description": "Issue key (e.g. PROJ-123). Required for get, update, comment, transition, assign.",
            },
            "summary": {
                "type": "string",
                "description": "Issue title. Required for create.",
            },
            "description": {
                "type": "string",
                "description": "Issue description body (Markdown supported on Jira Cloud).",
            },
            "issue_type": {
                "type": "string",
                "description": "Issue type: Story, Bug, Task, Epic, Subtask, etc. Default: Task.",
                "default": "Task",
            },
            "priority": {
                "type": "string",
                "description": "Priority: Highest, High, Medium, Low, Lowest.",
            },
            "assignee": {
                "type": "string",
                "description": "Assignee: accountId, email, or display name (resolved "
                               "automatically), 'me' for yourself, or 'none' to unassign. "
                               "Used by assign and on create.",
            },
            "labels": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Labels to attach to the issue.",
            },
            "components": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Component names to attach to the issue.",
            },
            "jql": {
                "type": "string",
                "description": "JQL query string for search action.",
            },
            "max_results": {
                "type": "integer",
                "description": "Max results for search (default 10).",
                "default": 10,
            },
            "comment_body": {
                "type": "string",
                "description": "Comment text. Required for comment action.",
            },
            "transition_name": {
                "type": "string",
                "description": "Transition name to apply, e.g. 'In Progress', 'Done'. Required for transition.",
            },
        },
        "required": ["action"],
    },
}


def _client():
    """Return a configured httpx client with Jira auth."""
    import httpx

    base_url = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
    email     = os.environ.get("JIRA_EMAIL", "")
    token     = os.environ.get("JIRA_API_TOKEN", "")

    if not base_url:
        raise ValueError("JIRA_BASE_URL not set in ~/.aria/.env")
    if not email or not token:
        raise ValueError("JIRA_EMAIL and JIRA_API_TOKEN must be set in ~/.aria/.env")

    return httpx.Client(
        base_url=f"{base_url}/rest/api/3",
        auth=(email, token),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=15,
    )


def _check(r) -> None:
    """Raise with Jira's actual error detail instead of httpx's generic message.
    Jira returns useful reasons in errorMessages/errors — surface them."""
    if r.status_code < 400:
        return
    detail = ""
    try:
        j = r.json()
        parts = list(j.get("errorMessages") or [])
        parts += [f"{k}: {v}" for k, v in (j.get("errors") or {}).items()]
        detail = "; ".join(parts)
    except Exception:
        detail = (r.text or "")[:300]
    raise ValueError(f"HTTP {r.status_code} on {r.request.url.path} — {detail or 'request failed'}")


def _resolve_account_id(client, who: str):
    """Resolve an assignee string to a Jira Cloud accountId.
    Accepts an accountId as-is, an email or display name (looked up via
    /user/search), 'me'/'currentUser' (the authenticated account), or
    'none'/'unassign' (returns None → unassigns)."""
    w   = (who or "").strip()
    low = w.lower()
    if low in ("me", "currentuser", "currentuser()"):
        r = client.get("/myself")
        return r.json().get("accountId") if r.status_code == 200 else w
    if low in ("none", "unassigned", "unassign", "null", "-1"):
        return None
    if "@" in w or " " in w:   # email or display name → look up
        r = client.get("/user/search", params={"query": w})
        if r.status_code == 200 and r.json():
            return r.json()[0].get("accountId", w)
        # Looked like an email/name but matched no one — fail clearly instead of
        # sending the raw string as an accountId (which Jira rejects with 400).
        raise ValueError(f"no Jira user found matching '{w}' — use their exact "
                         "email or accountId")
    return w  # assume it's already an accountId


def _default_project(args: dict) -> str:
    project = args.get("project") or os.environ.get("JIRA_DEFAULT_PROJECT", "")
    if not project:
        raise ValueError("Provide 'project' or set JIRA_DEFAULT_PROJECT in ~/.aria/.env")
    return project


def _format_issue(issue: dict) -> str:
    """Format a Jira issue dict into a readable summary."""
    f       = issue.get("fields", {})
    key     = issue.get("key", "?")
    summary = f.get("summary", "")
    status  = f.get("status", {}).get("name", "")
    itype   = f.get("issuetype", {}).get("name", "")
    prio    = f.get("priority", {}).get("name", "")
    asgn    = (f.get("assignee") or {}).get("displayName", "Unassigned")
    url     = f"{os.environ.get('JIRA_BASE_URL', '').rstrip('/')}/browse/{key}"
    return f"[{key}] {summary}\n  Type: {itype} | Status: {status} | Priority: {prio} | Assignee: {asgn}\n  URL: {url}"


def execute(args: dict) -> str:
    try:
        return _execute(args)
    except Exception as exc:
        return f"[jira error] {exc}"


def _execute(args: dict) -> str:
    action = args["action"]

    with _client() as client:

        # ── List projects ─────────────────────────────────────────────────────
        if action == "list_projects":
            r = client.get("/project/search", params={"maxResults": 50})
            _check(r)
            projects = r.json().get("values", [])
            if not projects:
                return "No projects found."
            lines = [f"{p['key']:12} {p['name']}" for p in projects]
            return "\n".join(lines)

        # ── Get issue ─────────────────────────────────────────────────────────
        if action == "get":
            key = args.get("issue_key", "")
            if not key:
                return "[jira] 'issue_key' is required for get."
            r = client.get(f"/issue/{key}")
            _check(r)
            issue = r.json()
            f = issue.get("fields", {})
            desc  = (f.get("description") or {})
            # Extract plain text from Atlassian Document Format if present
            body  = _adf_to_text(desc) if isinstance(desc, dict) else (desc or "")
            base  = _format_issue(issue)
            return f"{base}\n\n{body}" if body else base

        # ── Search ────────────────────────────────────────────────────────────
        if action == "search":
            jql = args.get("jql", "")
            if not jql:
                return "[jira] 'jql' is required for search."
            n = int(args.get("max_results", 10))
            # Jira Cloud removed GET/POST /rest/api/3/search (deadline 2025-05-01).
            # The replacement /search/jql uses token pagination and returns no
            # `total`; counts come from the separate /search/approximate-count.
            r = client.get("/search/jql",
                           params={"jql": jql, "maxResults": n,
                                   "fields": "summary,status,issuetype,priority,assignee"})
            _check(r)
            data   = r.json()
            issues = data.get("issues", [])
            if not issues:
                return "No issues found."
            # Best-effort total via approximate-count — degrade gracefully.
            total = None
            try:
                cr = client.post("/search/approximate-count", json={"jql": jql})
                if cr.status_code == 200:
                    total = cr.json().get("count")
            except Exception:
                pass
            lines = [_format_issue(i) for i in issues]
            more  = bool(data.get("nextPageToken")) and not data.get("isLast", False)
            if total is not None:
                header = f"~{total} matching, showing {len(issues)}:\n"
            else:
                hint = " (more available — narrow the JQL or raise max_results)" if more else ""
                header = f"Showing {len(issues)} issue(s){hint}:\n"
            return header + "\n\n".join(lines)

        # ── Create ────────────────────────────────────────────────────────────
        if action == "create":
            summary = args.get("summary", "")
            if not summary:
                return "[jira] 'summary' is required for create."
            project    = _default_project(args)
            issue_type = args.get("issue_type", "Task")
            payload: dict = {
                "fields": {
                    "project":   {"key": project},
                    "summary":   summary,
                    "issuetype": {"name": issue_type},
                }
            }
            desc = args.get("description", "")
            if desc:
                # Jira Cloud uses Atlassian Document Format
                payload["fields"]["description"] = _text_to_adf(desc)
            if args.get("priority"):
                payload["fields"]["priority"] = {"name": args["priority"]}
            if args.get("labels"):
                payload["fields"]["labels"] = args["labels"]
            if args.get("components"):
                payload["fields"]["components"] = [{"name": c} for c in args["components"]]
            if args.get("assignee"):
                payload["fields"]["assignee"] = {
                    "accountId": _resolve_account_id(client, args["assignee"])
                }

            r = client.post("/issue", json=payload)
            _check(r)
            data    = r.json()
            key     = data.get("key", "?")
            base_url = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
            return f"Created {key}\n  URL: {base_url}/browse/{key}"

        # ── Update ────────────────────────────────────────────────────────────
        if action == "update":
            key = args.get("issue_key", "")
            if not key:
                return "[jira] 'issue_key' is required for update."
            fields: dict = {}
            if args.get("summary"):
                fields["summary"] = args["summary"]
            if args.get("description"):
                fields["description"] = _text_to_adf(args["description"])
            if args.get("priority"):
                fields["priority"] = {"name": args["priority"]}
            if args.get("labels"):
                fields["labels"] = args["labels"]
            if args.get("components"):
                fields["components"] = [{"name": c} for c in args["components"]]
            if not fields:
                return ("[jira] nothing to update — provide one of: summary, "
                        "description, priority, labels, components.")
            r = client.put(f"/issue/{key}", json={"fields": fields})
            _check(r)
            return f"Updated {key} ({', '.join(fields)})."

        # ── Comment ───────────────────────────────────────────────────────────
        if action == "comment":
            key  = args.get("issue_key", "")
            body = args.get("comment_body", "")
            if not key or not body:
                return "[jira] 'issue_key' and 'comment_body' are required for comment."
            payload = {"body": _text_to_adf(body)}
            r = client.post(f"/issue/{key}/comment", json=payload)
            _check(r)
            return f"Comment added to {key}."

        # ── Transition ────────────────────────────────────────────────────────
        if action == "transition":
            key   = args.get("issue_key", "")
            tname = args.get("transition_name", "")
            if not key or not tname:
                return "[jira] 'issue_key' and 'transition_name' are required."
            # Fetch available transitions
            r = client.get(f"/issue/{key}/transitions")
            _check(r)
            transitions = r.json().get("transitions", [])
            match = next(
                (t for t in transitions if t["name"].lower() == tname.lower()),
                None,
            )
            if not match:
                names = ", ".join(t["name"] for t in transitions)
                return f"[jira] Transition '{tname}' not found. Available: {names}"
            r = client.post(f"/issue/{key}/transitions",
                            json={"transition": {"id": match["id"]}})
            _check(r)
            return f"{key} transitioned to '{match['name']}'."

        # ── Assign ────────────────────────────────────────────────────────────
        if action == "assign":
            key      = args.get("issue_key", "")
            assignee = args.get("assignee", "")
            if not key or not assignee:
                return "[jira] 'issue_key' and 'assignee' are required for assign."
            account_id = _resolve_account_id(client, assignee)
            r = client.put(f"/issue/{key}/assignee",
                           json={"accountId": account_id})
            _check(r)
            who = "Unassigned" if account_id is None else assignee
            return f"{key} assigned to {who}."

    return f"[jira] Unknown action: {action}"


# ── Atlassian Document Format helpers ────────────────────────────────────────

def _text_to_adf(text: str) -> dict:
    """Convert plain text to minimal Atlassian Document Format (ADF)."""
    paragraphs = []
    for para in text.strip().split("\n\n"):
        lines = para.strip().splitlines()
        content = []
        for i, line in enumerate(lines):
            content.append({"type": "text", "text": line})
            if i < len(lines) - 1:
                content.append({"type": "hardBreak"})
        if content:
            paragraphs.append({"type": "paragraph", "content": content})
    return {
        "type": "doc",
        "version": 1,
        "content": paragraphs or [{"type": "paragraph", "content": []}],
    }


def _adf_to_text(adf: dict) -> str:
    """Extract plain text from an Atlassian Document Format blob."""
    if not isinstance(adf, dict):
        return str(adf)
    parts: list[str] = []

    def walk(node: dict) -> None:
        t = node.get("type", "")
        if t == "text":
            parts.append(node.get("text", ""))
        elif t == "hardBreak":
            parts.append("\n")
        elif t in ("paragraph", "heading"):
            for child in node.get("content", []):
                walk(child)
            parts.append("\n")
        elif t == "bulletList":
            for item in node.get("content", []):
                parts.append("• ")
                for child in item.get("content", []):
                    walk(child)
        else:
            for child in node.get("content", []):
                walk(child)

    for block in adf.get("content", []):
        walk(block)

    return "".join(parts).strip()
