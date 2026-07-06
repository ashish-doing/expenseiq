"""
ExpenseIQ — Persistent store backed by SQLite.
Replaces in-memory lists; survives Render cold starts.
Schema is flat to match dashboard JS expectations.
"""
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

DB_PATH = "expenseiq.db"


@contextmanager
def _conn():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def _init_db() -> None:
    with _conn() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS expenses (
            expense_id   TEXT PRIMARY KEY,
            submitter    TEXT,
            amount       REAL,
            category     TEXT,
            description  TEXT,
            risk_score   REAL DEFAULT 0,
            security_alert INTEGER DEFAULT 0,
            status       TEXT,
            reason       TEXT,
            created_at   TEXT,
            decided_at   TEXT,
            decision     TEXT,
            rejection_reason TEXT
        );

        CREATE TABLE IF NOT EXISTS pending_approvals (
            expense_id   TEXT PRIMARY KEY,
            submitter    TEXT,
            amount       REAL,
            category     TEXT,
            description  TEXT,
            risk_score   REAL DEFAULT 0,
            security_alert INTEGER DEFAULT 0,
            status       TEXT DEFAULT 'ESCALATED',
            reason       TEXT,
            created_at   TEXT
        );
        """)


def _seed() -> None:
    """Insert seed rows only if expenses table is empty."""
    with _conn() as con:
        count = con.execute("SELECT COUNT(*) FROM expenses").fetchone()[0]
        if count > 0:
            return
        seed_date = datetime.utcnow()
        rows = [
            ("seed-001", "alice@corp.com",   85.0,   "meals",    "Team lunch Q2 planning",               0.10, 0, "APPROVED",  seed_date - timedelta(days=30)),
            ("seed-002", "bob@corp.com",    1200.0,  "travel",   "Flight to AWS re:Invent conference",   0.75, 0, "APPROVED",  seed_date - timedelta(days=25)),
            ("seed-003", "carol@corp.com",   450.0,  "software", "Annual JetBrains IDE license",         0.40, 0, "APPROVED",  seed_date - timedelta(days=20)),
            ("seed-004", "dave@corp.com",     50.0,  "office",   "Desk supplies and printer paper",      0.10, 0, "AUTO_APPROVED", seed_date - timedelta(days=15)),
            ("seed-005", "eve@corp.com",    2500.0,  "hardware", "MacBook Pro for new hire onboarding",  0.75, 0, "ESCALATED", seed_date - timedelta(days=10)),
            ("seed-006", "frank@corp.com",   300.0,  "training", "Udemy business subscription Q3",       0.40, 0, "APPROVED",  seed_date - timedelta(days=5)),
        ]
        con.executemany(
            """INSERT OR IGNORE INTO expenses
               (expense_id, submitter, amount, category, description, risk_score, security_alert, status, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            [(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8].isoformat()) for r in rows]
        )


# ── Initialise on import ──────────────────────────────────────────────────────
_init_db()
_seed()


# ── Public API ────────────────────────────────────────────────────────────────

def _row_to_dict(row) -> dict[str, Any]:
    d = dict(row)
    d["security_alert"] = bool(d.get("security_alert", 0))
    return d


def record_expense(outcome: dict) -> None:
    """
    Write a finalised expense outcome to the persistent store.
    Accepts BOTH the old nested format {expense:{...}, status:...}
    and the flat format {submitter:..., amount:..., status:...}.
    Always writes flat rows so the dashboard JS never sees malformed data.
    """
    # Unwrap nested format produced by old record_outcome nodes
    inner = outcome.get("expense", {})

    expense_id = outcome.get("expense_id") or inner.get("expense_id") or str(uuid.uuid4())
    submitter  = outcome.get("submitter")  or inner.get("submitter", "unknown")
    amount     = float(outcome.get("amount")  or inner.get("amount", 0))
    category   = outcome.get("category")   or inner.get("category", "other")
    description = outcome.get("description") or inner.get("description", "")
    risk_score  = float(outcome.get("risk_score", 0))
    security_alert = int(bool(outcome.get("security_alert", False)))
    status      = outcome.get("status", "UNKNOWN")
    reason      = outcome.get("reason", "")
    created_at  = outcome.get("created_at") or datetime.utcnow().isoformat()

    with _conn() as con:
        con.execute(
            """INSERT OR REPLACE INTO expenses
               (expense_id, submitter, amount, category, description,
                risk_score, security_alert, status, reason, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (expense_id, submitter, amount, category, description,
             risk_score, security_alert, status, reason, created_at)
        )


def add_pending(record: dict) -> None:
    with _conn() as con:
        con.execute(
            """INSERT OR REPLACE INTO pending_approvals
               (expense_id, submitter, amount, category, description,
                risk_score, security_alert, status, reason, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                record["expense_id"],
                record.get("submitter", "unknown"),
                float(record.get("amount", 0)),
                record.get("category", "other"),
                record.get("description", ""),
                float(record.get("risk_score", 0)),
                int(bool(record.get("security_alert", False))),
                "ESCALATED",
                record.get("reason", ""),
                record.get("created_at") or datetime.utcnow().isoformat(),
            )
        )


def get_pending() -> list[dict[str, Any]]:
    with _conn() as con:
        rows = con.execute("SELECT * FROM pending_approvals ORDER BY created_at DESC").fetchall()
    return [_row_to_dict(r) for r in rows]


def approve_pending(expense_id: str) -> dict[str, Any] | None:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM pending_approvals WHERE expense_id=?", (expense_id,)
        ).fetchone()
        if row is None:
            return None
        record = _row_to_dict(row)
        decided_at = datetime.utcnow().isoformat()
        record["status"] = "APPROVED"
        record["decided_at"] = decided_at
        record["decision"] = "APPROVED"
        con.execute(
            """INSERT OR REPLACE INTO expenses
               (expense_id, submitter, amount, category, description,
                risk_score, security_alert, status, reason, created_at, decided_at, decision)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (record["expense_id"], record["submitter"], record["amount"],
             record["category"], record["description"], record["risk_score"],
             int(record["security_alert"]), "APPROVED", record.get("reason",""),
             record["created_at"], decided_at, "APPROVED")
        )
        con.execute("DELETE FROM pending_approvals WHERE expense_id=?", (expense_id,))
    return record


def reject_pending(expense_id: str, reason: str = "") -> dict[str, Any] | None:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM pending_approvals WHERE expense_id=?", (expense_id,)
        ).fetchone()
        if row is None:
            return None
        record = _row_to_dict(row)
        decided_at = datetime.utcnow().isoformat()
        con.execute(
            """INSERT OR REPLACE INTO expenses
               (expense_id, submitter, amount, category, description,
                risk_score, security_alert, status, reason, created_at, decided_at, decision, rejection_reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (record["expense_id"], record["submitter"], record["amount"],
             record["category"], record["description"], record["risk_score"],
             int(record["security_alert"]), "REJECTED", record.get("reason",""),
             record["created_at"], decided_at, "REJECTED", reason)
        )
        con.execute("DELETE FROM pending_approvals WHERE expense_id=?", (expense_id,))
    return record


def get_all_expenses() -> list[dict[str, Any]]:
    with _conn() as con:
        rows = con.execute("SELECT * FROM expenses ORDER BY created_at DESC").fetchall()
    return [_row_to_dict(r) for r in rows]


def get_stats() -> dict[str, Any]:
    expenses = get_all_expenses()
    pending_count = len(get_pending())
    total = len(expenses)
    total_amount = sum(
        float(e.get("amount", 0)) for e in expenses
        if e.get("status") in ("APPROVED", "AUTO_APPROVED")
    )
    approved = sum(1 for e in expenses if e.get("status") in ("APPROVED", "AUTO_APPROVED"))
    rejected = sum(1 for e in expenses if e.get("status") == "REJECTED")
    escalated = sum(1 for e in expenses if e.get("status") == "ESCALATED")
    risk_scores = [float(e.get("risk_score", 0)) for e in expenses]
    avg_risk = sum(risk_scores) / len(risk_scores) if risk_scores else 0.0
    by_category: dict[str, float] = {}
    for e in expenses:
        cat = e.get("category", "other")
        by_category[cat] = by_category.get(cat, 0.0) + float(e.get("amount", 0))
    return {
        "total_expenses": total,
        "total_amount": round(total_amount, 2),
        "approved": approved,
        "rejected": rejected,
        "escalated": escalated,
        "pending": pending_count,
        "avg_risk_score": round(avg_risk, 3),
        "by_category": by_category,
    }