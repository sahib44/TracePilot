import logging
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from psycopg.errors import DivisionByZero

from services.common import tracing
from services.event_processor import main as processor
from services.game_server import main as game


class EventConnection:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        pass

    async def execute(self, statement, params=None):
        if statement == "SELECT 1 / 0":
            raise DivisionByZero("private database error text")
        self.params = params
        return self

    async def fetchone(self):
        event_id, session_id, event_type, payload, request_id = self.params
        return {
            "id": event_id,
            "session_id": session_id,
            "event_type": event_type,
            "payload": payload.obj,
            "request_id": request_id,
            "created_at": datetime.now(UTC),
        }


@pytest.fixture
def tracing_providers():
    providers = {
        name: TracerProvider(resource=Resource({"service.name": name}), shutdown_on_exit=False)
        for name in ("game-server", "event-processor")
    }
    exporters = {name: InMemorySpanExporter() for name in providers}
    for name, provider in providers.items():
        provider.add_span_processor(SimpleSpanProcessor(exporters[name]))
    yield providers, exporters
    for provider in providers.values():
        provider.shutdown()


@pytest.mark.parametrize("database_failure", [False, True])
def test_event_trace_links_services_database_and_logs(
    database_failure, tracing_providers, monkeypatch, caplog
):
    async def connect():
        return EventConnection()

    monkeypatch.setattr(processor, "connect", connect)
    providers, exporters = tracing_providers
    processor_app = processor.create_app(
        enable_failure_simulation=True, tracer_provider=providers["event-processor"]
    )
    if database_failure:
        with TestClient(processor_app) as client:
            assert client.put("/dev/failure", json={"mode": "database_error"}).status_code == 200
        exporters["event-processor"].clear()

    game_app = game.create_app(
        processor_transport=httpx.ASGITransport(app=processor_app),
        tracer_provider=providers["game-server"],
    )
    trace_id = uuid4().hex
    caller_span_id = "0000000000000042"
    session_id = str(uuid4())
    request_id = "private-request-id"
    with caplog.at_level(logging.INFO, logger="tracepilot"), TestClient(game_app) as client:
        response = client.post(
            f"/sessions/{session_id}/events?private-query=value",
            json={"event_type": "score_updated", "payload": {"private-body": "value"}},
            headers={
                "traceparent": f"00-{trace_id}-{caller_span_id}-01",
                "tracestate": "vendor=private-tracestate",
                "baggage": "example=private-baggage",
                "X-Request-ID": request_id,
            },
        )

    status_code = 503 if database_failure else 201
    assert response.status_code == status_code
    assert response.headers["X-Request-ID"] == request_id
    game_client, game_server = exporters["game-server"].get_finished_spans()
    database, processor_server = exporters["event-processor"].get_finished_spans()
    assert game_server.name == "POST /sessions/{session_id}/events"
    assert game_client.name == processor_server.name == "POST /process-event"
    assert database.name == ("SELECT" if database_failure else "INSERT game_events")
    assert game_server.kind == processor_server.kind == trace.SpanKind.SERVER
    assert game_client.kind == database.kind == trace.SpanKind.CLIENT
    assert game_server.parent.span_id == int(caller_span_id, 16)
    assert game_client.parent.span_id == game_server.context.span_id
    assert processor_server.parent.span_id == game_client.context.span_id
    assert database.parent.span_id == processor_server.context.span_id

    for span in (game_server, game_client, processor_server, database):
        assert span.context.trace_id == int(trace_id, 16)
        assert span.context.trace_state == trace.TraceState()
        assert span.end_time > span.start_time
        assert span.status.status_code == (
            trace.StatusCode.ERROR if database_failure else trace.StatusCode.UNSET
        )
        assert not span.events
        attributes = str(dict(span.attributes))
        assert "private-" not in attributes
        assert session_id not in attributes

    for span in (game_server, game_client, processor_server):
        assert span.attributes["http.response.status_code"] == status_code
    assert database.attributes["db.system.name"] == "postgresql"
    if database_failure:
        assert database.attributes["db.response.status_code"] == "22012"
        assert database.attributes["error.type"] == "DivisionByZero"

    completions = {
        record.service: record
        for record in caplog.records
        if record.getMessage() == "request_completed" and record.request_id == request_id
    }
    for service, span in (("game-server", game_server), ("event-processor", processor_server)):
        assert completions[service].trace_id == trace_id
        assert completions[service].span_id == trace.format_span_id(span.context.span_id)
        assert completions[service].status_code == status_code


def test_configured_otlp_exporter_flushes_traces_at_shutdown(monkeypatch):
    endpoint = "http://jaeger:4318/v1/traces"
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", endpoint)
    exported = []
    shutdown = []

    class Exporter(SpanExporter):
        def __init__(self, *, endpoint, timeout):
            assert endpoint == "http://jaeger:4318/v1/traces"
            assert timeout == 3

        def export(self, spans):
            exported.extend(spans)
            return SpanExportResult.SUCCESS

        def shutdown(self):
            shutdown.append(True)

    async def ready(table_name):
        pass

    monkeypatch.setattr(tracing, "OTLPSpanExporter", Exporter)
    monkeypatch.setattr(game, "check_database", ready)

    def processor_ready(request):
        assert request.url.path == "/ready"
        assert "traceparent" not in request.headers
        return httpx.Response(200)

    app = game.create_app(processor_transport=httpx.MockTransport(processor_ready))
    with TestClient(app) as client:
        for path in ("/health", "/ready", "/metrics"):
            assert client.get(path).status_code == 200
        assert client.get("/missing-private-path").status_code == 404

    assert shutdown == [True]
    span, = exported
    assert span.resource.attributes["service.name"] == "game-server"
    assert span.name == "GET"
    assert span.attributes["http.response.status_code"] == 404
    assert "missing-private-path" not in str(dict(span.attributes))
