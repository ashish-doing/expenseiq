"""
Dashboard API routes for ExpenseIQ CRM.
"""
from fastapi import APIRouter
from dashboard.store import get_stats, EXPENSE_STORE

router = APIRouter()


@router.get("/api/stats")
async def get_dashboard_stats():
    """Return aggregated expense statistics for dashboard charts."""
    return get_stats()


@router.get("/api/expenses")
async def get_recent_expenses():
    """Return the 10 most recent expense records."""
    recent = sorted(EXPENSE_STORE, key=lambda x: x.get("timestamp", ""), reverse=True)[:10]
    return {"expenses": recent, "total": len(EXPENSE_STORE)}