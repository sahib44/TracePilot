import asyncio
import os
from contextlib import asynccontextmanager
from functools import partial
from uuid import UUID, uuid4

import httpx
import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb

from services.common.database import connect
from services.common.observability import configure_logging
from services.investigator.engine import ToolSpec, investigate
from services.investigator.local_tools import (
    SearchLogsArgs,
    ServiceHealthArgs,
    get_service_health,
    search_logs,
)
from services.investigator.models import IncidentInput, InvestigationResult
from services.investigator.monitoring import GetTracesArgs, QueryMetricsArgs, get_traces, query_metrics


def local_url(port_setting: str, default: int) -> str:
    port = int(os.getenv(port_setting, str(default)))
    if not 1 <= port <= 65535:
        raise ValueError(f"{port_setting} must be between 1 and 65535")
    return f"http://127.0.0.1:{port}"


async def store_connection():
    return await connect(
        default_port=os.getenv("POSTGRES_HOST_PORT", "5433"),
        default_password="tracepilot_dev",
    )


async def initialize_store() -> None:
    async with await store_connection() as connection:
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS investigations (
                id UUID PRIMARY KEY,
                result JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


async def save_result(result: InvestigationResult) -> None:
    async with await store_connection() as connection:
        await connection.execute(
            "INSERT INTO investigations (id, result) VALUES (%s, %s)",
            (result.id, Jsonb(result.model_dump(mode="json"))),
        )


async def load_result(investigation_id: UUID) -> InvestigationResult | None:
    async with await store_connection() as connection:
        cursor = await connection.execute(
            "SELECT result FROM investigations WHERE id = %s", (investigation_id,)
        )
        row = await cursor.fetchone()
    return InvestigationResult.model_validate(row["result"]) if row is not None else None


def create_app(*, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        max_tool_calls = int(os.getenv("INVESTIGATION_MAX_TOOL_CALLS", "8"))
        timeout_seconds = float(os.getenv("INVESTIGATION_TIMEOUT_SECONDS", "180"))
        if not 1 <= max_tool_calls <= 12:
            raise ValueError("INVESTIGATION_MAX_TOOL_CALLS must be between 1 and 12")
        if not 1 <= timeout_seconds <= 300:
            raise ValueError("INVESTIGATION_TIMEOUT_SECONDS must be between 1 and 300")

        await initialize_store()
        async with httpx.AsyncClient(
            timeout=timeout_seconds, transport=transport, trust_env=False
        ) as client:
            app.state.client = client
            app.state.ollama_url = local_url("OLLAMA_PORT", 11434)
            app.state.max_tool_calls = max_tool_calls
            app.state.timeout_seconds = timeout_seconds
            app.state.investigation_lock = asyncio.Lock()
            app.state.tools = {
                "search_logs": ToolSpec(
                    args_model=SearchLogsArgs,
                    handler=search_logs,
                    description=(
                        "Read recent structured application logs. Filter by request_id to find its "
                        "trace_id and all related events, including any later write or failure."
                    ),
                ),
                "query_metrics": ToolSpec(
                    args_model=QueryMetricsArgs,
                    handler=partial(
                        query_metrics, client=client, base_url=local_url("PROMETHEUS_PORT", 9090)
                    ),
                    description="Read aggregate request, error, latency or availability metrics.",
                ),
                "get_traces": ToolSpec(
                    args_model=GetTracesArgs,
                    handler=partial(
                        get_traces, client=client, base_url=local_url("JAEGER_PORT", 16686)
                    ),
                    description=(
                        "Read a distributed trace by trace ID, including span timing, HTTP statuses "
                        "and database errors. Use a trace ID from the incident or matching logs."
                    ),
                ),
                "get_service_health": ToolSpec(
                    args_model=ServiceHealthArgs,
                    handler=partial(
                        get_service_health,
                        client=client,
                        game_url=local_url("GAME_SERVER_PORT", 8000),
                        event_processor_url=local_url("EVENT_PROCESSOR_PORT", 8001),
                    ),
                    description="Check current service liveness and readiness on localhost.",
                ),
            }
            yield

    app = FastAPI(title="TracePilot Investigator", lifespan=lifespan)
    configure_logging(app, "investigator")

    @app.exception_handler(psycopg.Error)
    async def database_error(request: Request, exc: psycopg.Error) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": "Investigation storage unavailable"})

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/investigations", status_code=201)
    async def create_investigation(incident: IncidentInput, request: Request) -> InvestigationResult:
        state = request.app.state
        if state.investigation_lock.locked():
            raise HTTPException(status_code=429, detail="An investigation is already running")
        async with state.investigation_lock:
            result = await investigate(
                incident,
                investigation_id=uuid4(),
                client=state.client,
                ollama_url=state.ollama_url,
                tools=state.tools,
                max_tool_calls=state.max_tool_calls,
                timeout_seconds=state.timeout_seconds,
            )
            await save_result(result)
            return result

    @app.get("/investigations/{investigation_id}")
    async def get_investigation(investigation_id: UUID) -> InvestigationResult:
        result = await load_result(investigation_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Investigation not found")
        return result

    return app


app = create_app()
