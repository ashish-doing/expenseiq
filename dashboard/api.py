from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from datetime import datetime
from typing import Any

from dashboard.store import EXPENSE_STORE, PENDING_APPROVALS, get_stats

router = APIRouter()


# ── Request schemas ───────────────────────────────────────────────────────────

class RejectRequest(BaseModel):
    reason: str = ""


# ── Stats / existing ──────────────────────────────────────────────────────────

@router.get("/api/stats")
def stats() -> dict[str, Any]:
    return get_stats()


@router.get("/api/expenses")
def list_expenses() -> list[dict[str, Any]]:
    return list(reversed(EXPENSE_STORE))


# ── HITL endpoints ────────────────────────────────────────────────────────────

@router.get("/api/pending")
def get_pending() -> list[dict[str, Any]]:
    """Return all expenses currently awaiting human approval."""
    return list(PENDING_APPROVALS)


@router.post("/api/approve/{expense_id}")
def approve_expense(expense_id: str) -> dict[str, Any]:
    """
    Approve a pending escalated expense.
    Moves it from PENDING_APPROVALS → EXPENSE_STORE with status APPROVED.
    """
    # Find in pending queue
    pending_item = next(
        (e for e in PENDING_APPROVALS if e.get("expense_id") == expense_id), None
    )
    if pending_item is None:
        raise HTTPException(status_code=404, detail=f"No pending expense with id={expense_id!r}")

    # Update status and record decision time
    pending_item = dict(pending_item)          # shallow copy — don't mutate in-place
    pending_item["status"] = "APPROVED"
    pending_item["decided_at"] = datetime.utcnow().isoformat()
    pending_item["decision"] = "APPROVED"

    # Add to main store
    EXPENSE_STORE.append(pending_item)

    # Remove from pending queue
    PENDING_APPROVALS[:] = [e for e in PENDING_APPROVALS if e.get("expense_id") != expense_id]

    return {"ok": True, "expense_id": expense_id, "status": "APPROVED"}


@router.post("/api/reject/{expense_id}")
def reject_expense(expense_id: str, body: RejectRequest) -> dict[str, Any]:
    """
    Reject a pending escalated expense with an optional reason.
    Moves it from PENDING_APPROVALS → EXPENSE_STORE with status REJECTED.
    """
    pending_item = next(
        (e for e in PENDING_APPROVALS if e.get("expense_id") == expense_id), None
    )
    if pending_item is None:
        raise HTTPException(status_code=404, detail=f"No pending expense with id={expense_id!r}")

    pending_item = dict(pending_item)
    pending_item["status"] = "REJECTED"
    pending_item["decided_at"] = datetime.utcnow().isoformat()
    pending_item["decision"] = "REJECTED"
    pending_item["rejection_reason"] = body.reason

    EXPENSE_STORE.append(pending_item)
    PENDING_APPROVALS[:] = [e for e in PENDING_APPROVALS if e.get("expense_id") != expense_id]

    return {"ok": True, "expense_id": expense_id, "status": "REJECTED", "reason": body.reason}