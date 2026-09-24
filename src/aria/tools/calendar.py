"""
aria/tools/calendar.py — Google Calendar via the gogcli (gog) CLI.

Correct command syntax (gogcli / steipete):
  gog calendar events <calendarId> --from <RFC3339> --to <RFC3339> [--json]
  gog calendar create <calendarId> --summary "..." --from <RFC3339> --to <RFC3339>
  gog calendar get    <calendarId> <eventId> [--json]
  gog calendar update <calendarId> <eventId> --summary "..." --from ... --to ...
  gog calendar delete <calendarId> <eventId>
  gog calendar respond <calendarId> <eventId> --status accepted|declined|tentative

Date format: RFC3339 — e.g. 2026-04-10T14:00:00 (local) or 2026-04-10T14:00:00Z (UTC)
CalendarId:  use "primary" for the main calendar.

Required env var in ~/.aria/.env:
  GOG_ACCOUNT=you@gmail.com
  GMAIL_CLI=gog   (also used for calendar — same binary)

Docs: https://github.com/steipete/gogcli
"""

from __future__ import annotations

import os
import shlex

from aria.tools import _gog

_CLI = os.getenv("GMAIL_CLI", "gog")

# Independent gog subprocesses (shared keyring is read-only) — a batch may run
# concurrently.
PARALLEL_SAFE = True

DEFINITION = {
    "name": "calendar",
    "description": (
        "Read and write Google Calendar events via the gog CLI. "
        "Actions: list (events in a date range), get (single event), "
        "create, update, delete, respond (accept/decline/tentative)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "get", "create", "update", "delete", "respond"],
                "description": "Calendar operation to perform.",
            },
            "calendar_id": {
                "type": "string",
                "description": 'Calendar ID. Use "primary" for the main calendar.',
                "default": "primary",
            },
            "event_id": {
                "type": "string",
                "description": "Event ID (required for get, update, delete, respond).",
            },
            "summary": {
                "type": "string",
                "description": "Event title (required for create; optional for update).",
            },
            "start": {
                "type": "string",
                "description": (
                    "Start datetime in RFC3339 format, e.g. 2026-04-10T14:00:00. "
                    "Required for create; also used as range start for list."
                ),
            },
            "end": {
                "type": "string",
                "description": (
                    "End datetime in RFC3339 format, e.g. 2026-04-10T15:00:00. "
                    "Required for create; also used as range end for list."
                ),
            },
            "description": {
                "type": "string",
                "description": "Event description / notes (optional).",
            },
            "attendees": {
                "type": "string",
                "description": "Comma-separated list of attendee emails (optional).",
            },
            "location": {
                "type": "string",
                "description": "Event location (optional).",
            },
            "status": {
                "type": "string",
                "enum": ["accepted", "declined", "tentative"],
                "description": "RSVP status for respond action.",
            },
            "days": {
                "type": "integer",
                "description": "Number of days to list events for (default 7). Used when start/end not provided.",
                "default": 7,
            },
        },
        "required": ["action"],
    },
}


def _run(cmd: str) -> str:
    return _gog.run(
        cmd,
        tag="calendar",
        no_account=("[calendar error] GOG_ACCOUNT is not set. "
                    "Add GOG_ACCOUNT=you@gmail.com to ~/.aria/.env"),
        not_found=(f"[calendar error] '{_CLI}' not found in PATH. "
                   "Ensure gog is installed and GMAIL_CLI is set in ~/.aria/.env"),
    )


def execute(args: dict) -> str:
    action      = args["action"]
    cal_id      = shlex.quote(args.get("calendar_id", "primary"))
    event_id    = args.get("event_id", "")
    summary     = args.get("summary", "")
    start       = args.get("start", "")
    end         = args.get("end", "")
    description = args.get("description", "")
    attendees   = args.get("attendees", "")
    location    = args.get("location", "")
    status      = args.get("status", "")
    try:
        days    = max(1, int(args.get("days", 7)))
    except (TypeError, ValueError):
        days    = 7

    match action:

        case "list":
            cmd = f"{_CLI} calendar events {cal_id}"
            if start:
                cmd += f" --from {shlex.quote(start)}"
            if end:
                cmd += f" --to {shlex.quote(end)}"
            if not start and not end:
                cmd += f" --days {days}"
            return _run(cmd)

        case "get":
            if not event_id:
                return "[calendar] 'event_id' is required for get."
            return _run(f"{_CLI} calendar get {cal_id} {shlex.quote(event_id)} --json")

        case "create":
            if not summary:
                return "[calendar] 'summary' is required for create."
            if not start or not end:
                return "[calendar] 'start' and 'end' are required for create."
            cmd = (
                f"{_CLI} calendar create {cal_id}"
                f" --summary {shlex.quote(summary)}"
                f" --from {shlex.quote(start)}"
                f" --to {shlex.quote(end)}"
            )
            if description:
                cmd += f" --description {shlex.quote(description)}"
            if attendees:
                cmd += f" --attendees {shlex.quote(attendees)}"
            if location:
                cmd += f" --location {shlex.quote(location)}"
            return _run(cmd)

        case "update":
            if not event_id:
                return "[calendar] 'event_id' is required for update."
            cmd = f"{_CLI} calendar update {cal_id} {shlex.quote(event_id)}"
            if summary:
                cmd += f" --summary {shlex.quote(summary)}"
            if start:
                cmd += f" --from {shlex.quote(start)}"
            if end:
                cmd += f" --to {shlex.quote(end)}"
            if description:
                cmd += f" --description {shlex.quote(description)}"
            if location:
                cmd += f" --location {shlex.quote(location)}"
            if attendees:
                cmd += f" --attendees {shlex.quote(attendees)}"
            return _run(cmd)

        case "delete":
            if not event_id:
                return "[calendar] 'event_id' is required for delete."
            return _run(f"{_CLI} calendar delete {cal_id} {shlex.quote(event_id)}")

        case "respond":
            if not event_id:
                return "[calendar] 'event_id' is required for respond."
            if not status:
                return "[calendar] 'status' (accepted|declined|tentative) is required for respond."
            return _run(
                f"{_CLI} calendar respond {cal_id} {shlex.quote(event_id)}"
                f" --status {shlex.quote(status)}"
            )

        case _:
            return f"[calendar] Unknown action: {action}"
