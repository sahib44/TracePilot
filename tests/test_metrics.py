import asyncio

import httpx
from fastapi import FastAPI, Response
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from services.common.metrics import configure_metrics


def samples(exposition):
    return {
        (sample.name, frozenset(sample.labels.items())): sample.value
        for metric in text_string_to_metric_families(exposition)
        for sample in metric.samples
    }


def metric(name, **labels):
    return f"tracepilot_{name}", frozenset(labels.items())


def test_requests_and_errors_use_bounded_labels():
    app = FastAPI()
    configure_metrics(app, "test-service")

    @app.get("/items/{item_id}")
    async def item(item_id: str):
        return Response(status_code=503 if item_id == "broken-item" else 200)

    with TestClient(app) as client:
        item_one = client.get("/items/item-one", headers={"X-Request-ID": "request-value"})
        assert item_one.status_code == 200
        assert client.get("/items/item-two").status_code == 200
        assert client.get("/items/broken-item").status_code == 503
        assert client.get("/unknown-item").status_code == 404
        assert client.request("CUSTOM", "/unhandled").status_code == 404
        assert client.get("/metrics").status_code == 200
        response = client.get("/metrics")

    assert response.headers["content-type"].startswith("text/plain")
    for value in (
        "item-one", "item-two", "broken-item", "unknown-item", "request-value", "CUSTOM", "/metrics",
    ):
        assert value not in response.text

    values = samples(response.text)
    service = {"service": "test-service"}
    matched = {**service, "method": "GET", "route": "/items/{item_id}"}
    missing = {**service, "method": "GET", "route": "unmatched"}
    other = {**missing, "method": "OTHER"}
    assert values[metric("http_requests_total", **matched, status_code="200")] == 2
    assert values[metric("http_requests_total", **matched, status_code="503")] == 1
    assert values[metric("http_request_errors_total", **matched, status_code="503")] == 1
    assert values[metric("http_request_errors_total", **missing, status_code="404")] == 1
    assert values[metric("http_requests_total", **other, status_code="404")] == 1
    assert values[metric("http_request_duration_seconds_bucket", **matched, le="+Inf")] == 3
    assert values[metric("http_request_duration_seconds_count", **matched)] == 3
    assert values[metric("http_request_duration_seconds_sum", **matched)] > 0
    assert values[metric("http_requests_in_progress", **service)] == 0
    assert values[metric("service_available", **service)] == 1


def test_in_progress_tracks_response_until_stream_finishes():
    async def exercise():
        app = FastAPI()
        configure_metrics(app, "test-service")
        started = asyncio.Event()
        finish = asyncio.Event()

        @app.get("/stream")
        async def stream():
            async def body():
                yield b"first"
                started.set()
                await finish.wait()
                yield b"last"

            return StreamingResponse(body())

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            request = asyncio.create_task(client.get("/stream"))
            in_progress = metric("http_requests_in_progress", service="test-service")
            try:
                await asyncio.wait_for(started.wait(), timeout=5)
                assert samples((await client.get("/metrics")).text)[in_progress] == 1
            finally:
                finish.set()
                response = await request

            assert response.content == b"firstlast"
            values = samples((await client.get("/metrics")).text)
            assert values[in_progress] == 0
            request_count = metric(
                "http_requests_total",
                service="test-service", method="GET", route="/stream", status_code="200",
            )
            assert values[request_count] == 1

    asyncio.run(exercise())


def test_unhandled_error_is_counted_and_registries_are_isolated():
    app = FastAPI()
    second_app = FastAPI()
    configure_metrics(app, "test-service")
    configure_metrics(second_app, "test-service")

    @app.get("/crash")
    async def crash():
        raise RuntimeError("deliberate test error")

    with (
        TestClient(app, raise_server_exceptions=False) as client,
        TestClient(second_app) as second,
    ):
        assert client.get("/crash").status_code == 500
        first_metrics = samples(client.get("/metrics").text)
        second_metrics = samples(second.get("/metrics").text)

    error = metric(
        "http_request_errors_total", service="test-service", method="GET", route="/crash", status_code="500"
    )
    assert first_metrics[error] == 1
    assert first_metrics[metric("http_requests_in_progress", service="test-service")] == 0
    assert error not in second_metrics
