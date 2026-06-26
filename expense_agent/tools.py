"""
ExpenseIQ ADK Tools
===================
lookup_expense_policy — PolicyAgent's MCP-pattern policy retrieval tool.
check_review_quality  — ReviewValidator's loop-exit signal tool.
budget_check          — BudgetAgent's dept spend tool (reads SQLite store).

MCP Integration boundary
------------------------
`lookup_expense_policy` follows the MCP tool-call contract: natural-language
question in, authoritative policy text out. PolicyAgent calls it on every
review so the tool call is always visible in agent traces.

Current implementation: deterministic local knowledge base (no external call)
so the demo works without credentials and is fully reproducible.

Production roadmap: swap the body for one MCP client call to the Google
Developer Knowledge MCP server (`@google/developer-knowledge`). The tool
signature, PolicyAgent instruction, and call site are unchanged — only the
data source switches. This is the clean MCP integration point.

BudgetAgent (budget_check)
--------------------------
Reads the live SQLite expense store to surface per-category spend in the
current month. LLMReviewer uses this to contextualise whether an expense
is within budget before writing its review. Pure Python, zero LLM calls.
"""

from google.adk.tools.tool_context import ToolContext


# ─── PolicyAgent tool ─────────────────────────────────────────────────────────

def lookup_expense_policy(question: str, tool_context: ToolContext) -> str:
    """
    Look up company expense policy or IRS guidelines for a given question.

    MCP tool — called by PolicyAgent on every LLM review path.
    Returns authoritative policy text for the category in `question`.

    In production: routes to Google Developer Knowledge MCP server.
    Locally: returns deterministic rules from built-in policy knowledge base.

    Args:
        question: Natural-language policy question, e.g.
                  "What is the policy for travel expenses over $500?"
    """
    # Local policy knowledge base
    # Production: replace body with → mcp_client.call("lookup_policy", question=question)
    q = question.lower()

    if any(k in q for k in ("meal", "food", "lunch", "dinner", "restaurant")):
        return (
            "IRS per diem meal rate: $69/day domestic, $74/day international. "
            "Team meals require business purpose documentation and attendee list. "
            "Alcohol is non-reimbursable unless client entertainment with manager approval."
        )
    if any(k in q for k in ("travel", "flight", "hotel", "accommodation", "airfare")):
        return (
            "Travel policy: Economy class for flights under 6 hours. "
            "Hotel rate cap: $250/night domestic, $350/night international. "
            "Requires manager pre-approval for trips over $500."
        )
    if any(k in q for k in ("software", "saas", "license", "subscription", "tool")):
        return (
            "Software/SaaS policy: Individual licenses under $500 auto-approved. "
            "Team licenses $500–$2000 require manager approval. "
            "Enterprise licenses over $2000 require VP approval and IT security review."
        )
    if any(k in q for k in ("hardware", "laptop", "equipment", "device", "monitor")):
        return (
            "Hardware policy: Standard laptop budget $1500–$2500 for engineers. "
            "Peripherals under $200 auto-approved. "
            "All hardware must be registered with IT within 5 business days."
        )
    if any(k in q for k in ("training", "course", "conference", "certification", "workshop")):
        return (
            "Training policy: Online courses under $500 auto-approved with manager notification. "
            "Conferences require pre-approval: registration + travel estimated cost. "
            "Annual training budget per employee: $2000."
        )
    if any(k in q for k in ("office", "supplies", "stationery", "furniture")):
        return (
            "Office supplies policy: Items under $100 auto-approved. "
            "$100–$500 requires team lead approval. "
            "Furniture and equipment over $500 requires facilities team approval."
        )

    return (
        "General expense policy: All expenses must have a clear business purpose. "
        "Receipts required for expenses over $25. "
        "Submit within 30 days of expense date. "
        "Personal expenses are never reimbursable."
    )


# ─── ReviewValidator tool ─────────────────────────────────────────────────────

def check_review_quality(decision: str, tool_context: ToolContext) -> str:
    """
    Signal loop exit from the self-correcting review cycle.

    Called by ReviewValidator after checking all three review criteria.
    'PASS'   → sets route='PASS'   → Workflow exits to record_outcome.
    'REVISE' → sets route='REVISE' → Workflow cycles back via iteration_guard.

    Args:
        decision: 'PASS' if review is complete, 'REVISE: [reason]' otherwise.
    """
    if decision.strip().upper().startswith("PASS"):
        tool_context.actions.escalate = True          # exits LoopAgent if still used
        tool_context.actions.route    = "PASS"        # Workflow cycle exit
        return "Review quality confirmed. Routing to approval."

    tool_context.actions.route = "REVISE"
    return f"Review needs improvement: {decision}. Cycling back to reviewer."


# ─── BudgetAgent tool ────────────────────────────────────────────────────────

def budget_check(category: str, amount: float) -> str:
    """
    Check current-month department spend for a given expense category.

    BudgetAgent tool — reads live SQLite expense store to surface budget context.
    LLMReviewer uses this to assess whether the expense fits within monthly budget
    before writing its review decision.

    Args:
        category: Expense category (meals, travel, software, hardware, training, office).
        amount:   Proposed expense amount in USD.

    Returns:
        Human-readable budget context string for LLMReviewer to cite.
    """
    MONTHLY_BUDGETS = {
        "meals":    500.0,
        "travel":  5000.0,
        "software": 2000.0,
        "hardware": 8000.0,
        "training": 2000.0,
        "office":   1000.0,
    }

    budget_limit = MONTHLY_BUDGETS.get(category.lower(), 1000.0)

    try:
        from dashboard.store import get_all_expenses
        from datetime import datetime, timezone

        now       = datetime.now(timezone.utc)
        month_str = now.strftime("%Y-%m")

        all_expenses = get_all_expenses()
        month_spend  = sum(
            float(e.get("amount", 0))
            for e in all_expenses
            if (
                e.get("category", "").lower() == category.lower()
                and e.get("status") in ("APPROVED", "AUTO_APPROVED")
                and str(e.get("created_at", "")).startswith(month_str)
            )
        )

        remaining   = budget_limit - month_spend
        after_spend = month_spend + amount
        utilisation = (after_spend / budget_limit * 100) if budget_limit else 0

        if remaining <= 0:
            status = "OVER BUDGET"
        elif utilisation > 90:
            status = "NEAR BUDGET LIMIT"
        elif utilisation > 70:
            status = "MODERATE USAGE"
        else:
            status = "WITHIN BUDGET"

        return (
            f"Category '{category}' monthly budget: ${budget_limit:,.0f}. "
            f"Spent this month: ${month_spend:,.2f}. "
            f"This expense (${amount:,.2f}) would bring total to ${after_spend:,.2f} "
            f"({utilisation:.1f}% of budget). Status: {status}."
        )

    except Exception as e:
        return (
            f"Budget check unavailable for '{category}' (${amount:.2f}): {e}. "
            f"Monthly limit reference: ${budget_limit:,.0f}."
        )