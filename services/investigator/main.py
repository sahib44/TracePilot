import asyncio
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from functools import partial
from typing import Annotated
from uuid import UUID, uuid4

import httpx
import psycopg
from fastapi import FastAPI, HTTPException, Query, Request
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
from services.investigator.models import (
    SERVICES,
    DashboardQuery,
    DashboardSnapshot,
    HealthProbe,
    HistoryQuery,
    IncidentInput,
    InvestigationPage,
    InvestigationResult,
    InvestigationSummary,
    MetricReading,
    ServiceSnapshot,
)
from services.investigator.monitoring import (
    DASHBOARD_METRICS,
    GetTracesArgs,
    QueryMetricsArgs,
    get_traces,
    query_metric_range,
    query_metrics,
)


STORAGE_UNAVAILABLE = "Investigation storage unavailable"
SUMMARY_COLUMNS = """
    id,
    result->'incident' AS incident,
    result->>'status' AS status,
    (result->>'started_at')::timestamptz AS started_at,
    result->>'suspected_root_cause' AS suspected_root_cause,
    result->>'confidence' AS confidence,
    (result->>'duration_ms')::bigint AS duration_ms
"""


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


async def list_results(query: HistoryQuery) -> InvestigationPage:
    # Only this fixed SQL keyword varies. Search terms, status, and page bounds are bound parameters.
    direction = "ASC" if query.sort == "oldest" else "DESC"
    search = query.search or None
    async with await store_connection() as connection:
        cursor = await connection.execute(
            f"""
            WITH filtered AS NOT MATERIALIZED (
                SELECT id, result FROM investigations
                WHERE (%s::text IS NULL OR result->>'status' = %s)
                    AND (%s::text IS NULL OR strpos(lower(concat_ws(' ',
                        id::text,
                        result->'incident'->>'description',
                        result->'incident'->>'request_id',
                        result->'incident'->>'trace_id',
                        result->'incident'->>'context',
                        result->>'suspected_root_cause'
                    )), lower(%s)) > 0)
            )
            SELECT totals.total, page.*
            FROM (SELECT count(*) AS total FROM filtered) AS totals
            LEFT JOIN LATERAL (
                SELECT {SUMMARY_COLUMNS} FROM filtered
                ORDER BY started_at {direction}, id {direction}
                LIMIT %s OFFSET %s
            ) AS page ON true
            ORDER BY page.started_at {direction}, page.id {direction}
            """,
            (query.status, query.status, search, search, query.limit, query.offset),
        )
        rows = await cursor.fetchall()
    return InvestigationPage(
        items=[InvestigationSummary.model_validate(row) for row in rows if row["id"] is not None],
        total=rows[0]["total"], limit=query.limit, offset=query.offset,
    )


async def recent_results() -> tuple[list[InvestigationSummary], str | None]:
    try:
        async with await store_connection() as connection:
            cursor = await connection.execute(
                f"SELECT {SUMMARY_COLUMNS} FROM investigations ORDER BY started_at DESC, id DESC LIMIT 6"
            )
            rows = await cursor.fetchall()
        return [InvestigationSummary.model_validate(row) for row in rows], None
    except psycopg.Error:
        return [], STORAGE_UNAVAILABLE


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
            service_health = partial(
                get_service_health,
                client=client,
                game_url=local_url("GAME_SERVER_PORT", 8000),
                event_processor_url=local_url("EVENT_PROCESSOR_PORT", 8001),
            )
            app.state.client = client
            app.state.service_health = service_health
            app.state.ollama_url = local_url("OLLAMA_PORT", 11434)
            app.state.prometheus_url = local_url("PROMETHEUS_PORT", 9090)
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
                        query_metrics, client=client, base_url=app.state.prometheus_url
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
                    handler=service_health,
                    description="Check current service liveness and readiness on localhost.",
                ),
            }
            yield

    app = FastAPI(title="TracePilot Investigator", lifespan=lifespan)
    configure_logging(app, "investigator")

    @app.exception_handler(psycopg.Error)
    async def database_error(request: Request, exc: psycopg.Error) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": STORAGE_UNAVAILABLE})

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/dashboard")
    async def dashboard(query: Annotated[DashboardQuery, Query()], request: Request) -> DashboardSnapshot:
        state = request.app.state
        window = 15 if query.window == "15" else 60
        step = window  # Both chart windows use 60 intervals.
        end = int(datetime.now(UTC).timestamp())
        timestamps = range(end - window * 60, end + 1, step)
        health_result, (recent, recent_error), *metric_results = await asyncio.gather(
            state.service_health(ServiceHealthArgs()),
            recent_results(),
            *(
                query_metric_range(
                    metric, timestamps=timestamps, client=state.client, base_url=state.prometheus_url,
                )
                for metric in DASHBOARD_METRICS
            ),
        )
        probes = {
            (record["service"], record["endpoint"]): HealthProbe.model_validate(record)
            for record in health_result["records"]
        }
        metrics = dict(zip(DASHBOARD_METRICS, metric_results, strict=True))
        return DashboardSnapshot(
            window_minutes=window, step_seconds=step, observed_at=datetime.fromtimestamp(end, UTC),
            services=[
                ServiceSnapshot(
                    service=service, liveness=probes.get((service, "/health")),
                    readiness=probes.get((service, "/ready")),
                    metrics={
                        metric: readings.get(service, MetricReading())
                        for metric, (readings, _) in metrics.items()
                    },
                )
                for service in SERVICES
            ],
            metric_errors={metric: error for metric, (_, error) in metrics.items() if error is not None},
            health_error=health_result.get("error"), recent_investigations=recent, recent_error=recent_error,
        )

    @app.get("/investigations")
    async def investigations(query: Annotated[HistoryQuery, Query()]) -> InvestigationPage:
        return await list_results(query)

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
