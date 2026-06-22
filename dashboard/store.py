"""
In-memory expense store with seed data for dashboard.
"""
from datetime import datetime, timedelta
from typing import Any
import random

# ─── Seed data so dashboard looks populated immediately ───────────────────────
_seed_date = datetime.utcnow()

EXPENSE_STORE: list[dict] = [
    {
        "expense": {"amount": 85.0, "category": "meals", "submitter": "alice@corp.com", "description": "Team lunch Q2 planning", "date": (_seed_date - timedelta(days=30)).date().isoformat()},
        "status": "AUTO_APPROVED", "risk_score": 0.1, "review_reason": None,
        "timestamp": (_seed_date - timedelta(days=30)).isoformat(),
    },
    {
        "expense": {"amount": 1200.0, "category": "travel", "submitter": "bob@corp.com", "description": "Flight to AWS re:Invent conference", "date": (_seed_date - timedelta(days=25)).date().isoformat()},
        "status": "APPROVED", "risk_score": 0.75, "review_reason": "Approved: $1200 travel for AWS re:Invent aligns with cloud strategy goals.",
        "timestamp": (_seed_date - timedelta(days=25)).isoformat(),
    },
    {
        "expense": {"amount": 450.0, "category": "software", "submitter": "carol@corp.com", "description": "Annual JetBrains IDE license", "date": (_seed_date - timedelta(days=20)).date().isoformat()},
        "status": "APPROVED", "risk_score": 0.4, "review_reason": "Approved: $450 JetBrains license is standard dev tooling with clear productivity ROI.",
        "timestamp": (_seed_date - timedelta(days=20)).isoformat(),
    },
    {
        "expense": {"amount": 50.0, "category": "office", "submitter": "dave@corp.com", "description": "Desk supplies and printer paper", "date": (_seed_date - timedelta(days=15)).date().isoformat()},
        "status": "AUTO_APPROVED", "risk_score": 0.1, "review_reason": None,
        "timestamp": (_seed_date - timedelta(days=15)).isoformat(),
    },
    {
        "expense": {"amount": 2500.0, "category": "hardware", "submitter": "eve@corp.com", "description": "MacBook Pro for new hire onboarding", "date": (_seed_date - timedelta(days=10)).date().isoformat()},
        "status": "ESCALATED", "risk_score": 0.75, "review_reason": None,
        "timestamp": (_seed_date - timedelta(days=10)).isoformat(),
    },
    {
        "expense": {"amount": 300.0, "category": "training", "submitter": "frank@corp.com", "description": "Udemy business subscription Q3", "date": (_seed_date - timedelta(days=5)).date().isoformat()},
        "status": "APPROVED", "risk_score": 0.4, "review_reason": "Approved: $300 Udemy subscription supports team upskilling initiative.",
        "timestamp": (_seed_date - timedelta(days=5)).isoformat(),
    },
]


def record_expense(outcome: dict) -> None:
    """Add a processed expense outcome to the store."""
    outcome["timestamp"] = datetime.utcnow().isoformat()
    EXPENSE_STORE.append(outcome)


def get_stats() -> dict:
    """Compute dashboard stats from all stored expenses."""
    total = len(EXPENSE_STORE)
    approved = sum(1 for e in EXPENSE_STORE if e.get("status") in ("APPROVED", "AUTO_APPROVED"))
    rejected = sum(1 for e in EXPENSE_STORE if e.get("status") == "REJECTED")
    escalated = sum(1 for e in EXPENSE_STORE if e.get("status") == "ESCALATED")

    by_category: dict[str, float] = {}
    by_month: dict[str, float] = {}

    for entry in EXPENSE_STORE:
        exp = entry.get("expense", {})
        cat = exp.get("category", "other")
        amount = float(exp.get("amount", 0))
        by_category[cat] = by_category.get(cat, 0) + amount

        ts = entry.get("timestamp", "")
        month = ts[:7] if ts else "unknown"
        by_month[month] = by_month.get(month, 0) + amount

    risk_scores = [e.get("risk_score", 0) for e in EXPENSE_STORE]
    avg_risk = sum(risk_scores) / len(risk_scores) if risk_scores else 0

    recent = sorted(EXPENSE_STORE, key=lambda x: x.get("timestamp", ""), reverse=True)[:10]

    return {
        "total_submitted": total,
        "total_approved": approved,
        "total_rejected": rejected,
        "total_escalated": escalated,
        "approval_rate": round((approved / total * 100) if total else 0, 1),
        "by_category": by_category,
        "by_month": dict(sorted(by_month.items())),
        "avg_risk_score": round(avg_risk, 3),
        "recent_expenses": recent,
    }