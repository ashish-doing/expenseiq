"""
ExpenseIQ Dashboard API
RBAC: approve/reject endpoints require X-Approver-Role: manager|finance|auditor
"""
from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel
from typing import Any, Annotated

from dashboard.store import (
    get_stats, get_all_expenses, get_pending,
    approve_pending, reject_pending
)

router = APIRouter()

# Roles allowed to approve or reject expenses
APPROVER_ROLES = {"manager", "finance", "auditor"}


class RejectRequest(BaseModel):
    reason: str = ""


def _require_approver(x_approver_role: str | None) -> str:
    """
    RBAC gate for HITL endpoints.
    Reads X-Approver-Role header; raises 403 if missing or unauthorised.
    Production: replace with JWT/OAuth2 role claim extraction.
    """
    if not x_approver_role:
        raise HTTPException(
            status_code=403,
            detail=(
                "Missing X-Approver-Role header. "
                f"Allowed roles: {sorted(APPROVER_ROLES)}."
            ),
        )
    role = x_approver_role.strip().lower()
    if role not in APPROVER_ROLES:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Role '{role}' is not authorised to approve/reject expenses. "
                f"Allowed roles: {sorted(APPROVER_ROLES)}."
            ),
        )
    return role


# ── Read endpoints (no auth required) ────────────────────────────────────────

@router.get("/api/stats")
def stats() -> dict[str, Any]:
    return get_stats()


@router.get("/api/expenses")
def list_expenses() -> list[dict[str, Any]]:
    return get_all_expenses()


@router.get("/api/pending")
def pending() -> list[dict[str, Any]]:
    return get_pending()


# ── HITL write endpoints (RBAC gated) ────────────────────────────────────────

@router.post("/api/approve/{expense_id}")
def approve_expense(
    expense_id: str,
    x_approver_role: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """
    Approve a pending escalated expense.
    Requires header: X-Approver-Role: manager | finance | auditor
    """
    role   = _require_approver(x_approver_role)
    record = approve_pending(expense_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"No pending expense with id={expense_id!r}",
        )
    return {
        "ok":         True,
        "expense_id": expense_id,
        "status":     "APPROVED",
        "approved_by_role": role,
    }


@router.post("/api/reject/{expense_id}")
def reject_expense(
    expense_id: str,
    body: RejectRequest,
    x_approver_role: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """
    Reject a pending escalated expense.
    Requires header: X-Approver-Role: manager | finance | auditor
    """
    role   = _require_approver(x_approver_role)
    record = reject_pending(expense_id, body.reason)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"No pending expense with id={expense_id!r}",
        )
    return {
        "ok":         True,
        "expense_id": expense_id,
        "status":     "REJECTED",
        "reason":     body.reason,
        "rejected_by_role": role,
    }