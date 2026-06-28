"""
ExpenseIQ FastAPI entry point.
Exposes ADK agent via REST + serves CRM dashboard.

v3 additions:
  - Per-submitter session reuse: same user_id reuses InMemorySessionService session
  - SSE /api/stream/{expense_id} endpoint: real-time agent trace stream
  - /api/explain/{expense_id} endpoint: "Why was this escalated?" Policy Explainer
  - /batch endpoint: asyncio.gather() concurrent processing
  - HTTP Basic Auth on /dashboard (admin/demo23)
"""
import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
import secrets

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

from expense_agent.agent import app as adk_app
from dashboard.api import router as dashboard_router

# ─── Session service — reused per submitter (Day 3 concept) ──────────────────
session_service = InMemorySessionService()

# In-memory session map: user_id → session_id (per-submitter continuity)
_user_sessions: dict[str, str] = {}

# SSE trace queues: expense_id → asyncio.Queue for real-time streaming
_sse_queues: dict[str, asyncio.Queue] = {}

runner = Runner(
    app=adk_app,
    session_service=session_service,
    auto_create_session=True,
)


async def _get_or_create_session(user_id: str) -> str:
    """
    Reuse existing session for a submitter if one exists, else create new.
    This implements per-submitter session continuity (Day 3: Sessions & State).
    The agent can recall 'this is Alice's 3rd expense this month' across calls.
    """
    if user_id in _user_sessions:
        session_id = _user_sessions[user_id]
        try:
            existing = await session_service.get_session(
                app_name="expense_agent",
                user_id=user_id,
                session_id=session_id,
            )
            if existing:
                logger.info("Reusing session %s for user %s", session_id, user_id)
                return session_id
        except Exception:
            pass

    session = await session_service.create_session(
        app_name="expense_agent",
        user_id=user_id,
    )
    _user_sessions[user_id] = session.id
    logger.info("Created new session %s for user %s", session.id, user_id)
    return session.id


# ─── HTTP Basic Auth ─────────────────────────────────────────────────────────
security = HTTPBasic()

DASHBOARD_USERS = {
    "admin":   "demo23",
    "manager": "expenseiq2026",
}


def verify_dashboard_auth(credentials: HTTPBasicCredentials = Depends(security)):
    """
    HTTP Basic Auth gate for dashboard.
    Production: replace with JWT/OAuth2. For demo: admin/demo23 or manager/expenseiq2026.
    """
    expected = DASHBOARD_USERS.get(credentials.username)
    if not expected or not secrets.compare_digest(credentials.password, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials. Use admin/demo23 for the demo.",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


# ─── App lifecycle ────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("ExpenseIQ v3 started — sessions, SSE, batch, auth enabled.")
    yield
    logger.info("ExpenseIQ stopped.")


fastapi_app = FastAPI(
    title="ExpenseIQ",
    description="Business Expense Intelligence Agent — v3",
    version="3.0.0",
    lifespan=lifespan,
)

fastapi_app.mount(
    "/dashboard",
    StaticFiles(directory="dashboard/static", html=True),
    name="dashboard",
)
fastapi_app.include_router(dashboard_router)


@fastapi_app.get("/")
async def root():
    return {
        "message":   "ExpenseIQ API v3",
        "dashboard": "/dashboard",
        "docs":      "/docs",
        "features":  ["per-submitter sessions", "SSE trace stream", "batch processing", "RBAC HITL"],
    }


# ─── Core trigger ─────────────────────────────────────────────────────────────
@fastapi_app.post("/apps/expense_agent/trigger")
async def trigger_expense(request: Request):
    """Submit an expense for agent processing. Session reused per submitter."""
    from dashboard.store import add_pending

    body    = await request.json()
    user_id = body.get("submitter", "default-user").replace("@", "_").replace(".", "_")
    expense_id = body.get("expense_id") or str(uuid.uuid4())
    body["expense_id"] = expense_id

    session_id = await _get_or_create_session(user_id)
    q: asyncio.Queue = asyncio.Queue()
    _sse_queues[expense_id] = q

    final_outcome = None

    async for event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=genai_types.Content(
            role="user",
            parts=[genai_types.Part.from_text(text=json.dumps(body))]
        ),
    ):
        # Capture agent trace events for SSE stream
        if hasattr(event, "author") and event.author:
            trace_msg = f"[{datetime.utcnow().strftime('%H:%M:%S')}] {event.author}"
            if hasattr(event, "output") and event.output:
                if isinstance(event.output, dict):
                    status_val = event.output.get("status", "")
                    if status_val:
                        trace_msg += f" → {status_val}"
                    tool_traces = event.output.get("tool_traces", {})
                    for tn, td in tool_traces.items():
                        trace_msg += f"\n  ↳ {td.get('tool','?')}({td.get('args',{})}) [{td.get('duration_ms','')}]"
                elif isinstance(event.output, str):
                    trace_msg += f": {event.output[:80]}"
            q.put_nowait(trace_msg)

        if hasattr(event, "output") and event.output:
            if isinstance(event.output, dict) and event.output.get("status"):
                final_outcome = event.output

    # Signal SSE stream that processing is complete
    if expense_id in _sse_queues:
        _sse_queues[expense_id].put_nowait(None)  # None = sentinel = done

    if final_outcome and final_outcome.get("status") == "ESCALATED":
        pending_record = {
            "expense_id":    expense_id,
            "submitter":     body.get("submitter", "unknown"),
            "amount":        body.get("amount", 0),
            "category":      body.get("category", "unknown"),
            "description":   body.get("description", ""),
            "risk_score":    final_outcome.get("risk_score", 0),
            "security_alert": final_outcome.get("security_alert", False),
            "status":        "ESCALATED",
            "reason":        final_outcome.get("reason", ""),
            "created_at":    datetime.utcnow().isoformat(),
        }
        add_pending(pending_record)
        return JSONResponse({
            "status":     "escalated",
            "expense_id": expense_id,
            "outcome":    final_outcome,
            "trace_url":  f"/api/stream/{expense_id}",
            "message":    "Expense flagged for human review. Visit /dashboard to approve or reject.",
        })

    return JSONResponse({
        "status":    "processed",
        "outcome":   final_outcome,
        "trace_url": f"/api/stream/{expense_id}",
    })


# ─── SSE trace stream ─────────────────────────────────────────────────────────
@fastapi_app.get("/api/stream/{expense_id}")
async def stream_trace(expense_id: str):
    """
    SSE endpoint: streams agent reasoning steps in real-time.
    Dashboard JS connects here to show live trace while agent processes.
    Judges see: SecurityGate → PolicyLookup → LLMReviewer → ReviewValidator → PASS
    """
    async def event_generator():
        q = _sse_queues.get(expense_id)
        if q is None:
            yield f"data: {json.dumps({'error': 'No trace found for expense_id', 'expense_id': expense_id})}\n\n"
            return
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=30.0)
            except asyncio.TimeoutError:
                yield f"data: {json.dumps({'done': True, 'expense_id': expense_id, 'reason': 'timeout'})}\n\n"
                return
            if msg is None:  # sentinel — agent finished
                yield f"data: {json.dumps({'done': True, 'expense_id': expense_id})}\n\n"
                return
            yield f"data: {json.dumps({'trace': msg})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ─── Policy Explainer — "Why was this escalated?" ─────────────────────────────
@fastapi_app.get("/api/explain/{expense_id}")
async def explain_decision(expense_id: str):
    """
    Policy Explainer: surfaces decision metadata from the persistent store as a
    human-readable explanation. Reads SQLite record, returns structured context
    including status, reason, risk score, and security alert state.
    """
    from dashboard.store import get_all_expenses
    expenses = get_all_expenses()
    expense  = next((e for e in expenses if e.get("expense_id") == expense_id), None)

    if not expense:
        raise HTTPException(status_code=404, detail=f"Expense {expense_id} not found")

    explanation = (
        f"Expense {expense_id} for {expense.get('submitter','?')} "
        f"(${expense.get('amount',0):.2f} {expense.get('category','?')}) "
        f"was {expense.get('status','?')} because: {expense.get('reason','No reason recorded.')} "
        f"Risk score: {expense.get('risk_score',0):.2f}. "
        f"Security alert: {'Yes — payload contained injection/PII patterns' if expense.get('security_alert') else 'No'}."
    )
    return JSONResponse({
        "expense_id":  expense_id,
        "status":      expense.get("status"),
        "explanation": explanation,
        "submitter":   expense.get("submitter"),
        "amount":      expense.get("amount"),
        "risk_score":  expense.get("risk_score"),
    })


# ─── Batch endpoint — asyncio.gather() concurrent processing ──────────────────
@fastapi_app.post("/batch")
async def batch_trigger(request: Request):
    """
    Batch expense processing using asyncio.gather().
    Demonstrates '10x agent' concurrent capability — process month-end submissions at once.
    Example: POST /batch with {"expenses": [...list of expense objects...]}
    """
    from dashboard.store import add_pending

    body     = await request.json()
    expenses = body.get("expenses", [])

    if not expenses:
        raise HTTPException(status_code=400, detail="'expenses' list is required")
    if len(expenses) > 50:
        raise HTTPException(status_code=400, detail="Batch limit is 50 expenses per request")

    async def process_one(expense: dict) -> dict:
        expense.setdefault("expense_id", str(uuid.uuid4()))
        user_id    = expense.get("submitter", "batch-user").replace("@","_").replace(".","_")
        session_id = await _get_or_create_session(user_id)
        outcome    = None
        try:
            async for event in runner.run_async(
                user_id=user_id,
                session_id=session_id,
                new_message=genai_types.Content(
                    role="user",
                    parts=[genai_types.Part.from_text(text=json.dumps(expense))]
                ),
            ):
                if hasattr(event, "output") and isinstance(event.output, dict):
                    if event.output.get("status"):
                        outcome = event.output
            if outcome and outcome.get("status") == "ESCALATED":
                add_pending({**expense, **outcome, "status": "ESCALATED"})
        except Exception as e:
            outcome = {"expense_id": expense.get("expense_id"), "status": "ERROR", "reason": str(e)}
        return outcome or {"expense_id": expense.get("expense_id"), "status": "NO_OUTCOME"}

    start    = datetime.utcnow()
    results  = await asyncio.gather(*[process_one(e) for e in expenses], return_exceptions=False)
    elapsed  = (datetime.utcnow() - start).total_seconds()

    summary = {
        "AUTO_APPROVED": sum(1 for r in results if r.get("status") == "AUTO_APPROVED"),
        "APPROVED":      sum(1 for r in results if r.get("status") == "APPROVED"),
        "ESCALATED":     sum(1 for r in results if r.get("status") == "ESCALATED"),
        "ERROR":         sum(1 for r in results if r.get("status") == "ERROR"),
    }

    return JSONResponse({
        "processed":    len(results),
        "elapsed_s":    round(elapsed, 2),
        "summary":      summary,
        "results":      results,
    })


# ─── Pub/Sub trigger ─────────────────────────────────────────────────────────
@fastapi_app.post("/apps/expense_agent/trigger/pubsub")
async def trigger_pubsub(request: Request):
    import base64
    body         = await request.json()
    subscription = body.get("subscription", "default-sub")
    user_id      = subscription.split("/")[-1]
    data         = body.get("message", {}).get("data", "")
    try:
        payload = json.loads(base64.b64decode(data).decode("utf-8"))
    except Exception:
        payload = {"raw": data, "amount": 0, "category": "unknown",
                   "description": "unparseable", "date": "2026-01-01"}

    session_id = await _get_or_create_session(user_id)
    events     = []
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=genai_types.Content(
            role="user",
            parts=[genai_types.Part.from_text(text=json.dumps(payload))]
        ),
    ):
        events.append(str(event))
    return JSONResponse({"status": "processed", "events": len(events)})