"""
ExpenseIQ — Core Agent Graph
Safety Gate + Self-Correcting Workflow Cycle + Multi-Agent Review + HITL

Architecture:
  parse_expense → security_checkpoint
    → auto_approve_node          (amount < $100, risk < 0.80)
    → policy_agent               (amount ≥ $100, risk < 0.80)  ← PolicyAgent
        → budget_check_node      (check dept budget via SQLite)
        → llm_reviewer           (review with policy + budget context)
            → review_validator
                → PASS  → record_outcome
                → REVISE → iteration_guard → llm_reviewer  (max 3 cycles)
    → high_risk_node             (risk ≥ 0.80, injection detected)
  → record_outcome → END

Changes from v1:
  - LoopAgent replaced with Workflow conditional back-edge cycle (ADK 2.0 native)
  - PolicyAgent: dedicated LlmAgent for policy retrieval before review
  - BudgetAgent tool: budget_check reads dept spend from SQLite store
  - iteration_guard node: enforces max_iterations=3 without LoopAgent
"""
import json
import uuid
from datetime import datetime
from dotenv import load_dotenv

from google.adk.agents import LlmAgent
from google.adk.workflow import Workflow
from google.adk.events.event import Event, EventActions
from google.adk.agents.context import Context
from google.adk.apps.app import App

from expense_agent.security import redact_pii, detect_injection, compute_risk_score
from expense_agent.tools import (
    lookup_expense_policy,
    check_review_quality,
    budget_check,
)

load_dotenv()

GEMINI_MODEL        = "gemini-2.5-flash"
APPROVAL_THRESHOLD  = 100.0
RISK_THRESHOLD      = 0.80
MAX_REVIEW_ITERS    = 3


# ─── helpers ─────────────────────────────────────────────────────────────────
def _flat_record(expense: dict, status: str, reason: str, risk_score: float,
                 security_alert: bool = False) -> dict:
    return {
        "expense_id":     expense.get("expense_id") or str(uuid.uuid4()),
        "submitter":      expense.get("submitter", "unknown"),
        "amount":         float(expense.get("amount", 0)),
        "category":       expense.get("category", "other"),
        "description":    expense.get("description", ""),
        "risk_score":     round(float(risk_score), 3),
        "security_alert": security_alert,
        "status":         status,
        "reason":         reason,
        "created_at":     datetime.utcnow().isoformat(),
    }


# ─── Node 1: Parse ───────────────────────────────────────────────────────────
def parse_expense(node_input: str) -> Event:
    """Deserialize + normalise incoming expense JSON."""
    try:
        expense = json.loads(node_input) if isinstance(node_input, str) else node_input
    except Exception:
        expense = {
            "raw": str(node_input), "amount": 0,
            "category": "unknown", "description": "",
            "date": datetime.utcnow().date().isoformat(),
        }
    expense.setdefault("expense_id", str(uuid.uuid4()))
    return Event(
        output=expense,
        actions=EventActions(state_delta={"expense": expense, "review_iterations": 0})
    )


# ─── Node 2: Safety Gate ─────────────────────────────────────────────────────
def security_checkpoint(ctx: Context, node_input=None) -> Event:
    """
    Safety Gate: PII redaction → injection detection → risk scoring → routing.
    Routes: auto_approve | llm_review | high_risk
    """
    expense     = dict(ctx.state.get("expense", {}))
    description = str(expense.get("description", ""))
    amount      = float(expense.get("amount", 0))

    scrubbed_desc, redacted = redact_pii(description)
    expense["description"]  = scrubbed_desc
    has_injection            = detect_injection(scrubbed_desc)
    risk_score               = compute_risk_score(amount, has_injection, redacted)

    state_delta = {
        "expense":              expense,
        "redacted_categories":  redacted,
        "risk_score":           risk_score,
        "security_alert":       has_injection,
    }

    if risk_score >= RISK_THRESHOLD:
        return Event(output=expense, actions=EventActions(route="high_risk",    state_delta=state_delta))
    elif amount < APPROVAL_THRESHOLD:
        return Event(output=expense, actions=EventActions(route="auto_approve", state_delta=state_delta))
    else:
        return Event(output=expense, actions=EventActions(route="llm_review",   state_delta=state_delta))


# ─── Node 3a: Auto-approve ───────────────────────────────────────────────────
def auto_approve_node(ctx: Context, node_input=None) -> Event:
    """Deterministic fast-path. Zero LLM calls."""
    expense    = ctx.state.get("expense", {})
    risk_score = ctx.state.get("risk_score", 0)
    reason     = (
        f"Amount ${float(expense.get('amount', 0)):.2f} is below "
        f"${APPROVAL_THRESHOLD} threshold. Auto-approved."
    )
    outcome = _flat_record(expense, "AUTO_APPROVED", reason, risk_score)
    return Event(output=outcome, actions=EventActions(state_delta={"outcome": outcome}))


# ─── Node 3b: High-risk escalation ───────────────────────────────────────────
def high_risk_node(ctx: Context, node_input=None) -> Event:
    """Security Gate bypass — LLM never sees injected payload."""
    expense    = ctx.state.get("expense", {})
    alert      = ctx.state.get("security_alert", False)
    redacted   = ctx.state.get("redacted_categories", [])
    risk_score = ctx.state.get("risk_score", 0)
    reason     = (
        "SECURITY ALERT: Prompt injection detected. LLM bypassed. Manual review required."
        if alert else
        f"Risk score {risk_score:.2f} ≥ {RISK_THRESHOLD}. Manual review required."
    )
    outcome = _flat_record(expense, "ESCALATED", reason, risk_score, security_alert=alert)
    outcome["redacted_categories"] = redacted
    return Event(output=outcome, actions=EventActions(state_delta={"outcome": outcome}))


# ─── PolicyAgent — dedicated policy retrieval agent ──────────────────────────
policy_agent = LlmAgent(
    name="PolicyAgent",
    model=GEMINI_MODEL,
    instruction="""You are the ExpenseIQ Policy Agent.

Your ONLY job: retrieve the applicable expense policy for this expense.

Expense details: {expense}

Call lookup_expense_policy with a precise question for the expense category,
e.g. "What is the policy for software license expenses?" or
"What is the travel policy for flights?"

Output the policy text verbatim as returned by the tool. Nothing else.""",
    tools=[lookup_expense_policy],
    output_key="policy_text",
)


# ─── BudgetCheck node — reads dept spend from SQLite ─────────────────────────
def budget_check_node(ctx: Context, node_input=None) -> Event:
    """
    Budget Agent node: calls budget_check tool to surface dept spend context.
    Writes budget_context to state for LLMReviewer to consume.
    """
    expense  = ctx.state.get("expense", {})
    category = expense.get("category", "other")
    amount   = float(expense.get("amount", 0))

    try:
        budget_info = budget_check(category, amount)
    except Exception:
        budget_info = f"Budget data unavailable for category '{category}'."

    return Event(
        output={"budget_context": budget_info},
        actions=EventActions(state_delta={"budget_context": budget_info})
    )


# ─── LLMReviewer — uses policy + budget context ──────────────────────────────
llm_reviewer = LlmAgent(
    name="LLMReviewer",
    model=GEMINI_MODEL,
    instruction="""You are a senior business expense reviewer for ExpenseIQ.

Expense details  : {expense}
Risk score       : {risk_score}
Company policy   : {policy_text}
Budget context   : {budget_context}

Write ONE clear sentence that includes ALL THREE of:
1. The specific business purpose
2. The exact dollar amount
3. Whether it is justified or not and why (cite the policy and budget context)

Output ONLY the review sentence. No preamble.""",
    output_key="review_reason",
)


# ─── ReviewValidator ─────────────────────────────────────────────────────────
review_validator = LlmAgent(
    name="ReviewValidator",
    model=GEMINI_MODEL,
    include_contents="none",
    instruction="""You are validating an expense review for completeness.

Review to validate: "{review_reason}"

Check ALL THREE criteria:
1. Specific business purpose mentioned? (not just "business expense")
2. Exact dollar amount mentioned?
3. Justification stated with reason (citing policy or budget)?

If ALL THREE present  → call check_review_quality with 'PASS'
If ANY missing        → call check_review_quality with 'REVISE: [what is missing]'""",
    tools=[check_review_quality],
)


# ─── Iteration guard — enforces max review cycles ────────────────────────────
def iteration_guard(ctx: Context, node_input=None) -> Event:
    """
    Replaces LoopAgent's max_iterations.
    Routes: retry → llm_reviewer | escalate → record_outcome
    """
    iters = int(ctx.state.get("review_iterations", 0)) + 1
    state_delta = {"review_iterations": iters}

    if iters >= MAX_REVIEW_ITERS:
        # Max cycles hit — escalate with best review so far
        return Event(
            output={"review_iterations": iters},
            actions=EventActions(route="escalate", state_delta=state_delta)
        )
    return Event(
        output={"review_iterations": iters},
        actions=EventActions(route="retry", state_delta=state_delta)
    )


# ─── Node 4: Record outcome ───────────────────────────────────────────────────
def record_outcome(ctx: Context, node_input=None) -> Event:
    """Consolidate final outcome → flat record → SQLite store."""
    expense       = ctx.state.get("expense", {})
    review_reason = ctx.state.get("review_reason", "")
    risk_score    = ctx.state.get("risk_score", 0)
    prior_outcome = ctx.state.get("outcome", {})

    if prior_outcome and prior_outcome.get("status"):
        outcome = dict(prior_outcome)
        if review_reason and not outcome.get("reason"):
            outcome["reason"] = review_reason
    else:
        outcome = _flat_record(
            expense, "APPROVED",
            review_reason or "LLM review passed.",
            risk_score,
        )

    try:
        from dashboard.store import record_expense
        record_expense(outcome)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("record_expense failed: %s", e)

    return Event(output=outcome, actions=EventActions(state_delta={"outcome": outcome}))


# ─── Workflow Graph ───────────────────────────────────────────────────────────
#
# LLM review path (ADK 2.0 native cycle — no LoopAgent):
#
#   security_checkpoint → [llm_review] → policy_agent
#                                          → budget_check_node
#                                            → llm_reviewer
#                                              → review_validator
#                                                → PASS    → record_outcome
#                                                → REVISE  → iteration_guard
#                                                              → retry    → llm_reviewer  (back-edge)
#                                                              → escalate → record_outcome
#
root_agent = Workflow(
    name="expense_workflow",
    edges=[
        # Spine
        ("START",              parse_expense,       security_checkpoint),

        # Gate routing
        (security_checkpoint, {
            "auto_approve": auto_approve_node,
            "llm_review":   policy_agent,          # enters multi-agent review pipeline
            "high_risk":    high_risk_node,
        }),

        # Multi-agent review pipeline (linear)
        (policy_agent,         budget_check_node),
        (budget_check_node,    llm_reviewer),
        (llm_reviewer,         review_validator),

        # Self-correcting cycle — review_validator routes PASS or REVISE
        (review_validator, {
            "PASS":   record_outcome,
            "REVISE": iteration_guard,
        }),

        # iteration_guard routes retry (back-edge) or escalate
        (iteration_guard, {
            "retry":    llm_reviewer,              # ← conditional back-edge (cycle)
            "escalate": record_outcome,
        }),

        # Terminal paths
        (auto_approve_node,    record_outcome),
        (high_risk_node,       record_outcome),
    ],
)

app = App(root_agent=root_agent, name="expense_agent")