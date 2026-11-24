#!/usr/bin/env bash
# Claude Code SessionStart. Pass the host session identity to subsequent Bash calls.
set -euo pipefail
[ -n "${CLAUDE_ENV_FILE:-}" ] || exit 0
python3 -B -c '
import json
import re
import sys

event = json.load(sys.stdin)
session = event.get("session_id")
if event.get("hook_event_name") != "SessionStart" or not isinstance(session, str) or not re.fullmatch(
        r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", session):
    sys.exit("Pipeline progress: missing verified Claude Code session identity")
with open(sys.argv[1], "a", encoding="utf-8") as env:
    env.write(f"export PIPELINE_CLAUDE_SESSION_ID={session}\n")
' "$CLAUDE_ENV_FILE"
