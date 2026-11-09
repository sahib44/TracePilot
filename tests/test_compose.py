"""Run against the development stack with TRACEPILOT_COMPOSE_SMOKE=1."""

import base64
import json
import os
import re
import subprocess
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    os.environ.get("TRACEPILOT_COMPOSE_SMOKE") != "1",
    reason="set TRACEPILOT_COMPOSE_SMOKE=1 with the development stack running",
)
EVENT_ROUTES = {
    "game-server": "/sessions/{session_id}/events",
    "event-processor": "/process-event",
}


def compose(*args, sql=None):
    result = subprocess.run(
        ["docker", "compose", *args],
        cwd=PROJECT_ROOT,
        input=sql,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout.strip()


def service_url(service, port="8000"):
    address = compose("port", service, port)
    return f"http://{address}"


def postgres(sql, parameter, value):
    return compose(
        "exec",
        "-T",
        "db",
        "sh",
        "-c",
        'exec psql -X -Aqt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v "$1"',
        "psql",
        f"{parameter}={value}",
        sql=sql,
    )


def json_logs(service):
    output = compose("logs", "--no-color", "--no-log-prefix", "--since", "5m", service)
    for line in output.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            yield record


def wait_for(check, message):
    deadline = monotonic() + 25
    while monotonic() < deadline:
        result = check()
        if result:
            return result
        sleep(0.5)
    pytest.fail(message)


def metric_samples(prometheus):
    response = prometheus.get(
        "/api/v1/query", params={"query": '{__name__=~"up|tracepilot_.*",job="tracepilot"}'}
    ).raise_for_status().json()
    assert response["status"] == "success"
    return response["data"]["result"]


def metric_value(samples, name, **labels):
    return sum(
        float(sample["value"][1])
        for sample in samples
        if sample["metric"]["__name__"] == name
        and all(sample["metric"].get(key) == value for key, value in labels.items())
    )


def available_samples(prometheus):
    samples = metric_samples(prometheus)
    if all(
        metric_value(samples, "up", instance=f"{service}:8000") == 1
        and metric_value(samples, "tracepilot_service_available", service=service) == 1
        for service in EVENT_ROUTES
    ):
        return samples
    return None


def verify_metrics(prometheus, before, processor_timeout, session_id):
    def updated_samples():
        samples = metric_samples(prometheus)

        def delta(name, service, **labels):
            labels.update(service=service, method="POST", route=EVENT_ROUTES[service])
            return metric_value(samples, name, **labels) - metric_value(before, name, **labels)

        errors = "tracepilot_http_request_errors_total"
        failures = {"game-server": (502, 503, 504), "event-processor": (500, 503)}
        if all(
            delta(errors, service, status_code=str(status)) >= 1
            for service, statuses in failures.items()
            for status in statuses
        ) and all(
            delta("tracepilot_http_requests_total", service) >= requests
            and delta("tracepilot_http_request_duration_seconds_bucket", service, le="+Inf") >= requests
            and delta("tracepilot_http_request_duration_seconds_sum", service) >= processor_timeout
            for service, requests in {"game-server": 7, "event-processor": 6}.items()
        ):
            return samples
        return None

    samples = wait_for(updated_samples, "Prometheus did not scrape expected error and latency samples")
    for sample in samples:
        labels = sample["metric"]
        assert {"request_id", "session_id", "player_id", "event_id", "trace_id"}.isdisjoint(labels)
        assert session_id not in labels.get("route", "")


def trace_attributes(item):
    return {
        entry["key"]: next(iter(entry["value"].values()))
        for entry in item.get("attributes", [])
    }


def trace_identifier(value):
    if re.fullmatch(r"(?:[0-9a-fA-F]{16}|[0-9a-fA-F]{32})", value):
        return value.lower()
    return base64.b64decode(value, validate=True).hex()


def trace_spans(jaeger, trace_id):
    response = jaeger.get(f"/api/v3/traces/{trace_id}", params={"rawTraces": "true"})
    if response.status_code == 404:
        return {}
    response.raise_for_status()
    spans = {}
    for line in response.iter_lines():
        if not line.strip():
            continue
        chunk = json.loads(line)
        assert "error" not in chunk, chunk["error"]
        for resource in chunk["result"].get("resourceSpans", []):
            service = trace_attributes(resource["resource"])["service.name"]
            for scope in resource["scopeSpans"]:
                for span in scope["spans"]:
                    spans[service, span["name"]] = span
    return spans


def verify_trace(jaeger, trace_id, leaf_name, game_status, processor_status, private_values):
    game_server = ("game-server", f"POST {EVENT_ROUTES['game-server']}")
    game_client = ("game-server", "POST /process-event")
    processor_server = ("event-processor", "POST /process-event")
    leaf_key = ("event-processor", leaf_name)

    def complete_trace():
        spans = trace_spans(jaeger, trace_id)
        if {game_server, game_client, processor_server, leaf_key}.issubset(spans):
            return spans
        return None

    spans = wait_for(complete_trace, f"Incomplete Jaeger trace {trace_id} for {leaf_name}")
    entry, client, processor, leaf = (
        spans[key] for key in (game_server, game_client, processor_server, leaf_key)
    )
    parent_id = entry.get("parentSpanId")
    assert not parent_id or int(trace_identifier(parent_id), 16) == 0
    assert entry["kind"] in (2, "SPAN_KIND_SERVER")
    assert processor["kind"] in (2, "SPAN_KIND_SERVER")
    assert client["kind"] in (3, "SPAN_KIND_CLIENT")
    for parent, child in ((entry, client), (client, processor), (processor, leaf)):
        assert trace_identifier(child["parentSpanId"]) == trace_identifier(parent["spanId"])
    assert int(trace_attributes(entry)["http.response.status_code"]) == game_status
    assert int(trace_attributes(processor)["http.response.status_code"]) == processor_status
    for span in spans.values():
        assert trace_identifier(span["traceId"]) == trace_id
        assert int(span["endTimeUnixNano"]) >= int(span["startTimeUnixNano"])
        assert {"db.connection_string", "db.query.text", "db.statement", "url.full"}.isdisjoint(
            trace_attributes(span)
        )
    serialized = json.dumps(list(spans.values()))
    assert all(value not in serialized for value in private_values)

    return entry, client, processor, leaf


def test_compose_event_round_trip_and_failure_recovery():
    processor_timeout = float(
        compose("exec", "-T", "game-server", "printenv", "PROCESSOR_TIMEOUT_SECONDS")
    )
    prometheus_url = service_url("prometheus", "9090")
    with httpx.Client(base_url=prometheus_url, trust_env=False) as prometheus:
        baseline = wait_for(lambda: available_samples(prometheus), "Both Prometheus targets must be up")

    with (
        httpx.Client(
            base_url=service_url("game-server"),
            timeout=max(10, processor_timeout + 5),
            trust_env=False,
        ) as game,
        httpx.Client(base_url=service_url("event-processor"), trust_env=False) as processor,
    ):
        for service in (game, processor):
            assert service.get("/health").raise_for_status().json()["status"] == "ok"
            assert service.get("/ready").raise_for_status().json()["status"] == "ready"

        original_failure_mode = processor.get("/dev/failure").raise_for_status().json()
        try:
            processor.put("/dev/failure", json={"mode": "none"}).raise_for_status()
            player_id = f"smoke-{uuid4()}"
            created = game.post("/sessions", json={"player_id": player_id}).raise_for_status()
            assert created.status_code == 201
            session = created.json()
            session_id = session["id"]
            assert session["status"] == "active"
            assert game.get(f"/sessions/{session_id}").raise_for_status().json() == session
            assert game.get("/stats").raise_for_status().json()["active_sessions"] >= 1

            path = f"/sessions/{session_id}/events"
            payload_marker = f"private-payload-{uuid4()}"
            body = {"event_type": "score_updated", "payload": {"score": 17, "marker": payload_marker}}
            request_id = f"smoke-{uuid4()}"
            response = game.post(path, json=body, headers={"X-Request-ID": request_id})
            assert response.raise_for_status().status_code == 201
            assert response.headers["X-Request-ID"] == request_id
            event = response.json()
            assert event["request_id"] == request_id
            assert event["session_id"] == session_id
            assert event["payload"] == body["payload"]
            stats = processor.get("/stats").raise_for_status().json()
            assert stats["events_by_type"]["score_updated"] >= 1

            missing = game.post(f"/sessions/{uuid4()}/events", json=body)
            assert missing.status_code == 404
            assert game.post(path, json={"event_type": "unknown"}).status_code == 422

            processor.put("/dev/failure", json={"mode": "processor_error"}).raise_for_status()
            processor_error = game.post(path, json=body)
            assert processor_error.status_code == 502
            processor_failure_id = processor_error.headers["X-Request-ID"]

            processor.put("/dev/failure", json={"mode": "database_error"}).raise_for_status()
            database_failure_id = f"smoke-db-error-{uuid4()}"
            db_error = game.post(path, json=body, headers={"X-Request-ID": database_failure_id})
            assert db_error.status_code == 503
            assert db_error.headers["X-Request-ID"] == database_failure_id
            assert game.get("/health").status_code == 200

            delay = min(processor_timeout + 0.5, 60)
            assert delay > processor_timeout, "The smoke check requires a processor timeout below 60s"
            processor.put(
                "/dev/failure", json={"mode": "delay", "delay_seconds": delay}
            ).raise_for_status()
            timed_out = game.post(path, json=body)
            assert timed_out.status_code == 504
            delay_request_id = timed_out.headers["X-Request-ID"]

            processor.put("/dev/failure", json={"mode": "none"}).raise_for_status()
            recovered = game.post(path, json={"event_type": "match_ended"}).raise_for_status()
            assert recovered.status_code == 201
            assert recovered.json()["request_id"] == recovered.headers["X-Request-ID"]
        finally:
            processor.put("/dev/failure", json=original_failure_mode).raise_for_status()

    persisted = postgres(
        """
        SELECT json_build_object('id', e.id, 'session_id', s.id, 'player_id', s.player_id,
                                 'event_type', e.event_type, 'payload', e.payload,
                                 'request_id', e.request_id)
        FROM game_events e JOIN game_sessions s ON s.id = e.session_id
        WHERE e.id = :'event_id'::uuid;
        """,
        "event_id",
        event["id"],
    )
    assert json.loads(persisted) == {
        "id": event["id"],
        "session_id": session_id,
        "player_id": player_id,
        **body,
        "request_id": request_id,
    }
    assert postgres(
        "SELECT count(*) FROM game_events WHERE request_id = :'request_id';",
        "request_id",
        database_failure_id,
    ) == "0"

    with httpx.Client(base_url=prometheus_url, trust_env=False) as prometheus:
        verify_metrics(prometheus, baseline, processor_timeout, session_id)

    service_logs = {service: list(json_logs(service)) for service in EVENT_ROUTES}
    scenarios = {
        "success": (request_id, 201, 201, "INSERT game_events"),
        "processor_error": (processor_failure_id, 502, 500, "simulate processor error"),
        "database_error": (database_failure_id, 503, 503, "SELECT"),
        "delay": (delay_request_id, 504, 201, "simulate processor delay"),
    }
    with httpx.Client(base_url=service_url("jaeger", "16686"), trust_env=False) as jaeger:
        for mode, (correlation_id, game_status, processor_status, leaf_name) in scenarios.items():
            logs = {
                service: [record for record in records if record.get("request_id") == correlation_id]
                for service, records in service_logs.items()
            }
            completions = {}
            for service, status in (("game-server", game_status), ("event-processor", processor_status)):
                completion = [record for record in logs[service] if record.get("event") == "request_completed"]
                assert len(completion) == 1, f"Missing completion for {service} ({correlation_id})"
                record = completions[service] = completion[0]
                assert record["status_code"] == status
                assert record["service"] == service
                assert record["session_id"] == session_id
                assert record["timestamp"]
                assert record["level"] == ("ERROR" if status >= 500 else "INFO")
                if mode == "success":
                    assert record["event_id"] == event["id"]
            trace_id = completions["game-server"]["trace_id"]
            assert re.fullmatch(r"[0-9a-f]{32}", trace_id)
            print(f"{mode} trace {trace_id} (request {correlation_id})")
            entry, client, processor_span, leaf = verify_trace(
                jaeger,
                trace_id,
                leaf_name,
                game_status,
                processor_status,
                (player_id, session_id, event["id"], correlation_id, payload_marker),
            )
            for service, span in (("game-server", entry), ("event-processor", processor_span)):
                assert completions[service]["trace_id"] == trace_id
                assert completions[service]["span_id"] == trace_identifier(span["spanId"])
            if game_status >= 500:
                assert entry["status"]["code"] in (2, "STATUS_CODE_ERROR")
                assert client["status"]["code"] in (2, "STATUS_CODE_ERROR")
            leaf_attributes = trace_attributes(leaf)
            if mode == "delay":
                assert trace_attributes(client)["error.type"] == "ReadTimeout"
                assert float(leaf_attributes["tracepilot.delay_seconds"]) == delay
                assert int(leaf["endTimeUnixNano"]) - int(leaf["startTimeUnixNano"]) >= delay * 0.9e9
                assert any(
                    record.get("trace_id") == trace_id and record.get("delay_seconds") == delay
                    for record in logs["event-processor"]
                )
            if mode in ("success", "database_error"):
                assert leaf_attributes["db.system.name"] == "postgresql"
            if mode == "database_error":
                assert leaf_attributes["db.response.status_code"] == "22012"
                assert leaf_attributes["error.type"] == "DivisionByZero"
                assert any(
                    record.get("trace_id") == trace_id
                    and record.get("error") == {"type": "DivisionByZero", "sqlstate": "22012"}
                    for record in logs["event-processor"]
                )
            if mode == "processor_error":
                assert leaf_attributes["error.type"] == "SimulatedProcessorError"
                assert any(
                    record.get("trace_id") == trace_id
                    and record.get("error") == {"type": "SimulatedProcessorError"}
                    for record in logs["event-processor"]
                )
            if processor_status >= 500:
                assert processor_span["status"]["code"] in (2, "STATUS_CODE_ERROR")
                assert leaf["status"]["code"] in (2, "STATUS_CODE_ERROR")
