"""
ExpenseIQ ADK Tools
===================
lookup_expense_policy — MCP-pattern policy lookup tool.
check_review_quality  — LoopAgent escalation signal tool.
 
MCP Integration boundary
------------------------
`lookup_expense_policy` is an MCP-compatible tool that follows the Model Context
Protocol tool-call contract: it accepts a natural-language question and returns
authoritative policy text.  The LLMReviewer sub-agent calls it on EVERY review
so the tool call is always visible in agent traces.
 
Current implementation: deterministic local knowledge base (no external network
call) so the demo works without credentials and is fully reproducible.
 
Production roadmap: swap the function body for a single MCP client call to the
Google Developer Knowledge MCP server (`@google/developer-knowledge`).  The tool
signature, agent instruction, and call site in LLMReviewer are unchanged — only
the data source switches.  This is the clean MCP integration point.
"""

from google.adk.tools.tool_context import ToolContext


def lookup_expense_policy(question: str, tool_context: ToolContext) -> str:
    """
    Look up company expense policy or IRS guidelines for a given question.
    Use this to validate whether an expense category and amount are policy-compliant.
    
    Args:
        question: The policy question to look up, e.g. 
                 "What is the IRS per diem rate for meals?"
    """
    # Policy knowledge base — in production this calls MCP google-developer-knowledge
    # For local dev, deterministic policy rules are returned
    question_lower = question.lower()

    if "meal" in question_lower or "food" in question_lower or "lunch" in question_lower:
        return (
            "IRS per diem meal rate: $69/day domestic, $74/day international. "
            "Team meals require business purpose documentation and attendee list. "
            "Alcohol is non-reimbursable unless client entertainment with manager approval."
        )
    if "travel" in question_lower or "flight" in question_lower or "hotel" in question_lower:
        return (
            "Travel policy: Economy class for flights under 6 hours. "
            "Hotel rate cap: $250/night domestic, $350/night international. "
            "Requires manager pre-approval for trips over $500."
        )
    if "software" in question_lower or "saas" in question_lower or "license" in question_lower:
        return (
            "Software/SaaS policy: Individual licenses under $500 auto-approved. "
            "Team licenses $500-$2000 require manager approval. "
            "Enterprise licenses over $2000 require VP approval and IT security review."
        )
    if "hardware" in question_lower or "laptop" in question_lower or "equipment" in question_lower:
        return (
            "Hardware policy: Standard laptop budget $1500-$2500 for engineers. "
            "Peripherals under $200 auto-approved. "
            "All hardware must be registered with IT within 5 business days."
        )
    if "training" in question_lower or "course" in question_lower or "conference" in question_lower:
        return (
            "Training policy: Online courses under $500 auto-approved with manager notification. "
            "Conferences require pre-approval: registration + travel estimated cost. "
            "Annual training budget per employee: $2000."
        )

    return (
        "General expense policy: All expenses must have a clear business purpose. "
        "Receipts required for expenses over $25. "
        "Submit within 30 days of expense date. "
        "Personal expenses are never reimbursable."
    )


def check_review_quality(decision: str, tool_context: ToolContext) -> str:
    """
    Signal loop completion during expense review.
    Call with 'PASS' if review reason is complete, 'REVISE' if it needs improvement.
    
    Args:
        decision: Either 'PASS' or 'REVISE'
    """
    if decision.strip().upper() == "PASS":
        tool_context.actions.escalate = True
        return "Review quality confirmed. Escalating to approval."
    return f"Review needs improvement. Reason: {decision}. Please revise."