import asyncio
import base64
import json

import httpx
import pytest
from pydantic import ValidationError

from services.investigator import monitoring
from services.investigator.monitoring import GetTracesArgs, QueryMetricsArgs, get_traces, query_metrics


TRACE_ID = "abcd0123456789abcdef0123456789ab"
BASE_URL = "http://test-monitoring"


def call(tool, args, handler):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await tool(args, client=client, base_url=BASE_URL)

    return asyncio.run(run())


@pytest.mark.parametrize(
    ("metric", "fragment", "labels", "unit"),
    [
        ("requests", "tracepilot_http_requests_total", {"service": "game-server"}, "requests"),
        ("errors", "tracepilot_http_request_errors_total", {"service": "game-server", "status_code": "503"}, "errors"),
        ("error_rate", '[5m]', {"service": "game-server"}, "errors/second"),
        ("latency", "histogram_quantile(0.95", {"service": "game-server"}, "seconds"),
        ("availability", 'up{job="tracepilot",instance="game-server:8000"}', {"instance": "game-server:8000"}, "up"),
    ],
)
def test_query_metrics_uses_a_bounded_predefined_query(metric, fragment, labels, unit):
    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/api/v1/query"
        params = request.url.params
        assert fragment in params["query"]
        assert 'job="tracepilot"' in params["query"]
        if metric != "availability":
            assert 'service="game-server"' in params["query"]
        assert params["timeout"] == "2s"
        assert params["limit"] == str(monitoring.MAX_METRIC_SAMPLES + 1)
        assert params["time"] == "2026-11-08T02:30:00+00:00"
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {"resultType": "vector", "result": [{"metric": labels, "value": [0, "0"]}]},
            },
        )

    args = QueryMetricsArgs(metric=metric, service="game-server", at="2026-11-07T22:30:00-04:00")
    result = call(query_metrics, args, handler)
    assert result["status"] == "ok"
    record, = result["records"]
    assert record["service"] == "game-server"
    assert record["value"] == 0
    assert record["metric"] == metric
    assert record["unit"] == unit
    if metric == "errors":
        assert record["http_status"] == 503
    if metric in ("latency", "error_rate"):
        assert record["window_seconds"] == 300


def test_tool_arguments_reject_promql_and_path_injection():
    with pytest.raises(ValidationError):
        QueryMetricsArgs(metric='sum(rate({__name__=~".+"}[1d]))')
    with pytest.raises(ValidationError):
        QueryMetricsArgs(metric="requests", service='game-server"}')
    with pytest.raises(ValidationError):
        GetTracesArgs(trace_id="../metrics")


def test_metrics_skip_nonfinite_samples_and_limit_evidence():
    samples = [{"metric": {"service": "game-server"}, "value": [0, "NaN"]}]
    samples.extend(
        {"metric": {"service": "event-processor"}, "value": [0, "1.25"]}
        for _ in range(monitoring.MAX_METRIC_SAMPLES)
    )

    def handler(request):
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": samples}})

    result = call(query_metrics, QueryMetricsArgs(metric="latency"), handler)
    assert result["status"] == "ok"
    assert result["truncated"]
    assert len(result["records"]) == monitoring.MAX_METRIC_SAMPLES - 1
    assert all(record["value"] == 1.25 for record in result["records"])


def attribute(key, value, value_type="stringValue"):
    return {"key": key, "value": {value_type: value}}


def trace_chunk(service, *spans):
    return json.dumps({"result": {"resourceSpans": [{
        "resource": {"attributes": [attribute("service.name", service)]},
        "scopeSpans": [{"spans": spans}],
    }]}})


def test_trace_preserves_status_and_relative_timing_without_raw_sql():
    start = 1_800_000_000_000_000_000
    root_id = "0000000000000001"
    root_span = {
        "traceId": TRACE_ID, "spanId": root_id, "name": "POST /items/{item_id}", "kind": 2,
        "startTimeUnixNano": str(start), "endTimeUnixNano": str(start + 1_000_000_000),
        "attributes": [attribute("http.response.status_code", "504", "intValue")],
        "status": {"code": 2},
    }
    db_span = {
        "traceId": base64.b64encode(bytes.fromhex(TRACE_ID)).decode(), "spanId": "0000000000000002",
        "parentSpanId": root_id, "name": "INSERT game_events", "kind": "SPAN_KIND_CLIENT",
        "startTimeUnixNano": str(start + 1_500_000_000), "endTimeUnixNano": str(start + 1_520_000_000),
        "attributes": [
            attribute("db.system.name", "postgresql"), attribute("db.response.status_code", "22012"),
            attribute("error.type", "DivisionByZero"), attribute("db.query.text", "private SQL"),
        ],
        "status": {"code": "STATUS_CODE_ERROR", "message": "private exception"},
    }

    def handler(request):
        assert request.url.path == f"/api/v3/traces/{TRACE_ID}"
        assert request.url.params["rawTraces"] == "true"
        return httpx.Response(200, text="\n".join([
            trace_chunk("event-processor", db_span), trace_chunk("game-server", root_span),
        ]))

    result = call(get_traces, GetTracesArgs(trace_id=TRACE_ID.upper()), handler)
    assert result["status"] == "ok"
    assert "private" not in json.dumps(result)
    record, = result["records"]
    assert record["trace_id"] == TRACE_ID
    assert record["duration_ms"] == 1520
    root, operation = record["spans"]
    assert root["http_status"] == 504
    assert root["status"] == "error"
    assert root["end_offset_ms"] == 1000
    assert operation["parent_span_id"] == root["span_id"]
    assert operation["service"] == "event-processor"
    assert operation["start_offset_ms"] == 1500
    assert operation["duration_ms"] == 20
    assert operation["sqlstate"] == "22012"
    assert operation["error_type"] == "DivisionByZero"


def test_large_trace_retains_its_late_failing_span():
    span_count = monitoring.MAX_TRACE_SPANS + 2
    spans = [
        {
            "traceId": TRACE_ID, "spanId": f"{index:016x}", "name": f"Operation {index}",
            "startTimeUnixNano": str(index * 1000), "endTimeUnixNano": str(index * 2000),
            "parentSpanId": "0000000000000001" if index != 1 else "",
            "status": {"code": 2 if index == span_count else 0},
        }
        for index in range(1, span_count + 1)
    ]
    result = call(
        get_traces, GetTracesArgs(trace_id=TRACE_ID),
        lambda request: httpx.Response(200, text=trace_chunk("event-processor", *spans)),
    )
    assert result["status"] == "ok"
    assert result["truncated"]
    record, = result["records"]
    assert record["span_count"] == span_count
    assert len(record["spans"]) == monitoring.MAX_TRACE_SPANS
    assert record["spans"][0]["operation"] == "Operation 1"
    assert record["spans"][-1]["operation"] == f"Operation {span_count}"
    assert record["spans"][-1]["status"] == "error"


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(404), "no trace"),
        (httpx.Response(503), "HTTP 503"),
        (httpx.Response(200, text="invalid"), "malformed trace"),
    ],
)
def test_jaeger_missing_or_invalid_response_returns_no_evidence(response, expected):
    result = call(get_traces, GetTracesArgs(trace_id=TRACE_ID), lambda request: response)
    assert result["status"] == "error"
    assert result["records"] == []
    assert expected in result["error"]


def test_prometheus_timeout_connection_error_and_response_limit(monkeypatch):
    args = QueryMetricsArgs(metric="availability")

    async def stalled(request):
        await asyncio.sleep(10)

    def disconnected(request):
        raise httpx.ConnectError("private error")

    monkeypatch.setattr(monitoring, "SOURCE_TIMEOUT_SECONDS", 0.01)
    for handler, expected in (
        (stalled, "timed out"),
        (disconnected, "unavailable"),
        (lambda request: httpx.Response(200, content=b"x" * (65 * 1024)), "size limit"),
    ):
        result = call(query_metrics, args, handler)
        assert result["status"] == "error"
        assert result["records"] == []
        assert expected in result["error"]
