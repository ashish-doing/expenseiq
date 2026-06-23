"""
PreToolUse hook: blocks destructive commands before execution.
Reads tool call JSON from stdin, exits 1 to block, 0 to allow.
"""
import sys
import json

BLOCKED = [
    "rm -rf",
    "del /s /q C:\\",
    "format C:",
    "shutdown",
    "mkfs",
    "DROP TABLE",
    "> /dev/null",
]

def main():
    try:
        context = json.load(sys.stdin)
        command = context.get("tool_args", {}).get("CommandLine", "")
        for dangerous in BLOCKED:
            if dangerous.lower() in command.lower():
                print(f"BLOCKED: Dangerous command detected: {dangerous}", file=sys.stderr)
                sys.exit(1)
        print("APPROVED: Command validation passed.")
        sys.exit(0)
    except Exception as e:
        print(f"Validation error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()