"""Shared fixtures for real incidents on the local Compose stack."""

import asyncio
import os
from datetime import UTC, datetime
from time import monotonic, sleep
from uuid import uuid4

import httpx
import psycopg

from services.investigator.local_tools import SearchLogsArgs, search_logs
from services.investigator.monitoring import GetTracesArgs, get_traces

DESCRIPTION = (
    "A client reported a failed event submission. Find what happened across the services "
    "and determine the outcome of the write."
)


def local_client(setting: str, default: int, timeout: float = 10) -> httpx.Client:
    port = int(os.getenv(setting, str(default)))
    return httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=timeout, trust_env=False)


def trigger_incident(mode: str, caller_status: int) -> dict:
    request_id = str(uuid4())
    processor_timeout = float(os.getenv("PROCESSOR_TIMEOUT_SECONDS", "1.0"))
    with (
        local_client("GAME_SERVER_PORT", 8000, timeout=max(10, processor_timeout + 3)) as game,
        local_client("EVENT_PROCESSOR_PORT", 8001) as processor,
    ):
        session_id = game.post(
            "/sessions", json={"player_id": "investigation-fixture"}
        ).raise_for_status().json()["id"]
        original_mode = processor.get("/dev/failure").raise_for_status().json()
        config = {"mode": mode}
        if mode == "delay":
            config["delay_seconds"] = processor_timeout + 1
        try:
            processor.put("/dev/failure", json=config).raise_for_status()
            response = game.post(
                f"/sessions/{session_id}/events",
                json={"event_type": "score_updated", "payload": {"score": 19}},
                headers={"X-Request-ID": request_id},
            )
            caller_finished_at = datetime.now(UTC)
        finally:
            processor.put("/dev/failure", json=original_mode).raise_for_status()

    if response.status_code != caller_status or response.headers.get("X-Request-ID") != request_id:
        raise RuntimeError(f"Expected caller HTTP {caller_status} and matching request ID, got {response.status_code}")
    return {
        "request_id": request_id,
        "session_id": session_id,
        "caller_status": response.status_code,
        "caller_finished_at": caller_finished_at.isoformat(),
    }


def check_persistence(incident: dict, writes: bool) -> dict | None:
    deadline = monotonic() + 6
    caller_finished_at = datetime.fromisoformat(incident["caller_finished_at"])
    with psycopg.connect(
        host="127.0.0.1",
        port=os.getenv("POSTGRES_HOST_PORT", "5433"),
        dbname=os.getenv("POSTGRES_DB", "tracepilot"),
        user=os.getenv("POSTGRES_USER", "tracepilot"),
        password=os.getenv("POSTGRES_PASSWORD", "tracepilot_dev"),
        connect_timeout=2,
    ) as connection:
        while True:
            rows = connection.execute(
                "SELECT id, created_at FROM game_events WHERE request_id = %s LIMIT 2",
                (incident["request_id"],),
            ).fetchall()
            if not writes:
                if rows:
                    raise RuntimeError("A write was recorded for a failed operation")
                return None
            if rows:
                if len(rows) != 1 or rows[0][1] <= caller_finished_at:
                    raise RuntimeError("Expected one write after the caller received HTTP 504")
                event_id, created_at = rows[0]
                return {"event_id": str(event_id), "created_at": created_at.isoformat()}
            if monotonic() >= deadline:
                raise RuntimeError("The timed-out request did not persist an event")
            sleep(0.2)


def _trace_matches(trace: dict, caller_log: dict, downstream_log: dict, mode: str) -> bool:
    spans = {span["span_id"]: span for span in trace["spans"]}
    caller = spans.get(caller_log.get("span_id"), {})
    downstream = spans.get(downstream_log.get("span_id"), {})
    client = spans.get(downstream.get("parent_span_id"), {})
    if not (
        caller.get("kind") == downstream.get("kind") == "server"
        and caller.get("http_status") == caller_log["status_code"]
        and downstream.get("http_status") == downstream_log["status_code"]
        and caller.get("service") == client.get("service") == "game-server"
        and downstream.get("service") == "event-processor"
        and client.get("kind") == "client"
        and client.get("parent_span_id") == caller["span_id"]
    ):
        return False

    operations = [span for span in spans.values() if span.get("parent_span_id") == downstream["span_id"]]
    if mode == "processor_error":
        return client.get("http_status") == 500 and any(
            span.get("error_type") == "SimulatedProcessorError" and span.get("status") == "error"
            for span in operations
        )
    if mode == "database_error":
        return any(
            span.get("db_system") == "postgresql" and span.get("sqlstate") == "22012"
            and span.get("error_type") == "DivisionByZero" and span.get("status") == "error"
            for span in operations
        )
    return (
        client.get("error_type") == "ReadTimeout"
        and any(span.get("failure_mode") == "delay" and span.get("delay_seconds", 0) > 0 for span in operations)
        and any(
            span.get("db_operation") == "INSERT" and span.get("db_collection") == "game_events"
            and span.get("status") in {"ok", "unset"}
            and span["start_offset_ms"] > caller["end_offset_ms"]
            for span in operations
        )
    )


async def verify_telemetry(incident: dict, mode: str, downstream_status: int, write: dict | None) -> dict:
    """Read the sources directly and match both completion logs to their trace spans."""
    deadline = monotonic() + 12
    port = int(os.getenv("JAEGER_PORT", "16686"))
    async with httpx.AsyncClient(trust_env=False) as client:
        while monotonic() < deadline:
            logs = await search_logs(SearchLogsArgs(request_id=incident["request_id"], limit=24))
            completions = {
                record["service"]: record
                for record in logs["records"] if record.get("event") == "request_completed"
            }
            caller = completions.get("game-server", {})
            downstream = completions.get("event-processor", {})
            trace_id = caller.get("trace_id")
            if (
                trace_id and trace_id == downstream.get("trace_id")
                and caller.get("status_code") == incident["caller_status"]
                and downstream.get("status_code") == downstream_status
                and (not write or downstream.get("event_id") == write["event_id"])
            ):
                trace_result = await get_traces(
                    GetTracesArgs(trace_id=trace_id), client=client, base_url=f"http://127.0.0.1:{port}",
                )
                for trace in trace_result["records"]:
                    if _trace_matches(trace, caller, downstream, mode):
                        return {
                            **incident, "trace_id": trace_id, "downstream_status": downstream["status_code"],
                            "write": write, "logs": logs["records"], "trace": trace,
                        }
            await asyncio.sleep(0.25)
    raise RuntimeError(f"Could not verify correlated logs and Jaeger spans for request {incident['request_id']}")
