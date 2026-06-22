"""
ExpenseIQ FastAPI entry point.
Exposes ADK agent via REST + serves CRM dashboard.
"""
import json
import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Import after load_dotenv so env vars are available
from expense_agent.agent import app as adk_app
from dashboard.api import router as dashboard_router

session_service = InMemorySessionService()
runner = Runner(
    app=adk_app,
    session_service=session_service,
    auto_create_session=True,
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("ExpenseIQ agent started.")
    yield
    logger.info("ExpenseIQ agent stopped.")

fastapi_app = FastAPI(
    title="ExpenseIQ",
    description="Business Expense Intelligence Agent",
    version="1.0.0",
    lifespan=lifespan,
)

# Dashboard static files
fastapi_app.mount(
    "/dashboard",
    StaticFiles(directory="dashboard/static", html=True),
    name="dashboard",
)

# API routes
fastapi_app.include_router(dashboard_router)


@fastapi_app.get("/")
async def root():
    return {"message": "ExpenseIQ API running", "dashboard": "/dashboard", "docs": "/docs"}


@fastapi_app.post("/apps/expense_agent/trigger")
async def trigger_expense(request: Request):
    """Submit an expense for agent processing."""
    body = await request.json()
    user_id = body.get("submitter", "default-user").replace("@", "_").replace(".", "_")

    session = await session_service.create_session(
        app_name="expense_agent",
        user_id=user_id,
    )

    events = []
    final_outcome = None

    async for event in runner.run_async(
        user_id=user_id,
        session_id=session.id,
        new_message=genai_types.Content(
            role="user",
            parts=[genai_types.Part.from_text(text=json.dumps(body))]
        ),
    ):
        events.append(str(event.author) if hasattr(event, 'author') else str(event))
        if hasattr(event, 'output') and event.output:
            if isinstance(event.output, dict) and event.output.get("status"):
                final_outcome = event.output

    return JSONResponse({
        "status": "processed",
        "outcome": final_outcome,
        "events_count": len(events),
    })


@fastapi_app.post("/apps/expense_agent/trigger/pubsub")
async def trigger_pubsub(request: Request):
    """Pub/Sub compatible trigger endpoint."""
    import base64
    body = await request.json()
    subscription = body.get("subscription", "default-sub")
    user_id = subscription.split("/")[-1]
    data = body.get("message", {}).get("data", "")

    try:
        payload = json.loads(base64.b64decode(data).decode("utf-8"))
    except Exception:
        payload = {"raw": data, "amount": 0, "category": "unknown",
                   "description": "unparseable", "date": "2026-01-01"}

    session = await session_service.create_session(
        app_name="expense_agent",
        user_id=user_id,
    )

    events = []
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session.id,
        new_message=genai_types.Content(
            role="user",
            parts=[genai_types.Part.from_text(text=json.dumps(payload))]
        ),
    ):
        events.append(str(event))

    return JSONResponse({"status": "processed", "events": len(events)})