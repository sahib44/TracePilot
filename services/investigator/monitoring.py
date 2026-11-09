import asyncio
import base64
import json
import math
import re
from datetime import UTC, datetime
from typing import Literal

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from services.investigator.models import (
    SERVICES,
    DashboardMetric,
    MetricPoint,
    MetricReading,
    Service,
    TraceId,
)


class QueryMetricsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric: Literal["requests", "errors", "error_rate", "latency", "availability"] = Field(
        description="Cumulative request count, cumulative error count by HTTP status, "
        "5xx errors per second over five minutes, p95 latency in seconds over five minutes, "
        "or scrape availability (1 is up, 0 is down)."
    )
    service: Service | None = Field(
        default=None, description="Limit to one service. Omit for both services."
    )
    at: AwareDatetime | None = Field(
        default=None, description="Optional ISO 8601 evaluation timestamp with a timezone."
    )


class GetTracesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trace_id: TraceId = Field(
        description="The exact 32-character hexadecimal trace ID, for example from search_logs.",
    )


MAX_METRIC_SAMPLES = 16
MAX_TRACE_SPANS = 16
SOURCE_TIMEOUT_SECONDS = 3
DASHBOARD_METRICS: tuple[DashboardMetric, ...] = ("throughput", "error_rate", "latency")


class SourceError(Exception):
    pass


async def _get(
    client: httpx.AsyncClient, url: str, params: dict, *, source: str, max_bytes: int
) -> bytearray:
    try:
        async with asyncio.timeout(SOURCE_TIMEOUT_SECONDS), client.stream(
            "GET", url, params=params, headers={"Accept-Encoding": "identity"},
            follow_redirects=False,
        ) as response:
            if source == "Jaeger" and response.status_code == 404:
                raise SourceError("Jaeger has no trace with that ID. It may not have arrived or may have expired.")
            if not response.is_success:
                raise SourceError(f"{source} returned HTTP {response.status_code}.")
            body = bytearray()
            async for chunk in response.aiter_bytes(8192):
                if len(body) + len(chunk) > max_bytes:
                    raise SourceError(f"{source} response exceeded the diagnostic size limit.")
                body.extend(chunk)
            return body
    except (TimeoutError, httpx.TimeoutException) as exc:
        raise SourceError(f"{source} request timed out.") from exc
    except httpx.HTTPError as exc:
        raise SourceError(f"{source} is unavailable.") from exc


def _query(args: QueryMetricsArgs) -> tuple[str, str]:
    filters = 'job="tracepilot"'
    if args.metric == "availability":
        if args.service:
            filters += f',instance="{args.service}:8000"'
        return f"up{{{filters}}}", "up"
    if args.service:
        filters += f',service="{args.service}"'
    if args.metric == "requests":
        return f"sum by (service) (tracepilot_http_requests_total{{{filters}}})", "requests"
    if args.metric == "errors":
        return f"sum by (service, status_code) (tracepilot_http_request_errors_total{{{filters}}})", "errors"
    return _rate_query(args.metric, filters)


def _rate_query(metric: DashboardMetric, filters: str) -> tuple[str, str]:
    if metric == "throughput":
        return f"sum by (service) (rate(tracepilot_http_requests_total{{{filters}}}[5m]))", "requests/second"
    if metric == "error_rate":
        return (
            "sum by (service) (rate(tracepilot_http_request_errors_total"
            f'{{{filters},status_code=~"5.."}}[5m]))',
            "errors/second",
        )
    return (
        "histogram_quantile(0.95, sum by (service, le) (rate("
        f"tracepilot_http_request_duration_seconds_bucket{{{filters}}}[5m])))",
        "seconds",
    )


def _result_error(error: str) -> dict:
    return {"status": "error", "records": [], "error": error}


async def query_metrics(args: QueryMetricsArgs, *, client: httpx.AsyncClient, base_url: str) -> dict:
    query, unit = _query(args)
    params = {"query": query, "timeout": "2s", "limit": MAX_METRIC_SAMPLES + 1}
    if args.at:
        params["time"] = args.at.astimezone(UTC).isoformat()
    try:
        body = await _get(
            client, f"{base_url.rstrip('/')}/api/v1/query", params, source="Prometheus",
            max_bytes=64 * 1024,
        )
        result = json.loads(body)
        if result["status"] != "success" or result["data"]["resultType"] != "vector":
            return _result_error("Prometheus did not return an instant-vector result.")
        samples = result["data"]["result"]
        records = []
        for sample in samples[:MAX_METRIC_SAMPLES]:
            timestamp, raw_value = sample["value"]
            value = float(raw_value)
            if not math.isfinite(value):
                continue
            labels = sample["metric"]
            service = (
                labels["instance"].partition(":")[0]
                if args.metric == "availability" else labels["service"]
            )
            if not isinstance(service, str):
                raise ValueError("Invalid metric service")
            record = {
                "service": service[:80], "metric": args.metric, "value": value, "unit": unit,
                "timestamp": datetime.fromtimestamp(timestamp, UTC).isoformat(),
            }
            if args.metric in ("error_rate", "latency"):
                record["window_seconds"] = 300
            if args.metric == "errors":
                record["http_status"] = int(labels["status_code"])
            records.append(record)
        return {"status": "ok", "records": records, "truncated": len(samples) > MAX_METRIC_SAMPLES}
    except SourceError as exc:
        return _result_error(str(exc))
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, OSError, RecursionError):
        return _result_error("Prometheus returned a malformed response.")


async def query_metric_range(
    metric: DashboardMetric, *, timestamps: range, client: httpx.AsyncClient, base_url: str
) -> tuple[dict[Service, MetricReading], str | None]:
    # Exclude readiness and liveness traffic, including the dashboard's own probes.
    query, _ = _rate_query(metric, 'job="tracepilot",route!~"/(health|ready)"')
    params = {
        "query": query, "start": timestamps[0], "end": timestamps[-1],
        "step": timestamps.step, "timeout": "2s", "limit": len(SERVICES),
    }
    try:
        body = await _get(
            client, f"{base_url.rstrip('/')}/api/v1/query_range", params,
            source="Prometheus", max_bytes=64 * 1024,
        )
        result = json.loads(body)
        if result["status"] != "success" or result["data"]["resultType"] != "matrix":
            raise SourceError("Prometheus did not return a range result.")

        samples = result["data"]["result"]
        if not isinstance(samples, list):
            raise ValueError("Invalid metric series")
        readings = {}
        for sample in samples[:len(SERVICES)]:
            service = sample["metric"]["service"]
            if service not in SERVICES:
                continue
            samples_at_time = sample["values"]
            if not isinstance(samples_at_time, list):
                raise ValueError("Invalid metric samples")
            values = {}
            for raw_timestamp, raw_value in samples_at_time[:len(timestamps)]:
                timestamp, value = float(raw_timestamp), float(raw_value)
                if timestamp in timestamps and math.isfinite(value):
                    values[timestamp] = value
            if values:
                readings[service] = MetricReading(
                    value=values.get(timestamps[-1]),
                    points=[MetricPoint(timestamp=t, value=values.get(t)) for t in timestamps],
                )
        return readings, None
    except SourceError as exc:
        return {}, str(exc)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        return {}, "Prometheus returned a malformed response."


def _identifier(value: str, length: int) -> str:
    if re.fullmatch(r"[0-9a-fA-F]+", value) and len(value) == length * 2:
        return value.lower()
    decoded = base64.b64decode(value, validate=True)
    if len(decoded) != length:
        raise ValueError("Invalid identifier length")
    return decoded.hex()


def _attributes(value: dict) -> dict:
    return {
        attribute["key"]: attribute["value"]
        for attribute in value.get("attributes", [])
    }


def _span_data(span: dict, service: str, trace_id: str) -> dict:
    if _identifier(span["traceId"], 16) != trace_id:
        raise ValueError("Unexpected trace ID")
    started = int(span["startTimeUnixNano"])
    ended = int(span["endTimeUnixNano"])
    if ended < started or started < 0:
        raise ValueError("Invalid span times")
    status = span.get("status", {}).get("code", 0)
    kind = span.get("kind", 0)
    kinds = ("unspecified", "internal", "server", "client", "producer", "consumer")
    if isinstance(kind, str):
        kind = kind.removeprefix("SPAN_KIND_").lower()
    elif isinstance(kind, int) and 0 <= kind < len(kinds):
        kind = kinds[kind]
    name = span["name"]
    if not isinstance(service, str) or not isinstance(name, str):
        raise ValueError("Invalid service or operation name")
    info = {
        "service": service[:80],
        "operation": name[:120],
        "span_id": _identifier(span["spanId"], 8),
        "started": started,
        "ended": ended,
        "kind": kind if kind in kinds else "unspecified",
        "status": {
            0: "unset", 1: "ok", 2: "error", "STATUS_CODE_UNSET": "unset",
            "STATUS_CODE_OK": "ok", "STATUS_CODE_ERROR": "error",
        }.get(status, "unknown"),
    }
    parent = span.get("parentSpanId")
    if parent:
        info["parent_span_id"] = _identifier(parent, 8)
    attributes = _attributes(span)
    for key, field in (
        ("error.type", "error_type"), ("db.system.name", "db_system"),
        ("db.operation.name", "db_operation"), ("db.collection.name", "db_collection"),
        ("db.response.status_code", "sqlstate"), ("tracepilot.failure.mode", "failure_mode"),
    ):
        text_value = attributes.get(key, {}).get("stringValue")
        if isinstance(text_value, str):
            info[field] = text_value[:80]
    if "http.response.status_code" in attributes:
        info["http_status"] = int(attributes["http.response.status_code"]["intValue"])
    if "tracepilot.delay_seconds" in attributes:
        delay = float(attributes["tracepilot.delay_seconds"]["doubleValue"])
        if math.isfinite(delay):
            info["delay_seconds"] = delay
    return info


async def get_traces(args: GetTracesArgs, *, client: httpx.AsyncClient, base_url: str) -> dict:
    trace_id = args.trace_id
    try:
        body = await _get(
            client, f"{base_url.rstrip('/')}/api/v3/traces/{trace_id}", {"rawTraces": "true"},
            source="Jaeger", max_bytes=256 * 1024,
        )
        spans = {}
        for line in body.splitlines():
            if not line.strip():
                continue
            chunk = json.loads(line)
            if "error" in chunk:
                return _result_error("Jaeger could not retrieve the requested trace.")
            for resource in chunk["result"].get("resourceSpans", []):
                service = _attributes(resource["resource"])["service.name"]["stringValue"]
                for scope in resource["scopeSpans"]:
                    for span in scope["spans"]:
                        data = _span_data(span, service, trace_id)
                        spans[data["span_id"]] = data
        if not spans:
            return _result_error("Jaeger returned no spans for that trace.")
        ordered = sorted(spans.values(), key=lambda span: span["started"])
        start = ordered[0]["started"]
        duration_ms = round((max(span["ended"] for span in ordered) - start) / 1e6, 3)
        truncated = len(ordered) > MAX_TRACE_SPANS
        selected = ordered
        if truncated:
            # Preserve roots and failing or database operations when a trace exceeds the limit.
            selected = sorted(ordered, key=lambda span: (
                bool(span.get("parent_span_id")), span["status"] != "error" and "db_system" not in span,
            ))[:MAX_TRACE_SPANS]
            selected.sort(key=lambda span: span["started"])
        record = {
            "trace_id": trace_id,
            "started_at": datetime.fromtimestamp(start / 1e9, UTC).isoformat(),
            "duration_ms": duration_ms,
            "span_count": len(ordered),
            "spans": selected,
        }
        for span in record["spans"]:
            started = span.pop("started")
            ended = span.pop("ended")
            span["start_offset_ms"] = round((started - start) / 1e6, 3)
            span["end_offset_ms"] = round((ended - start) / 1e6, 3)
            span["duration_ms"] = round((ended - started) / 1e6, 3)
        return {"status": "ok", "records": [record], "truncated": truncated}
    except SourceError as exc:
        return _result_error(str(exc))
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, OSError, RecursionError):
        return _result_error("Jaeger returned a malformed trace response.")
