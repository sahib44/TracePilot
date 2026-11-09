import asyncio
import os
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar

from fastapi import FastAPI, Request
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.util.types import Attributes

TRACER_NAME = "tracepilot"
UNTRACED_PATHS = {"/health", "/ready", "/metrics"}
HTTP_METHODS = {"GET", "HEAD", "POST", "PUT", "DELETE", "CONNECT", "OPTIONS", "TRACE", "PATCH"}
trace_context = TraceContextTextMapPropagator()
request_tracer: ContextVar[trace.Tracer | None] = ContextVar("request_tracer", default=None)


def configure_tracing(
    app: FastAPI, service_name: str, provider: trace.TracerProvider | None = None
) -> None:
    app.state.tracer = provider.get_tracer(TRACER_NAME) if provider is not None else None
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "").strip()
    if provider is not None or not endpoint:
        return

    application_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        provider = TracerProvider(
            resource=Resource({"service.name": service_name}), shutdown_on_exit=False
        )
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=endpoint, timeout=3),
                max_queue_size=512,
                max_export_batch_size=128,
                schedule_delay_millis=500,
            )
        )
        app.state.tracer = provider.get_tracer(TRACER_NAME)
        try:
            async with application_lifespan(app) as state:
                yield state
        finally:
            app.state.tracer = None
            await asyncio.to_thread(provider.shutdown)

    app.router.lifespan_context = lifespan


@contextmanager
def server_span(request: Request) -> Iterator[trace.Span]:
    tracer = request.app.state.tracer if request.url.path not in UNTRACED_PATHS else None
    token = request_tracer.set(tracer)
    try:
        if tracer is None:
            yield trace.INVALID_SPAN
            return

        method = request.method if request.method in HTTP_METHODS else "_OTHER"
        with tracer.start_as_current_span(
            method,
            # Propagate traceparent only. Arbitrary baggage and tracestate are not needed locally.
            context=trace_context.extract(
                {"traceparent": request.headers.get("traceparent", "")}, context=Context()
            ),
            kind=trace.SpanKind.SERVER,
            attributes={"http.request.method": method},
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            yield span
            route = route_template(request)
            if route:
                span.update_name(f"{method} {route}")
                span.set_attribute("http.route", route)
    finally:
        request_tracer.reset(token)


def route_template(request: Request) -> str | None:
    return getattr(request.scope.get("route"), "path_format", None)


@contextmanager
def operation_span(
    name: str, *, kind: trace.SpanKind = trace.SpanKind.INTERNAL, attributes: Attributes = None
) -> Iterator[trace.Span]:
    tracer = request_tracer.get()
    if tracer is None:
        yield trace.INVALID_SPAN
        return

    with tracer.start_as_current_span(
        name,
        kind=kind,
        attributes=attributes,
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        yield span


def mark_span_error(span: trace.Span, error_type: str) -> None:
    span.set_status(trace.StatusCode.ERROR)
    span.set_attribute("error.type", error_type)


def record_http_response(
    span: trace.Span, status_code: int, *, error_threshold: int = 500, error_type: str | None = None
) -> None:
    span.set_attribute("http.response.status_code", status_code)
    if status_code >= error_threshold:
        mark_span_error(span, error_type or str(status_code))
