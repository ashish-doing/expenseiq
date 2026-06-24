"""
Eval harness for ExpenseIQ security routing.

Loads 25 labeled synthetic expenses from eval_expenses.json and runs each
through the security_checkpoint logic (pure Python, no ADK agent required).

Reports:
  - Per-case pass/fail with diagnostic detail
  - Per-category (auto_approve / llm_review / high_risk) accuracy
  - Overall accuracy
  - CI assertion: accuracy >= 95%
  - Summary table printed to stdout

Run:
  uv run pytest tests/test_eval.py -v
  uv run pytest tests/test_eval.py -v -s        # see the summary table
"""

import json
import re
import sys
from pathlib import Path
from collections import defaultdict

import pytest

# ---------------------------------------------------------------------------
# Replicate security_checkpoint routing WITHOUT importing the full ADK agent.
# All logic comes directly from expense_agent/security.py functions.
# ---------------------------------------------------------------------------

# Add repo root to path so we can import expense_agent.security
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from expense_agent.security import (
    redact_pii,
    detect_injection,
    compute_risk_score,
)

EVAL_JSON = Path(__file__).parent / "eval_expenses.json"
ACCURACY_THRESHOLD = 0.95  # CI fails below this


# ---------------------------------------------------------------------------
# Routing logic (mirrors agent.py's security_checkpoint node exactly)
# ---------------------------------------------------------------------------

def security_checkpoint_route(expense: dict) -> str:
    """
    Returns 'high_risk' | 'llm_review' | 'auto_approve'.
    Implements the same branching used in the ADK agent node.
    """
    description = expense.get("description", "")
    amount = float(expense.get("amount", 0))

    _, redacted = redact_pii(description)
    has_injection = detect_injection(description)
    risk_score = compute_risk_score(amount, has_injection, redacted)

    if risk_score >= 0.80:
        return "high_risk"
    elif amount < 100:
        return "auto_approve"
    else:
        return "llm_review"


def _route_diagnostics(expense: dict) -> dict:
    """Return intermediate signals for failure reporting."""
    description = expense.get("description", "")
    amount = float(expense.get("amount", 0))
    _, redacted = redact_pii(description)
    has_injection = detect_injection(description)
    risk_score = compute_risk_score(amount, has_injection, redacted)
    return {
        "amount": amount,
        "has_injection": has_injection,
        "redacted_pii": redacted,
        "risk_score": round(risk_score, 4),
    }


# ---------------------------------------------------------------------------
# Load dataset once at module level
# ---------------------------------------------------------------------------

def _load_cases() -> list[dict]:
    assert EVAL_JSON.exists(), f"Eval dataset not found: {EVAL_JSON}"
    with EVAL_JSON.open() as f:
        return json.load(f)


ALL_CASES: list[dict] = _load_cases()


# ---------------------------------------------------------------------------
# Session-scoped results accumulator (populated during individual tests,
# read by the summary test that runs last)
# ---------------------------------------------------------------------------

_results: list[dict] = []


# ---------------------------------------------------------------------------
# Parametrised per-case tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("case", ALL_CASES, ids=[c["id"] for c in ALL_CASES])
def test_route_single_expense(case: dict) -> None:
    """Each expense routes to the expected destination."""
    expense = case["expense"]
    expected = case["expected_route"]
    case_id = case["id"]

    actual = security_checkpoint_route(expense)
    diag = _route_diagnostics(expense)

    # Accumulate for summary (idempotent — pytest may re-run in some modes)
    if not any(r["id"] == case_id for r in _results):
        _results.append(
            {
                "id": case_id,
                "expected": expected,
                "actual": actual,
                "passed": actual == expected,
                **diag,
            }
        )

    assert actual == expected, (
        f"\n[{case_id}] MISMATCH\n"
        f"  description : {expense['description'][:80]}\n"
        f"  expected    : {expected}\n"
        f"  actual      : {actual}\n"
        f"  diagnostics : amount={diag['amount']}  injection={diag['has_injection']}  "
        f"pii={diag['redacted_pii']}  score={diag['risk_score']}\n"
        f"  reason      : {case.get('reason', '')}"
    )


# ---------------------------------------------------------------------------
# CI gate: overall accuracy >= 95%
# ---------------------------------------------------------------------------

def test_overall_accuracy_gate() -> None:
    """
    Asserts overall routing accuracy >= 95%.
    This test MUST run after all parametrised cases; pytest collects
    alphabetically by default so 'test_overall_accuracy_gate' runs after
    'test_route_single_expense'.  If you see this fail before the individual
    tests, run with: pytest tests/test_eval.py -v  (default order is fine).
    """
    # Re-run all cases so this test is self-contained even if run in isolation
    local_results = []
    for case in ALL_CASES:
        actual = security_checkpoint_route(case["expense"])
        local_results.append(
            {
                "id": case["id"],
                "expected": case["expected_route"],
                "actual": actual,
                "passed": actual == case["expected_route"],
            }
        )

    total = len(local_results)
    passed = sum(r["passed"] for r in local_results)
    accuracy = passed / total

    failures = [r for r in local_results if not r["passed"]]
    failure_detail = "\n".join(
        f"  {r['id']}: expected={r['expected']}  actual={r['actual']}"
        for r in failures
    )

    assert accuracy >= ACCURACY_THRESHOLD, (
        f"\nRouting accuracy {accuracy:.1%} is below threshold {ACCURACY_THRESHOLD:.0%}.\n"
        f"Failed cases ({len(failures)}/{total}):\n{failure_detail}"
    )


# ---------------------------------------------------------------------------
# Per-category accuracy tests (individual CI signals per route class)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("category", ["auto_approve", "llm_review", "high_risk"])
def test_per_category_accuracy(category: str) -> None:
    """Each route category must achieve >= 85% accuracy independently."""
    CATEGORY_THRESHOLD = 0.85
    cat_cases = [c for c in ALL_CASES if c["expected_route"] == category]
    results = [
        security_checkpoint_route(c["expense"]) == c["expected_route"]
        for c in cat_cases
    ]
    accuracy = sum(results) / len(results)
    assert accuracy >= CATEGORY_THRESHOLD, (
        f"Category '{category}' accuracy {accuracy:.1%} below {CATEGORY_THRESHOLD:.0%}"
    )


# ---------------------------------------------------------------------------
# Summary table — printed via a dedicated test that always runs last
# ---------------------------------------------------------------------------

def test_zzz_print_summary(capsys) -> None:  # noqa: PT019
    """
    Prints the eval summary table.
    Named 'test_zzz_*' so pytest runs it last alphabetically.
    Always passes — the accuracy gate is in test_overall_accuracy_gate.
    """
    # Re-derive results cleanly
    rows = []
    for case in ALL_CASES:
        actual = security_checkpoint_route(case["expense"])
        diag = _route_diagnostics(case["expense"])
        rows.append(
            {
                "id": case["id"],
                "expected": case["expected_route"],
                "actual": actual,
                "passed": actual == case["expected_route"],
                **diag,
            }
        )

    total = len(rows)
    passed = sum(r["passed"] for r in rows)
    accuracy = passed / total

    # Per-category breakdown
    by_category: dict[str, list[bool]] = defaultdict(list)
    for r in rows:
        by_category[r["expected"]].append(r["passed"])

    col_w = {"id": 10, "expected": 13, "actual": 13, "amt": 9,
              "inj": 6, "pii": 16, "score": 7, "ok": 6}
    sep = "-" * 80

    with capsys.disabled():
        print(f"\n{'=' * 80}")
        print(f"  ExpenseIQ Eval Harness — Routing Accuracy Report")
        print(f"{'=' * 80}")
        print(
            f"  {'ID':<10}  {'EXPECTED':<13}  {'ACTUAL':<13}  "
            f"{'AMOUNT':>9}  {'INJ':<6}  {'PII':<16}  {'SCORE':>7}  {'OK'}"
        )
        print(f"  {sep}")

        for r in rows:
            ok_str = "✓" if r["passed"] else "✗ FAIL"
            pii_str = ",".join(r["redacted_pii"]) if r["redacted_pii"] else "—"
            print(
                f"  {r['id']:<10}  {r['expected']:<13}  {r['actual']:<13}  "
                f"{r['amount']:>9.2f}  {str(r['has_injection']):<6}  "
                f"{pii_str:<16}  {r['risk_score']:>7.4f}  {ok_str}"
            )

        print(f"  {sep}")
        print(f"\n  Per-category accuracy:")
        for cat in ["auto_approve", "llm_review", "high_risk"]:
            cat_results = by_category[cat]
            cat_acc = sum(cat_results) / len(cat_results)
            bar = "█" * int(cat_acc * 20) + "░" * (20 - int(cat_acc * 20))
            print(f"    {cat:<13}  [{bar}]  {sum(cat_results)}/{len(cat_results)}  ({cat_acc:.1%})")

        failures = [r for r in rows if not r["passed"]]
        if failures:
            print(f"\n  Failed cases ({len(failures)}):")
            for r in failures:
                print(f"    {r['id']}: expected={r['expected']!r}  actual={r['actual']!r}  "
                      f"score={r['risk_score']}")
        else:
            print(f"\n  No failures — all cases routed correctly.")

        status = "PASS" if accuracy >= ACCURACY_THRESHOLD else "FAIL"
        print(f"\n  Overall accuracy : {passed}/{total}  ({accuracy:.1%})  [{status}]")
        print(f"  CI threshold     : {ACCURACY_THRESHOLD:.0%}")
        print(f"{'=' * 80}\n")