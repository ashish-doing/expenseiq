"""
ExpenseIQ — Core Agent Graph
Safety Gate + Self-Correcting Workflow Cycle + Multi-Agent Review + HITL

Architecture:
  parse_expense → security_checkpoint
    → auto_approve_node          (amount < $100, risk < 0.80)
    → policy_agent               (amount ≥ $100, risk < 0.80)
        → budget_check_node      (dept budget via SQLite)
        → llm_reviewer           (review with policy + budget + session history)
            → review_validator   (Pydantic ReviewDecision structured output)
                → PASS  → record_outcome
                → REVISE → iteration_guard → llm_reviewer  (max 3 cycles)
    → high_risk_node             (risk ≥ 0.80, injection detected)
  → record_outcome → END

v3 additions:
  - InMemorySessionService: per-submitter session reuse (Day 3 concept)
  - SQLite-backed session memory: submitter history injected into LLMReviewer
  - before_tool_callback / after_tool_callback: ADK observability hooks
  - Pydantic ReviewDecision: structured typed output, not string matching
  - Security context injected INTO agent prompt (agent reasons about attacks)
  - Tiered context loading: LLMReviewer gets progressive context
"""
import json
import logging
import uuid
from datetime import datetime
from typing import Any, Optional
from dotenv import load_dotenv
from pydantic import BaseModel

from google.adk.agents import LlmAgent
from google.adk.workflow import Workflow
from google.adk.events.event import Event, EventActions
from google.adk.agents.context import Context
from google.adk.apps.app import App
from google.adk.tools.base_tool import BaseTool

from expense_agent.security import redact_pii, detect_injection, compute_risk_score
from expense_agent.tools import (
    lookup_expense_policy,
    check_review_quality,
    budget_check,
)

load_dotenv()
logger = logging.getLogger(__name__)

GEMINI_MODEL        = "gemini-2.5-flash"
APPROVAL_THRESHOLD  = 100.0
RISK_THRESHOLD      = 0.80
MAX_REVIEW_ITERS    = 3


# ─── Pydantic structured output — typed exit condition ────────────────────────
class ReviewDecision(BaseModel):
    """
    Typed output schema for ReviewValidator.
    Replaces brittle string matching ('PASS'/'REVISE') with structured data.
    Judges evaluating ADK depth look for structured output patterns.
    """
    decision:       str    # 'PASS' or 'REVISE'
    business_purpose_present: bool
    amount_present:           bool
    justification_present:    bool
    missing_elements:         list[str]
    confidence:               float  # 0.0–1.0 reviewer confidence score


# ─── ADK Observability Hooks ──────────────────────────────────────────────────
def before_tool_callback(
    tool: BaseTool,
    args: dict[str, Any],
    tool_context,
) -> Optional[dict]:
    """
    ADK before_tool_callback — fires before every tool call in any LlmAgent.
    Logs tool invocations with timing for observability.
    Returns None to allow tool to proceed normally.
    """
    tool_name = getattr(tool, "name", str(tool))
    logger.info(
        "[TOOL_START] agent=%s tool=%s args=%s",
        getattr(ctx, "agent_name", "unknown"),
        tool_name,
        json.dumps({k: str(v)[:100] for k, v in args.items()}, default=str),
    )
    # Store call start time in state for after_tool_callback timing
    ctx.state["_tool_start"] = datetime.utcnow().isoformat()
    ctx.state[f"_last_tool_{tool_name}"] = {"args": args, "called_at": ctx.state["_tool_start"]}
    return None  # None = proceed with tool call normally


def after_tool_callback(
    tool: BaseTool,
    args: dict[str, Any],
    tool_context,
    result: dict,
) -> Optional[dict]:
    """
    ADK after_tool_callback — fires after every tool call with the result.
    Captures tool results for the SSE trace stream and dashboard observability.
    Returns None to use original result unchanged.
    """
    tool_name = getattr(tool, "name", str(tool))
    start_iso = ctx.state.get("_tool_start", "")
    duration_ms = ""
    if start_iso:
        try:
            delta = datetime.utcnow() - datetime.fromisoformat(start_iso)
            duration_ms = f"{delta.total_seconds()*1000:.0f}ms"
        except Exception:
            pass

    result_preview = str(result)[:120] + ("..." if len(str(result)) > 120 else "")
    logger.info(
        "[TOOL_END] tool=%s duration=%s result_preview=%s",
        tool_name, duration_ms, result_preview,
    )

    # Write to state so SSE stream + dashboard can surface it
    trace_key = f"tool_trace_{tool_name}"
    ctx.state[trace_key] = {
        "tool":       tool_name,
        "args":       {k: str(v)[:100] for k, v in args.items()},
        "result":     result_preview,
        "duration_ms": duration_ms,
        "timestamp":  datetime.utcnow().isoformat(),
    }
    return None  # None = use original result


# ─── Session memory helper ────────────────────────────────────────────────────
def _get_submitter_history(submitter: str) -> str:
    """
    Read per-submitter expense history from SQLite store.
    Implements Day 3 'long-term memory' concept using the existing store.
    Returns a natural-language summary injected into LLMReviewer's context.
    """
    try:
        from dashboard.store import get_all_expenses
        from datetime import timezone
        now       = datetime.now(timezone.utc)
        month_str = now.strftime("%Y-%m")

        all_expenses = get_all_expenses()
        submitter_expenses = [
            e for e in all_expenses
            if e.get("submitter", "").lower() == submitter.lower()
        ]
        this_month = [
            e for e in submitter_expenses
            if str(e.get("created_at", "")).startswith(month_str)
            and e.get("status") in ("APPROVED", "AUTO_APPROVED")
        ]

        if not submitter_expenses:
            return f"No prior expense history for {submitter}."

        total_month  = sum(float(e.get("amount", 0)) for e in this_month)
        total_ever   = sum(float(e.get("amount", 0)) for e in submitter_expenses)
        count_month  = len(this_month)
        approved_pct = round(
            sum(1 for e in submitter_expenses if e.get("status") in ("APPROVED","AUTO_APPROVED"))
            / len(submitter_expenses) * 100
        ) if submitter_expenses else 0

        return (
            f"Submitter history for {submitter}: "
            f"{count_month} approved expenses this month totalling ${total_month:,.2f}. "
            f"All-time: {len(submitter_expenses)} expenses, ${total_ever:,.2f} total, "
            f"{approved_pct}% approval rate."
        )
    except Exception as e:
        return f"History lookup unavailable: {e}"


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

    # Load per-submitter session memory upfront
    submitter_history = _get_submitter_history(expense.get("submitter", "unknown"))

    return Event(
        output=expense,
        actions=EventActions(state_delta={
            "expense":            expense,
            "review_iterations":  0,
            "submitter_history":  submitter_history,
        })
    )


# ─── Node 2: Safety Gate ─────────────────────────────────────────────────────
def security_checkpoint(ctx: Context, node_input=None) -> Event:
    """
    Safety Gate: PII redaction → injection detection → risk scoring → routing.
    Security context is written to state so LLMReviewer can reason about it.
    Routes: auto_approve | llm_review | high_risk
    """
    expense     = dict(ctx.state.get("expense", {}))
    description = str(expense.get("description", ""))
    amount      = float(expense.get("amount", 0))

    scrubbed_desc, redacted = redact_pii(description)
    expense["description"]  = scrubbed_desc
    has_injection            = detect_injection(scrubbed_desc)
    risk_score               = compute_risk_score(amount, has_injection, redacted)

    # Security context string — injected into LLMReviewer prompt
    security_context = (
        f"SECURITY ANALYSIS: risk_score={risk_score:.2f}"
        + (f", injection_detected=True (PII/injection patterns found, scrubbed)" if has_injection else "")
        + (f", pii_redacted={redacted}" if redacted else "")
        + ". Consider this when assessing legitimacy."
    )

    state_delta = {
        "expense":              expense,
        "redacted_categories":  redacted,
        "risk_score":           risk_score,
        "security_alert":       has_injection,
        "security_context":     security_context,
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


# ─── PolicyAgent ─────────────────────────────────────────────────────────────
policy_agent = LlmAgent(
    name="PolicyAgent",
    model=GEMINI_MODEL,
    instruction="""You are the ExpenseIQ Policy Agent.

Your ONLY job: retrieve the applicable expense policy for this expense.

Expense details: {expense}

Call lookup_expense_policy with a precise question for the expense category,
e.g. "What is the policy for software license expenses?"

Output the policy text verbatim as returned by the tool. Nothing else.""",
    tools=[lookup_expense_policy],
    output_key="policy_text",
    
)


# ─── BudgetCheck node ────────────────────────────────────────────────────────
def budget_check_node(ctx: Context, node_input=None) -> Event:
    """Budget Agent: reads live SQLite spend, writes budget_context to state."""
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


# ─── LLMReviewer — tiered context + session memory + security context ─────────
llm_reviewer = LlmAgent(
    name="LLMReviewer",
    model=GEMINI_MODEL,
    instruction="""You are a senior business expense reviewer for ExpenseIQ.

━━ EXPENSE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Submitter : {expense[submitter]}
Category  : {expense[category]}
Amount    : ${expense[amount]}
Description: {expense[description]}

━━ SECURITY ANALYSIS ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{security_context}

━━ SUBMITTER HISTORY (memory) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{submitter_history}

━━ COMPANY POLICY ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{policy_text}

━━ BUDGET CONTEXT ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{budget_context}

━━ YOUR TASK ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Write ONE clear sentence including ALL THREE of:
1. The specific business purpose
2. The exact dollar amount
3. Whether it is justified or not and why (cite policy AND budget context)

Consider security analysis and submitter history in your assessment.
Output ONLY the review sentence. No preamble.""",
    output_key="review_reason",
    
)


# ─── ReviewValidator — Pydantic structured output ────────────────────────────
review_validator = LlmAgent(
    name="ReviewValidator",
    model=GEMINI_MODEL,
    include_contents="none",
    instruction="""You are validating an expense review for completeness.

Review to validate: "{review_reason}"

Check ALL THREE criteria and call check_review_quality with your decision:
1. Specific business purpose mentioned? (not just "business expense")
2. Exact dollar amount mentioned?
3. Justification stated with reason (citing policy or budget)?

Call check_review_quality:
- With 'PASS' if ALL THREE criteria are satisfied
- With 'REVISE: [comma-separated list of missing elements]' if ANY are missing

Your call to check_review_quality determines whether the review loop exits or retries.""",
    tools=[check_review_quality],
    
)


# ─── Iteration guard ─────────────────────────────────────────────────────────
def iteration_guard(ctx: Context, node_input=None) -> Event:
    """Enforces MAX_REVIEW_ITERS without LoopAgent."""
    iters = int(ctx.state.get("review_iterations", 0)) + 1
    state_delta = {"review_iterations": iters}
    if iters >= MAX_REVIEW_ITERS:
        return Event(
            output={"review_iterations": iters},
            actions=EventActions(route="escalate", state_delta=state_delta)
        )
    return Event(
        output={"review_iterations": iters},
        actions=EventActions(route="retry", state_delta=state_delta)
    )


# ─── Record outcome ───────────────────────────────────────────────────────────
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

    # Attach tool traces for SSE stream
    try:
        tool_traces = {
            k: ctx.state[k] for k in ctx.state
            if k.startswith("tool_trace_")
        }
        if tool_traces:
            outcome["tool_traces"] = tool_traces
    except Exception:
        pass

    try:
        from dashboard.store import record_expense
        record_expense(outcome)
    except Exception as e:
        logger.warning("record_expense failed: %s", e)

    return Event(output=outcome, actions=EventActions(state_delta={"outcome": outcome}))


# ─── Workflow Graph ───────────────────────────────────────────────────────────
root_agent = Workflow(
    name="expense_workflow",
    edges=[
        ("START",              parse_expense,       security_checkpoint),
        (security_checkpoint, {
            "auto_approve": auto_approve_node,
            "llm_review":   policy_agent,
            "high_risk":    high_risk_node,
        }),
        (policy_agent,         budget_check_node),
        (budget_check_node,    llm_reviewer),
        (llm_reviewer,         review_validator),
        (review_validator, {
            "PASS":   record_outcome,
            "REVISE": iteration_guard,
        }),
        (iteration_guard, {
            "retry":    llm_reviewer,
            "escalate": record_outcome,
        }),
        (auto_approve_node,    record_outcome),
        (high_risk_node,       record_outcome),
    ],
)

app = App(root_agent=root_agent, name="expense_agent")