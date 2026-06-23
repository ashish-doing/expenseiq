"""
Level 4 Deterministic Skill: Expense field validator.
Exit 0 = valid, Exit 1 = invalid with error message.
Usage: python validate_expense.py '<json_string>'
"""
import sys
import json
import re
from datetime import datetime

ALLOWED_CATEGORIES = ["meals", "travel", "software", "hardware", "training", "office"]
EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def validate(expense: dict) -> list[str]:
    errors = []
    try:
        amount = float(expense.get("amount", 0))
        if amount <= 0:
            errors.append("amount must be greater than 0")
    except (TypeError, ValueError):
        errors.append("amount must be a valid number")

    category = str(expense.get("category", "")).lower()
    if category not in ALLOWED_CATEGORIES:
        errors.append(f"category '{category}' not allowed. Must be one of: {', '.join(ALLOWED_CATEGORIES)}")

    description = str(expense.get("description", "")).strip()
    if not description:
        errors.append("description cannot be empty")

    date_str = str(expense.get("date", ""))
    try:
        datetime.fromisoformat(date_str)
    except ValueError:
        errors.append(f"date '{date_str}' is not valid ISO format (YYYY-MM-DD)")

    submitter = str(expense.get("submitter", ""))
    if submitter and not EMAIL_REGEX.match(submitter):
        errors.append(f"submitter '{submitter}' is not a valid email address")

    return errors


def main():
    if len(sys.argv) < 2:
        print("Usage: validate_expense.py '<json_string>'", file=sys.stderr)
        sys.exit(1)

    try:
        expense = json.loads(sys.argv[1])
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    errors = validate(expense)
    if errors:
        for err in errors:
            print(f"ERROR: {err}")
        sys.exit(1)

    print("VALID: Expense passed all validation checks.")
    sys.exit(0)


if __name__ == "__main__":
    main()