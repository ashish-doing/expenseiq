"""
ExpenseIQ — Core Agent Graph
Safety Gate (security_checkpoint) + Self-Correcting Loop (ReviewLoop) + Workflow
"""
import json
import uuid
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
    
    expense.setdefault("expense_id", str(uuid.uuid4()))
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
    risk_score = ctx.state.get("risk_score", 0)
    reason = f"Amount ${float(expense.get('amount', 0)):.2f} is below ${APPROVAL_THRESHOLD} threshold. Auto-approved."
    outcome = {
        "expense_id":    expense.get("expense_id") or str(uuid.uuid4()),
        "submitter":     expense.get("submitter", "unknown"),
        "amount":        float(expense.get("amount", 0)),
        "category":      expense.get("category", "other"),
        "description":   expense.get("description", ""),
        "risk_score":    round(float(risk_score), 3),
        "security_alert": False,
        "status":        "AUTO_APPROVED",
        "reason":        reason,
        "created_at":    datetime.utcnow().isoformat(),
    }
    return Event(output=outcome, actions=EventActions(state_delta={"outcome": outcome}))


# ─── Node 3b: High-risk escalation ───────────────────────────────────────────

def high_risk_node(ctx: Context, node_input=None) -> Event:
    """Security Gate bypass — LLM never sees injected payload."""
    expense = ctx.state.get("expense", {})
    alert = ctx.state.get("security_alert", False)
    redacted = ctx.state.get("redacted_categories", [])
    risk_score = ctx.state.get("risk_score", 0)
    reason = (
        "SECURITY ALERT: Prompt injection detected. LLM bypassed. Manual review required."
        if alert else
        f"Risk score {risk_score:.2f} exceeds threshold {RISK_THRESHOLD}. Manual review required."
    )
    outcome = {
        "expense_id":    expense.get("expense_id") or str(uuid.uuid4()),
        "submitter":     expense.get("submitter", "unknown"),
        "amount":        float(expense.get("amount", 0)),
        "category":      expense.get("category", "other"),
        "description":   expense.get("description", ""),
        "risk_score":    round(float(risk_score), 3),
        "security_alert": alert,
        "status":        "ESCALATED",
        "reason":        reason,
        "created_at":    datetime.utcnow().isoformat(),
        "redacted_categories": redacted,
    }
    return Event(output=outcome, actions=EventActions(state_delta={"outcome": outcome}))


# ─── LoopAgent: Self-Correcting Review ───────────────────────────────────────

llm_reviewer = LlmAgent(
    name="LLMReviewer",
    model=GEMINI_MODEL,
    instruction="""You are a business expense reviewer for ExpenseIQ.

Review this expense and call lookup_expense_policy to verify policy compliance:
- Details: {expense}
- Risk Score: {risk_score}

First call lookup_expense_policy with a question like "What is the policy for [category] expenses?"
Then write ONE clear sentence that includes ALL THREE of:
1. The specific business purpose
2. The exact dollar amount
3. Whether it is justified or not and why (citing the policy)

Output ONLY the review sentence. No preamble.""",
    tools=[lookup_expense_policy],
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
    """Consolidate final outcome and write flat record to dashboard store."""
    expense    = ctx.state.get("expense", {})
    review_reason = ctx.state.get("review_reason", "")
    risk_score = ctx.state.get("risk_score", 0)
    prior_outcome = ctx.state.get("outcome", {})

    if prior_outcome and prior_outcome.get("status"):
        outcome = dict(prior_outcome)
        if review_reason and not outcome.get("reason"):
            outcome["reason"] = review_reason
    else:
        outcome = {
            "expense_id":    expense.get("expense_id") or str(uuid.uuid4()),
            "submitter":     expense.get("submitter", "unknown"),
            "amount":        float(expense.get("amount", 0)),
            "category":      expense.get("category", "other"),
            "description":   expense.get("description", ""),
            "risk_score":    round(float(risk_score), 3),
            "security_alert": False,
            "status":        "APPROVED",
            "reason":        review_reason or "LLM review passed.",
            "created_at":    datetime.utcnow().isoformat(),
        }

    try:
        from dashboard.store import record_expense
        record_expense(outcome)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("record_expense failed: %s", e)

    return Event(output=outcome, actions=EventActions(state_delta={"outcome": outcome}))


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