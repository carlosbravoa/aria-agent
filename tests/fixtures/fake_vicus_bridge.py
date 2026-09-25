"""A stand-in for vicus/bridge.mjs speaking the same stdio protocol, for tests.

FAKE_VICUS_MESSAGES  JSON list of message events to emit after "ready"
FAKE_VICUS_LOG       file that receives every command received (JSON lines)
FAKE_VICUS_FATAL     if set, emit {"type":"fatal"} with this text and exit 3
"""
import json
import os
import sys

log = open(os.environ["FAKE_VICUS_LOG"], "a", buffering=1)
out = sys.stdout


def emit(obj):
    out.write(json.dumps(obj) + "\n")
    out.flush()


if os.environ.get("FAKE_VICUS_FATAL"):
    emit({"type": "fatal", "error": os.environ["FAKE_VICUS_FATAL"]})
    sys.exit(3)
emit({"type": "ready", "account": "aria@example.org", "tenant": "t", "deviceId": "cli-aria-test1234"})
for ev in json.loads(os.environ.get("FAKE_VICUS_MESSAGES", "[]")):
    emit({"type": "message", **ev})
for line in sys.stdin:
    cmd = json.loads(line)
    log.write(json.dumps(cmd) + "\n")
    if cmd["type"] == "shutdown":
        break
    if cmd["type"] in ("send", "sendfile"):
        emit({"type": "result", "reqId": cmd["reqId"], "ok": True, "seq": 99})
    elif cmd["type"] == "notify":
        emit({"type": "result", "reqId": cmd["reqId"], "ok": bool(cmd.get("accounts"))})
