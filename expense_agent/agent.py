"""
ExpenseIQ — Core Agent Graph
Safety Gate (security_checkpoint) + Self-Correcting Loop (ReviewLoop) + Workflow
"""
import json
import re
from datetime import datetime
from dotenv import load_dotenv

from google.adk.agents import LlmAgent, LoopAgent
from google.adk.tools.tool_context import ToolContext
from google.adk.workflow import Workflow
from google.adk.events.event import Event, EventActions
from google.adk.agents.context import Context
from google.adk.apps.app import App

from expense_agent.security import redact_pii, detect_injection, compute_risk_score
from expense_agent.tools import lookup_expense_policy, check_review_quality

load_dotenv()

GEMINI_MODEL = "gemini-2.5-flash"
APPROVAL_THRESHOLD = 100.0
RISK_THRESHOLD = 0.80


# ─── Node 1: Parse incoming expense ──────────────────────────────────────────
def parse_expense(node_input: str) -> Event:
    """Parse raw JSON expense string into structured state."""
    try:
        expense = json.loads(node_input) if isinstance(node_input, str) else node_input
    except Exception:
        expense = {"raw": str(node_input), "amount": 0, "category": "unknown", "description": "", "date": datetime.utcnow().date().isoformat()}
    return Event(
        output=expense,
        actions=EventActions(state_delta={"expense": expense})
    )


# ─── Node 2: Safety Gate ─────────────────────────────────────────────────────
def security_checkpoint(ctx: Context, node_input=None) -> Event:
    """
    Safety Gate: PII redaction + injection detection + risk scoring.
    Routes: auto_approve | llm_review | high_risk
    """
    expense = dict(ctx.state.get("expense", {}))
    description = str(expense.get("description", ""))
    amount = float(expense.get("amount", 0))

    # Redact PII
    scrubbed_desc, redacted = redact_pii(description)
    expense["description"] = scrubbed_desc

    # Detect injection
    has_injection = detect_injection(scrubbed_desc)

    # Compute risk score
    risk_score = compute_risk_score(amount, has_injection, redacted)

    state_delta = {
        "expense": expense,
        "redacted_categories": redacted,
        "risk_score": risk_score,
        "security_alert": has_injection,
    }

    if risk_score >= RISK_THRESHOLD:
        return Event(
            output=expense,
            actions=EventActions(route="high_risk", state_delta=state_delta)
        )
    elif amount < APPROVAL_THRESHOLD:
        return Event(
            output=expense,
            actions=EventActions(route="auto_approve", state_delta=state_delta)
        )
    else:
        return Event(
            output=expense,
            actions=EventActions(route="llm_review", state_delta=state_delta)
        )


# ─── Node 3a: Auto-approve (deterministic, no LLM) ───────────────────────────
def auto_approve_node(ctx: Context, node_input=None) -> Event:
    """Fast-path approval for low-risk, low-amount expenses. No LLM call."""
    expense = ctx.state.get("expense", {})
    outcome = {
        "status": "AUTO_APPROVED",
        "reason": f"Amount ${float(expense.get('amount', 0)):.2f} is below ${APPROVAL_THRESHOLD} threshold. Auto-approved.",
        "expense": expense,
        "risk_score": ctx.state.get("risk_score", 0),
        "timestamp": datetime.utcnow().isoformat(),
    }
    return Event(
        output=outcome,
        actions=EventActions(state_delta={"outcome": outcome})
    )


# ─── Node 3b: High-risk escalation ───────────────────────────────────────────
def high_risk_node(ctx: Context, node_input=None) -> Event:
    """Security Gate bypass — LLM never sees injected payload."""
    expense = ctx.state.get("expense", {})
    alert = ctx.state.get("security_alert", False)
    redacted = ctx.state.get("redacted_categories", [])
    outcome = {
        "status": "ESCALATED",
        "reason": (
            "SECURITY ALERT: Prompt injection detected. LLM bypassed. Manual review required."
            if alert else
            f"Risk score {ctx.state.get('risk_score', 0):.2f} exceeds threshold {RISK_THRESHOLD}. Manual review required."
        ),
        "expense": expense,
        "risk_score": ctx.state.get("risk_score", 0),
        "security_alert": alert,
        "redacted_categories": redacted,
        "timestamp": datetime.utcnow().isoformat(),
    }
    return Event(
        output=outcome,
        actions=EventActions(state_delta={"outcome": outcome})
    )


# ─── LoopAgent: Self-Correcting Review ───────────────────────────────────────
llm_reviewer = LlmAgent(
    name="LLMReviewer",
    model=GEMINI_MODEL,
    instruction="""You are a business expense reviewer for ExpenseIQ.

Review this expense:
- Details: {expense}
- Risk Score: {risk_score}

Write ONE clear sentence that includes ALL THREE of:
1. The specific business purpose
2. The exact dollar amount  
3. Whether it is justified or not and why

Output ONLY the review sentence. No preamble.""",
    output_key="review_reason",
)

review_validator = LlmAgent(
    name="ReviewValidator",
    model=GEMINI_MODEL,
    include_contents="none",
    instruction="""You are validating an expense review for completeness.

Review reason to validate:
"{review_reason}"

Check ALL THREE criteria:
1. Does it mention a specific business purpose? (not just "business expense")
2. Does it mention the exact dollar amount?
3. Does it state whether the expense is justified and why?

If ALL THREE are present: call check_review_quality with 'PASS'
If ANY are missing: call check_review_quality with 'REVISE: [what is missing]'""",
    tools=[check_review_quality],
)

review_loop = LoopAgent(
    name="ReviewLoop",
    sub_agents=[llm_reviewer, review_validator],
    max_iterations=3,
)


# ─── Node 4: Record outcome ───────────────────────────────────────────────────
def record_outcome(ctx: Context, node_input=None) -> Event:
    """Consolidate final outcome and write to dashboard store."""
    expense = ctx.state.get("expense", {})
    review_reason = ctx.state.get("review_reason", "")
    risk_score = ctx.state.get("risk_score", 0)
    outcome = ctx.state.get("outcome", {})

    if not outcome:
        outcome = {
            "status": "APPROVED",
            "reason": review_reason,
            "expense": expense,
            "risk_score": risk_score,
            "timestamp": datetime.utcnow().isoformat(),
        }
    elif review_reason and not outcome.get("reason"):
        outcome["reason"] = review_reason

    try:
        from dashboard.store import record_expense
        record_expense(outcome)
    except Exception:
        pass

    return Event(
        output=outcome,
        actions=EventActions(state_delta={"outcome": outcome})
    )


# ─── Workflow Graph ───────────────────────────────────────────────────────────
root_agent = Workflow(
    name="expense_workflow",
    edges=[
        ("START", parse_expense, security_checkpoint),
        (security_checkpoint, {
            "auto_approve": auto_approve_node,
            "llm_review": review_loop,
            "high_risk": high_risk_node,
        }),
        (auto_approve_node, record_outcome),
        (review_loop, record_outcome),
        (high_risk_node, record_outcome),
    ],
)

app = App(root_agent=root_agent, name="expense_agent")