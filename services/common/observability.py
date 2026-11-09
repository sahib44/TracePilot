import json
import logging
import re
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from opentelemetry import trace

from services.common.tracing import configure_tracing, record_http_response, route_template, server_span

REQUEST_ID_HEADER = "X-Request-ID"
REQUEST_ID_PATTERN = re.compile(r"[!-~]{1,128}")
request_context: ContextVar[tuple[str, Request] | None] = ContextVar(
    "request_context", default=None
)
logger = logging.getLogger("tracepilot")


class RequestContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        context = request_context.get()
        if context is not None:
            service, request = context
            record.service = service
            for field in ("request_id", "session_id", "event_id"):
                setattr(record, field, getattr(request.state, field, None))
        span = trace.get_current_span().get_span_context()
        if span.is_valid:
            record.trace_id = trace.format_trace_id(span.trace_id)
            record.span_id = trace.format_span_id(span.span_id)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        for field in (
            "service", "request_id", "trace_id", "span_id", "session_id", "event_id", "method", "path",
            "status_code", "delay_seconds", "error",
        ):
            value = getattr(record, field, None)
            if value is not None:
                entry[field] = value
        return json.dumps(entry, default=str)


def configure_logging(
    app: FastAPI, service_name: str, *, tracer_provider: trace.TracerProvider | None = None
) -> None:
    configure_tracing(app, service_name, tracer_provider)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.addFilter(RequestContextFilter())
        logger.setLevel(logging.INFO)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Omit raw inputs, which may include non-JSON values or user data.
        details = [
            {key: error[key] for key in ("loc", "msg", "type")}
            for error in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": details})

    @app.middleware("http")
    async def log_request(request: Request, call_next):
        supplied_id = request.headers.get(REQUEST_ID_HEADER, "")
        request.state.request_id = (
            supplied_id if REQUEST_ID_PATTERN.fullmatch(supplied_id) else str(uuid4())
        )
        token = request_context.set((service_name, request))
        try:
            with server_span(request) as span:
                error_type = None
                try:
                    response = await call_next(request)
                except Exception as exc:
                    error_type = type(exc).__name__
                    logger.error("Unhandled request error", extra={"error": error_type})
                    response = JSONResponse(status_code=500, content={"detail": "Internal server error"})
                record_http_response(span, response.status_code, error_type=error_type)
                response.headers[REQUEST_ID_HEADER] = request.state.request_id
                level = logging.ERROR if response.status_code >= 500 else logging.INFO
                logger.log(
                    level,
                    "request_completed",
                    extra={
                        "method": request.method,
                        "path": route_template(request) or "unmatched",
                        "status_code": response.status_code,
                    },
                )
                return response
        finally:
            request_context.reset(token)
