from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Any

from dashboard.store import (
    get_stats, get_all_expenses, get_pending,
    approve_pending, reject_pending
)

router = APIRouter()


class RejectRequest(BaseModel):
    reason: str = ""


@router.get("/api/stats")
def stats() -> dict[str, Any]:
    return get_stats()


@router.get("/api/expenses")
def list_expenses() -> list[dict[str, Any]]:
    return get_all_expenses()


@router.get("/api/pending")
def pending() -> list[dict[str, Any]]:
    return get_pending()


@router.post("/api/approve/{expense_id}")
def approve_expense(expense_id: str) -> dict[str, Any]:
    record = approve_pending(expense_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"No pending expense with id={expense_id!r}")
    return {"ok": True, "expense_id": expense_id, "status": "APPROVED"}


@router.post("/api/reject/{expense_id}")
def reject_expense(expense_id: str, body: RejectRequest) -> dict[str, Any]:
    record = reject_pending(expense_id, body.reason)
    if record is None:
        raise HTTPException(status_code=404, detail=f"No pending expense with id={expense_id!r}")
    return {"ok": True, "expense_id": expense_id, "status": "REJECTED", "reason": body.reason}