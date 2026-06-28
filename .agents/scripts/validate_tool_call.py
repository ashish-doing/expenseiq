#!/usr/bin/env python3
"""
ExpenseIQ — Antigravity PreToolUse Hook
Validates tool calls before execution in the Antigravity vibe coding session.
Blocks destructive commands (rm -rf, git push --force, drop table, etc.)
Exit 0 = allow, Exit 1 = block
"""
import sys
import json
import re

BLOCKED_PATTERNS = [
    r"rm\s+-rf",
    r"git\s+push\s+--force",
    r"DROP\s+TABLE",
    r"DELETE\s+FROM.*WHERE\s+1=1",
    r"--no-verify",
    r"os\.environ\[.GEMINI_API_KEY.\]\s*=\s*['\"]AI",  # hardcoded key
]

def main():
    try:
        payload = json.loads(sys.stdin.read()) if not sys.stdin.isatty() else {}
    except Exception:
        payload = {}

    command = payload.get("command", "") or " ".join(sys.argv[1:])

    for pattern in BLOCKED_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            print(f"[BLOCKED] Destructive pattern detected: {pattern}", file=sys.stderr)
            sys.exit(1)

    sys.exit(0)

if __name__ == "__main__":
    main()