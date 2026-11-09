from time import perf_counter

from fastapi import FastAPI, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.types import ASGIApp, Message, Receive, Scope, Send

METRICS_PATH = "/metrics"
HTTP_METHODS = {"GET", "HEAD", "POST", "PUT", "DELETE", "CONNECT", "OPTIONS", "TRACE", "PATCH"}


def configure_metrics(app: FastAPI, service_name: str) -> None:
    registry = CollectorRegistry()
    route_labels = ("service", "method", "route")
    status_labels = (*route_labels, "status_code")
    requests = Counter(
        "tracepilot_http_requests_total",
        "HTTP requests handled by this service.",
        status_labels,
        registry=registry,
    )
    errors = Counter(
        "tracepilot_http_request_errors_total",
        "HTTP requests with a 4xx or 5xx status.",
        status_labels,
        registry=registry,
    )
    duration = Histogram(
        "tracepilot_http_request_duration_seconds",
        "Time to handle an HTTP request, in seconds.",
        route_labels,
        registry=registry,
    )
    in_progress = Gauge(
        "tracepilot_http_requests_in_progress",
        "HTTP requests currently being handled by this service.",
        ("service",),
        registry=registry,
    ).labels(service_name)
    Gauge(
        "tracepilot_service_available",
        "Service process is running and exposing metrics. Prometheus up reports scrape failures.",
        ("service",),
        registry=registry,
    ).labels(service_name).set(1)

    def request_metrics(inner_app: ASGIApp) -> ASGIApp:
        async def middleware(scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] != "http" or scope["path"] == METRICS_PATH:
                await inner_app(scope, receive, send)
                return

            started = perf_counter()
            status_code = 500

            async def capture_status(message: Message) -> None:
                nonlocal status_code
                if message["type"] == "http.response.start":
                    status_code = message["status"]
                await send(message)

            in_progress.inc()
            try:
                await inner_app(scope, receive, capture_status)
            finally:
                elapsed = perf_counter() - started
                in_progress.dec()
                method = scope["method"] if scope["method"] in HTTP_METHODS else "OTHER"
                # Routing populates the template after the middleware has started.
                route = getattr(scope.get("route"), "path", "unmatched")
                labels = (service_name, method, route, str(status_code))
                requests.labels(*labels).inc()
                if 400 <= status_code < 600:
                    errors.labels(*labels).inc()
                duration.labels(service_name, method, route).observe(elapsed)

        return middleware

    app.add_middleware(request_metrics)

    @app.get(METRICS_PATH, include_in_schema=False)
    async def metrics() -> Response:
        return Response(generate_latest(registry), headers={"Content-Type": CONTENT_TYPE_LATEST})
