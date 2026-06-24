from datetime import datetime, timedelta
from typing import Any

PENDING_APPROVALS: list[dict[str, Any]] = []

_seed_date = datetime.utcnow()

EXPENSE_STORE: list[dict[str, Any]] = [
    {"expense_id": "seed-001", "submitter": "alice@corp.com", "amount": 85.0, "category": "meals", "description": "Team lunch Q2 planning", "risk_score": 0.1, "security_alert": False, "status": "APPROVED", "created_at": (_seed_date - timedelta(days=30)).isoformat()},
    {"expense_id": "seed-002", "submitter": "bob@corp.com", "amount": 1200.0, "category": "travel", "description": "Flight to AWS re:Invent conference", "risk_score": 0.75, "security_alert": False, "status": "APPROVED", "created_at": (_seed_date - timedelta(days=25)).isoformat()},
    {"expense_id": "seed-003", "submitter": "carol@corp.com", "amount": 450.0, "category": "software", "description": "Annual JetBrains IDE license", "risk_score": 0.4, "security_alert": False, "status": "APPROVED", "created_at": (_seed_date - timedelta(days=20)).isoformat()},
    {"expense_id": "seed-004", "submitter": "dave@corp.com", "amount": 50.0, "category": "office", "description": "Desk supplies and printer paper", "risk_score": 0.1, "security_alert": False, "status": "APPROVED", "created_at": (_seed_date - timedelta(days=15)).isoformat()},
    {"expense_id": "seed-005", "submitter": "eve@corp.com", "amount": 2500.0, "category": "hardware", "description": "MacBook Pro for new hire onboarding", "risk_score": 0.75, "security_alert": False, "status": "ESCALATED", "created_at": (_seed_date - timedelta(days=10)).isoformat()},
    {"expense_id": "seed-006", "submitter": "frank@corp.com", "amount": 300.0, "category": "training", "description": "Udemy business subscription Q3", "risk_score": 0.4, "security_alert": False, "status": "APPROVED", "created_at": (_seed_date - timedelta(days=5)).isoformat()},
]


def record_expense(outcome: dict) -> None:
    outcome["created_at"] = datetime.utcnow().isoformat()
    EXPENSE_STORE.append(outcome)


def get_stats() -> dict[str, Any]:
    total = len(EXPENSE_STORE)
    total_amount = sum(float(e.get("amount", 0)) for e in EXPENSE_STORE)
    approved = sum(1 for e in EXPENSE_STORE if e.get("status") in ("APPROVED", "AUTO_APPROVED"))
    rejected = sum(1 for e in EXPENSE_STORE if e.get("status") == "REJECTED")
    escalated = sum(1 for e in EXPENSE_STORE if e.get("status") == "ESCALATED")
    risk_scores = [float(e.get("risk_score", 0)) for e in EXPENSE_STORE]
    avg_risk = sum(risk_scores) / len(risk_scores) if risk_scores else 0.0
    by_category: dict[str, float] = {}
    for e in EXPENSE_STORE:
        cat = e.get("category", "other")
        by_category[cat] = by_category.get(cat, 0.0) + float(e.get("amount", 0))
    return {
        "total_expenses": total,
        "total_amount": round(total_amount, 2),
        "approved": approved,
        "rejected": rejected,
        "escalated": escalated,
        "pending": len(PENDING_APPROVALS),
        "avg_risk_score": round(avg_risk, 3),
        "by_category": by_category,
    }