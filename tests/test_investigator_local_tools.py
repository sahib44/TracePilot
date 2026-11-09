import asyncio
import json
import signal
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import ValidationError

from services.investigator import local_tools as tools


class LogProcess:
    pid = 1234

    def __init__(self, output=b"", exit_code=0):
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(output)
        self.returncode = exit_code
        if exit_code is not None:
            self.stdout.feed_eof()

    async def wait(self):
        return self.returncode


def test_search_logs_applies_exact_filters_and_preserves_chronology(monkeypatch):
    start = datetime(2026, 11, 7, 22, tzinfo=UTC)
    end = start + timedelta(minutes=15)
    trace_id = "ab" * 16
    record = {
        "timestamp": (start + timedelta(minutes=2)).isoformat(),
        "service": "event-processor",
        "level": "ERROR",
        "request_id": "request-42",
        "trace_id": trace_id,
        "event": "Event persistence failed",
        "error": {"type": "DivisionByZero", "sqlstate": "22012", "statement": "private-sql"},
        "payload": "private-body",
    }
    records = [
        record,
        {**record, "timestamp": start.isoformat(), "event": "Prior event"},
        {**record, "timestamp": (end + timedelta(seconds=1)).isoformat()},
        {**record, "timestamp": "bad timestamp"},
        {**record, "timestamp": start.replace(tzinfo=None).isoformat()},
        {**record, "service": "game-server"},
        {**record, "level": "WARNING"},
        {**record, "request_id": "different"},
        {**record, "trace_id": "cd" * 16},
        [],
    ]

    async def spawn(*argv, **kwargs):
        assert argv == (
            "docker", "compose", "--file", str(tools.PROJECT_ROOT / "docker-compose.yml"),
            "logs", "--no-color", "--no-log-prefix", "--tail", str(tools.LOG_TAIL),
            "--since", start.isoformat(), "--until", end.isoformat(), "event-processor",
        )
        assert kwargs["cwd"] == tools.PROJECT_ROOT
        assert kwargs["start_new_session"] is True
        output = b"Non-JSON line\n" + b"\n".join(json.dumps(item).encode() for item in records)
        return LogProcess(output)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    result = asyncio.run(tools.search_logs(tools.SearchLogsArgs(
        since=start, until=end, service="event-processor", severity="ERROR",
        request_id="request-42", trace_id=trace_id.upper(),
    )))

    assert result["status"] == "ok"
    assert result["truncated"] is False
    assert [item["event"] for item in result["records"]] == ["Prior event", "Event persistence failed"]
    assert all(item["trace_id"] == trace_id for item in result["records"])
    assert result["records"][0]["error"] == {"type": "DivisionByZero", "sqlstate": "22012"}
    assert "private-" not in json.dumps(result)


def test_log_limits_bound_results_and_stop_oversized_process(monkeypatch):
    timestamp = datetime.now(UTC).isoformat()
    lines = b"".join(
        json.dumps({"timestamp": timestamp, "service": "game-server", "level": "INFO", "event": str(i)}).encode()
        + b"\n"
        for i in range(3)
    )
    monkeypatch.setattr(tools, "MAX_LOG_BYTES", len(lines) + 5)
    spawned = []

    async def spawn(*args, **kwargs):
        process = LogProcess(lines + b"x" * 100, exit_code=None)
        spawned.append(process)
        return process

    def kill_group(pid, sig):
        assert (pid, sig) == (LogProcess.pid, signal.SIGKILL)
        spawned[0].returncode = -signal.SIGKILL
        spawned[0].stdout.feed_eof()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(tools.os, "killpg", kill_group)
    result = asyncio.run(tools.search_logs(tools.SearchLogsArgs(limit=1)))

    assert result["status"] == "ok"
    assert result["truncated"] is True
    assert [item["event"] for item in result["records"]] == ["2"]
    assert spawned[0].returncode == -signal.SIGKILL


def test_log_timeout_cleans_up_compose_process(monkeypatch):
    monkeypatch.setattr(tools, "LOG_TIMEOUT_SECONDS", 0.01)
    spawned = []

    async def spawn(*args, **kwargs):
        process = LogProcess(exit_code=None)
        spawned.append(process)
        return process

    def kill_group(pid, sig):
        assert (pid, sig) == (LogProcess.pid, signal.SIGKILL)
        spawned[0].returncode = -signal.SIGKILL
        spawned[0].stdout.feed_eof()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(tools.os, "killpg", kill_group)

    assert asyncio.run(tools.search_logs(tools.SearchLogsArgs())) == {
        "status": "error", "records": [], "error": "Docker log retrieval timed out",
    }
    assert spawned[0].returncode == -signal.SIGKILL


def test_log_failures_and_malformed_args_return_no_evidence(monkeypatch):
    with pytest.raises(ValidationError):
        tools.SearchLogsArgs(service="--follow", command="arbitrary command")
    with pytest.raises(ValidationError):
        tools.SearchLogsArgs(limit=999, trace_id="not-a-trace")

    async def spawn(*args, **kwargs):
        return LogProcess(b"private Docker error", exit_code=1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    assert asyncio.run(tools.search_logs(tools.SearchLogsArgs())) == {
        "status": "error", "records": [], "error": "Docker Compose logs are unavailable",
    }


def test_health_checks_fixed_endpoints_and_distinguishes_http_from_transport_failures():
    calls = []

    def respond(request):
        assert request.method == "GET"
        calls.append((request.url.port, request.url.path))
        if request.url.port == 8001 and request.url.path == "/ready":
            raise httpx.ConnectError("private error message", request=request)
        if request.url.port == 8000 and request.url.path == "/ready":
            return httpx.Response(503, json={"detail": "private response body"})
        return httpx.Response(200, json={"status": "ok"})

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await tools.get_service_health(
                tools.ServiceHealthArgs(), client=client,
                game_url="http://127.0.0.1:8000", event_processor_url="http://127.0.0.1:8001",
            )

    result = asyncio.run(check())
    assert set(calls) == {(port, path) for port in (8000, 8001) for path in ("/health", "/ready")}
    assert result["status"] == "ok"
    assert len(result["records"]) == 3
    assert "ConnectError" in result["error"]
    readiness = next(item for item in result["records"] if item["endpoint"] == "/ready")
    assert readiness["status_code"] == 503
    assert readiness["ok"] is False
    assert "private" not in json.dumps(result)


def test_health_timeout_returns_an_error_without_evidence(monkeypatch):
    monkeypatch.setattr(tools, "HEALTH_TIMEOUT_SECONDS", 0.01)
    calls = []

    async def respond(request):
        calls.append(request.url.port)
        await asyncio.Event().wait()

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await tools.get_service_health(
                tools.ServiceHealthArgs(service="game-server"), client=client,
                game_url="http://127.0.0.1:8000", event_processor_url="http://127.0.0.1:8001",
            )

    result = asyncio.run(check())
    assert calls == [8000, 8000]
    assert result["status"] == "error"
    assert result["records"] == []
    assert "TimeoutError" in result["error"]
