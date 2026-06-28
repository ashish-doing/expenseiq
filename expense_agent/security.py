"""
Security utilities: PII redaction + injection detection + risk scoring.
Standalone module — reusable in tests and agent nodes.
"""
import re

SSN_REGEX = re.compile(r"\b\d{3}-\d{2}-\d{4}\b|\b\d{9}\b")
CC_REGEX = re.compile(r"\b(?:\d[ -]*?){13,16}\b")

INJECTION_PATTERNS = [
    r"ignore\s+previous",
    r"system\s+override",
    r"bypass\s+the\s+rules",
    r"approve\s+this\s+instantly",
    r"you\s+must\s+approve",
    r"ignore\s+instructions",
    r"new\s+instructions",
    r"don't\s+review",
    r"skip\s+the\s+review",
    r"override\s+instructions",
]

ALLOWED_CATEGORIES = [
    "meals", "travel", "software",
    "hardware", "training", "office"
]


def redact_pii(text: str) -> tuple[str, list[str]]:
    """Redact SSN and credit card numbers. Returns (scrubbed_text, redacted_list)."""
    redacted = []
    if SSN_REGEX.search(text):
        text = SSN_REGEX.sub("[REDACTED SSN]", text)
        redacted.append("SSN")
    if CC_REGEX.search(text):
        text = CC_REGEX.sub("[REDACTED CC]", text)
        redacted.append("Credit Card")
    return text, redacted


def detect_injection(text: str) -> bool:
    """Return True if prompt injection patterns are found."""
    return any(re.search(p, text.lower()) for p in INJECTION_PATTERNS)


def compute_risk_score(amount: float, has_injection: bool, redacted: list) -> float:
    """
    Compute risk score 0.0-1.0.
    - Injection detected = 1.0 (maximum)
    - PII found = +0.3
    - Amount >= 1000 = 0.75
    - Amount >= 100 = 0.4
    - Below 100 = 0.1
    """
    if has_injection:
        return 1.0
    score = 0.1
    if redacted:
        score += 0.3
    if amount >= 1000:
        score = max(score, 0.75)
    elif amount >= 100:
        score = max(score, 0.4)
    return min(score, 1.0)


def validate_expense_fields(expense: dict) -> tuple[bool, str]:
    """Deterministic field validation. Returns (is_valid, error_message)."""
    from datetime import datetime
    if not expense.get("amount") or float(expense.get("amount", 0)) <= 0:
        return False, "Amount must be greater than 0"
    if expense.get("category", "").lower() not in ALLOWED_CATEGORIES:
        return False, f"Category must be one of: {', '.join(ALLOWED_CATEGORIES)}"
    if not expense.get("description", "").strip():
        return False, "Description cannot be empty"

    # Email format check — matches SKILL.md validation rule
    submitter = expense.get("submitter", "")
    if submitter and "@" not in submitter:
        return False, "Submitter must be a valid email address (e.g. alice@corp.com)."

    try:
        datetime.fromisoformat(str(expense.get("date", "")))
    except ValueError:
        return False, "Date must be valid ISO format (YYYY-MM-DD)"
    return True, ""