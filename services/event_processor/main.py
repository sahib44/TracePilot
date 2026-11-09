import asyncio
import logging
import os
from typing import Literal
from uuid import uuid4

import psycopg
from fastapi import FastAPI, HTTPException, Request
from opentelemetry import trace
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from services.common.database import check_database, connect, database_span
from services.common.metrics import configure_metrics
from services.common.models import EventRecord, ProcessEventInput
from services.common.observability import configure_logging
from services.common.tracing import mark_span_error, operation_span

logger = logging.getLogger("tracepilot")


class FailureConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["none", "delay", "processor_error", "database_error"] = "none"
    delay_seconds: float = Field(default=3.0, gt=0, le=60)


class EventStats(BaseModel):
    total_events: int
    events_by_type: dict[str, int]


def database_unavailable(description: str, exc: psycopg.Error) -> HTTPException:
    logger.error(
        description,
        extra={"error": {"type": type(exc).__name__, "sqlstate": exc.sqlstate}},
    )
    return HTTPException(status_code=503, detail="Database unavailable")


def create_app(
    *,
    enable_failure_simulation: bool | None = None,
    tracer_provider: trace.TracerProvider | None = None,
) -> FastAPI:
    if enable_failure_simulation is None:
        enable_failure_simulation = (
            os.getenv("ENABLE_FAILURE_SIMULATION", "false").strip().lower() == "true"
        )

    app = FastAPI(title="TracePilot Event Processor")
    configure_logging(app, "event-processor", tracer_provider=tracer_provider)
    configure_metrics(app, "event-processor")
    failure = FailureConfig()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> dict[str, str]:
        try:
            await check_database("game_events")
        except psycopg.Error as exc:
            raise database_unavailable("Database readiness check failed", exc) from exc
        return {"status": "ready"}

    @app.post("/process-event", status_code=201)
    async def process_event(event: ProcessEventInput, request: Request) -> EventRecord:
        event_id = uuid4()
        request.state.session_id = event.session_id
        request.state.event_id = event_id
        logger.info("Event processing started")

        current_failure = failure
        if current_failure.mode == "delay":
            with operation_span(
                "simulate processor delay",
                attributes={
                    "tracepilot.failure.mode": "delay",
                    "tracepilot.delay_seconds": current_failure.delay_seconds,
                },
            ):
                logger.warning(
                    "Simulating processor delay", extra={"delay_seconds": current_failure.delay_seconds}
                )
                await asyncio.sleep(current_failure.delay_seconds)
        if current_failure.mode == "processor_error":
            with operation_span(
                "simulate processor error", attributes={"tracepilot.failure.mode": "processor_error"}
            ) as span:
                mark_span_error(span, "SimulatedProcessorError")
                logger.error(
                    "Simulating processor failure", extra={"error": {"type": "SimulatedProcessorError"}}
                )
            raise HTTPException(status_code=500, detail="Simulated processor failure")

        # The deliberate fault runs a standalone SELECT in place of a successful INSERT.
        database_failure = current_failure.mode == "database_error"
        operation = ("SELECT", None) if database_failure else ("INSERT", "game_events")
        try:
            async with database_span(*operation) as span, await connect() as conn:
                if database_failure:
                    span.set_attribute("tracepilot.failure.mode", "database_error")
                    logger.warning("Simulating database operation failure")
                    await conn.execute("SELECT 1 / 0")
                cursor = await conn.execute(
                    """
                    INSERT INTO game_events (id, session_id, event_type, payload, request_id)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING id, session_id, event_type, payload, request_id, created_at
                    """,
                    (
                        event_id,
                        event.session_id,
                        event.event_type.value,
                        Jsonb(event.payload),
                        request.state.request_id,
                    ),
                )
                record = EventRecord.model_validate(await cursor.fetchone())
        except psycopg.errors.ForeignKeyViolation as exc:
            logger.warning(
                "Event session not found",
                extra={"error": {"type": type(exc).__name__, "sqlstate": exc.sqlstate}},
            )
            raise HTTPException(status_code=404, detail="Session not found") from exc
        except psycopg.Error as exc:
            raise database_unavailable("Event persistence failed", exc) from exc

        logger.info("Event persisted")
        return record

    @app.get("/stats")
    async def stats() -> EventStats:
        try:
            async with database_span("SELECT", "game_events"), await connect() as conn:
                cursor = await conn.execute(
                    "SELECT event_type, count(*) AS count FROM game_events GROUP BY event_type"
                )
                counts = {
                    row["event_type"]: row["count"] for row in await cursor.fetchall()
                }
        except psycopg.Error as exc:
            raise database_unavailable("Event statistics query failed", exc) from exc
        return EventStats(total_events=sum(counts.values()), events_by_type=counts)

    if enable_failure_simulation:

        @app.get("/dev/failure")
        async def get_failure() -> FailureConfig:
            return failure

        @app.put("/dev/failure")
        async def set_failure(config: FailureConfig) -> FailureConfig:
            nonlocal failure
            failure = config
            logger.info("Failure simulation mode set to %s", config.mode)
            return failure

    return app


app = create_app()
