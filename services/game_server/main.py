import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import UUID, uuid4

import httpx
import psycopg
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from opentelemetry import trace
from pydantic import BaseModel, Field, ValidationError, field_validator

from services.common.database import check_database, connect, database_span
from services.common.metrics import configure_metrics
from services.common.models import EventInput, EventRecord, ProcessEventInput
from services.common.observability import REQUEST_ID_HEADER, configure_logging
from services.common.tracing import mark_span_error, operation_span, record_http_response, trace_context

logger = logging.getLogger("tracepilot")


class SessionInput(BaseModel):
    player_id: str = Field(min_length=1, max_length=255)

    @field_validator("player_id")
    @classmethod
    def validate_player_id(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("Player identifier must contain text and no null characters")
        value.encode("utf-8")
        return value


class SessionRecord(SessionInput):
    id: UUID
    status: str
    created_at: datetime


def create_app(
    *,
    processor_transport: httpx.AsyncBaseTransport | None = None,
    tracer_provider: trace.TracerProvider | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with httpx.AsyncClient(
            base_url=os.getenv("PROCESSOR_URL", "http://event-processor:8000"),
            timeout=float(os.getenv("PROCESSOR_TIMEOUT_SECONDS", "1.0")),
            transport=processor_transport,
            trust_env=False,
        ) as client:
            app.state.processor = client
            yield

    app = FastAPI(title="TracePilot Game Server", lifespan=lifespan)
    configure_logging(app, "game-server", tracer_provider=tracer_provider)
    configure_metrics(app, "game-server")

    @app.exception_handler(psycopg.Error)
    async def database_error(request: Request, exc: psycopg.Error) -> JSONResponse:
        logger.error(
            "Database operation failed",
            extra={"error": {"type": type(exc).__name__, "sqlstate": exc.sqlstate}},
        )
        return JSONResponse(status_code=503, content={"detail": "Database unavailable"})

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready(request: Request) -> dict[str, str]:
        await check_database("game_sessions")
        try:
            response = await request.app.state.processor.get(
                "/ready", headers={REQUEST_ID_HEADER: request.state.request_id}
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning(
                "Event processor readiness check failed", extra={"error": type(exc).__name__}
            )
            raise HTTPException(status_code=503, detail="Event processor unavailable") from exc
        return {"status": "ready"}

    @app.post("/sessions", status_code=status.HTTP_201_CREATED)
    async def create_session(body: SessionInput, request: Request) -> SessionRecord:
        session_id = uuid4()
        request.state.session_id = session_id
        async with database_span("INSERT", "game_sessions"), await connect() as connection:
            cursor = await connection.execute(
                """
                INSERT INTO game_sessions (id, player_id, status)
                VALUES (%s, %s, 'active')
                RETURNING id, player_id, status, created_at
                """,
                (session_id, body.player_id),
            )
            row = await cursor.fetchone()
        logger.info("Player session created")
        return SessionRecord.model_validate(row)

    @app.get("/sessions/{session_id}")
    async def get_session(session_id: UUID, request: Request) -> SessionRecord:
        request.state.session_id = session_id
        async with database_span("SELECT", "game_sessions"), await connect() as connection:
            cursor = await connection.execute(
                """
                SELECT id, player_id, status, created_at
                FROM game_sessions
                WHERE id = %s
                """,
                (session_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return SessionRecord.model_validate(row)

    @app.post("/sessions/{session_id}/events", status_code=status.HTTP_201_CREATED)
    async def submit_event(session_id: UUID, body: EventInput, request: Request) -> EventRecord:
        request.state.session_id = session_id
        request_id = request.state.request_id
        event_input = ProcessEventInput(session_id=session_id, **body.model_dump())
        logger.info("Forwarding event to processor")
        with operation_span(
            "POST /process-event",
            kind=trace.SpanKind.CLIENT,
            attributes={"http.request.method": "POST"},
        ) as span:
            headers = {REQUEST_ID_HEADER: request_id}
            if span.get_span_context().is_valid:
                trace_context.inject(headers)
            try:
                response = await request.app.state.processor.post(
                    "/process-event", json=event_input.model_dump(mode="json"), headers=headers
                )
            except httpx.TimeoutException as exc:
                error_type = type(exc).__name__
                mark_span_error(span, error_type)
                logger.warning("Event processor timed out", extra={"error": error_type})
                raise HTTPException(status_code=504, detail="Event processor timed out") from exc
            except httpx.RequestError as exc:
                error_type = type(exc).__name__
                mark_span_error(span, error_type)
                logger.warning("Event processor request failed", extra={"error": error_type})
                raise HTTPException(status_code=503, detail="Event processor unavailable") from exc
            record_http_response(span, response.status_code, error_threshold=400)

        if response.status_code == 404:
            raise HTTPException(status_code=404, detail="Session not found")
        if response.status_code == 503:
            logger.warning("Event processor returned HTTP 503")
            raise HTTPException(status_code=503, detail="Event processor unavailable")
        if response.status_code != status.HTTP_201_CREATED:
            logger.error(
                "Event processor returned an unexpected status",
                extra={"error": f"HTTP {response.status_code}"},
            )
            raise HTTPException(status_code=502, detail="Event processor failed")

        try:
            event = EventRecord.model_validate_json(response.content)
        except ValidationError as exc:
            logger.error(
                "Event processor returned an invalid event",
                extra={"error": f"{exc.error_count()} response validation error(s)"},
            )
            raise HTTPException(status_code=502, detail="Invalid event processor response") from exc
        if event.session_id != session_id or event.request_id != request_id:
            logger.error(
                "Event processor returned an invalid event",
                extra={"error": "Session or request identifier did not match"},
            )
            raise HTTPException(status_code=502, detail="Invalid event processor response")
        request.state.event_id = event.id
        logger.info("Event accepted by processor")
        return event

    @app.get("/stats")
    async def stats() -> dict[str, int]:
        async with database_span("SELECT", "game_sessions"), await connect() as connection:
            cursor = await connection.execute(
                """
                SELECT COUNT(*) AS total_sessions,
                       COUNT(*) FILTER (WHERE status = 'active') AS active_sessions
                FROM game_sessions
                """
            )
            row = await cursor.fetchone()
        return row

    return app


app = create_app()
