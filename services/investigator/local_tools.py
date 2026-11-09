import asyncio
import json
import os
import re
import signal
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Literal

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from services.investigator.models import RequestId, TraceId

Service = Literal["game-server", "event-processor"]
Severity = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
SERVICES = ("game-server", "event-processor")
SEVERITIES = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOG_TAIL = 500
MAX_LOG_BYTES = 512 * 1024
LOG_TIMEOUT_SECONDS = 4
HEALTH_TIMEOUT_SECONDS = 2.5
MAX_WINDOW_MINUTES = 60
IDENTIFIER_PATTERNS = {
    "request_id": r"[!-~]{1,128}",
    "trace_id": r"[0-9a-f]{32}",
    "span_id": r"[0-9a-f]{16}",
    "session_id": r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}",
    "event_id": r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}",
}


class SearchLogsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: Service | None = Field(None, description="Service name. Omit to search both services.")
    severity: Severity | None = Field(None, description="Exact uppercase log level.")
    request_id: RequestId | None = Field(None, description="Exact request ID.")
    trace_id: TraceId | None = Field(None, description="Exact 32-character hexadecimal trace ID.")
    since_minutes: int = Field(
        15, ge=1, le=MAX_WINDOW_MINUTES,
        description="Lookback in minutes when since is omitted. Defaults to 15, maximum 60.",
    )
    since: AwareDatetime | None = Field(None, description="Start time with a timezone, in RFC 3339 format.")
    until: AwareDatetime | None = Field(
        None, description="End time with a timezone, in RFC 3339 format. Defaults to now."
    )
    limit: int = Field(
        16, ge=1, le=24, description="Newest matches, sorted by time. Defaults to 16, maximum 24."
    )


class ServiceHealthArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: Service | None = Field(None, description="Service name. Omit to check both services.")


class LogReadError(Exception):
    pass


async def _stop_log_process(process: asyncio.subprocess.Process) -> None:
    # Compose runs a CLI plugin. Stop the group so the plugin cannot keep the pipe open.
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    with suppress(TimeoutError):
        async with asyncio.timeout(0.5):
            while await process.stdout.read(32 * 1024):
                pass
            await process.wait()


async def _read_logs(
    services: tuple[str, ...], since: datetime, until: datetime
) -> tuple[bytes, bool]:
    process = None
    try:
        async with asyncio.timeout(LOG_TIMEOUT_SECONDS):
            process = await asyncio.create_subprocess_exec(
                "docker", "compose", "--file", str(PROJECT_ROOT / "docker-compose.yml"),
                "logs", "--no-color", "--no-log-prefix", "--tail", str(LOG_TAIL),
                "--since", since.isoformat(), "--until", until.isoformat(), *services,
                cwd=PROJECT_ROOT,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                capped = await process.stdout.readexactly(MAX_LOG_BYTES + 1)
            except asyncio.IncompleteReadError as exc:
                if await process.wait() != 0:
                    raise LogReadError
                return exc.partial, False
            return capped[:MAX_LOG_BYTES].rpartition(b"\n")[0], True
    finally:
        if process is not None and (process.returncode is None or not process.stdout.at_eof()):
            await _stop_log_process(process)


def _text(value: object, limit: int) -> str | None:
    if isinstance(value, str):
        return value[:limit].encode("utf-8", errors="replace").decode("utf-8")
    return None


def _safe_log_record(record: dict, timestamp: datetime) -> dict:
    result = {"timestamp": timestamp.isoformat(), "service": record["service"], "level": record["level"]}
    for key, pattern in IDENTIFIER_PATTERNS.items():
        value = record.get(key)
        if isinstance(value, str) and re.fullmatch(pattern, value):
            result[key] = value
    for key, limit in (("event", 240), ("path", 120), ("method", 16)):
        value = _text(record.get(key), limit)
        if value is not None:
            result[key] = value
    status = record.get("status_code")
    if type(status) is int and 100 <= status <= 599:
        result["status_code"] = status
    delay = record.get("delay_seconds")
    if type(delay) in (int, float) and 0 <= delay <= 60:
        result["delay_seconds"] = delay
    error = record.get("error")
    if isinstance(error, dict):
        fields = {
            key: value for key, limit in (("type", 80), ("sqlstate", 5))
            if (value := _text(error.get(key), limit)) is not None
        }
        if fields:
            result["error"] = fields
    elif (detail := _text(error, 120)) is not None:
        result["error"] = detail
    return result


async def search_logs(args: SearchLogsArgs) -> dict:
    try:
        until = args.until.astimezone(UTC) if args.until else datetime.now(UTC)
        since = (
            args.since.astimezone(UTC)
            if args.since else until - timedelta(minutes=args.since_minutes)
        )
    except (ValueError, OverflowError):
        return {"status": "error", "records": [], "error": "Invalid log time window"}
    if not timedelta() < until - since <= timedelta(minutes=MAX_WINDOW_MINUTES):
        return {"status": "error", "records": [], "error": "Log time window must be within 60 minutes"}

    services = (args.service,) if args.service else SERVICES
    try:
        output, capped = await _read_logs(services, since, until)
    except TimeoutError:
        return {"status": "error", "records": [], "error": "Docker log retrieval timed out"}
    except (OSError, LogReadError):
        return {"status": "error", "records": [], "error": "Docker Compose logs are unavailable"}

    matches = []
    lines = output.splitlines()
    for line in lines:
        if len(line) > 8 * 1024:
            capped = True
            continue
        try:
            record = json.loads(line)
            if not isinstance(record, dict):
                continue
            timestamp = datetime.fromisoformat(record["timestamp"])
            if timestamp.tzinfo is None or not since <= timestamp <= until:
                continue
        except (KeyError, TypeError, ValueError, RecursionError, OverflowError):
            continue
        if record.get("service") not in services or record.get("level") not in SEVERITIES:
            continue
        if args.severity and record["level"] != args.severity:
            continue
        if args.request_id and record.get("request_id") != args.request_id:
            continue
        if args.trace_id and record.get("trace_id") != args.trace_id:
            continue
        matches.append((timestamp, _safe_log_record(record, timestamp.astimezone(UTC))))

    matches.sort(key=lambda match: match[0])
    return {
        "status": "ok",
        "records": [record for _, record in matches[-args.limit:]],
        "truncated": capped or len(lines) >= LOG_TAIL or len(matches) > args.limit,
    }


async def _probe(
    client: httpx.AsyncClient, service: str, base_url: str, endpoint: str
) -> tuple[dict | None, str | None]:
    started = monotonic()
    try:
        async with asyncio.timeout(HEALTH_TIMEOUT_SECONDS):
            async with client.stream(
                "GET", base_url.rstrip("/") + endpoint, follow_redirects=False,
                timeout=HEALTH_TIMEOUT_SECONDS,
            ) as response:
                status = response.status_code
    except (httpx.HTTPError, TimeoutError) as exc:
        return None, f"{service} {endpoint} could not be reached ({type(exc).__name__})"
    return {
        "service": service,
        "endpoint": endpoint,
        "status_code": status,
        "ok": status == 200,
        "timestamp": datetime.now(UTC).isoformat(),
        "duration_ms": round((monotonic() - started) * 1000, 2),
    }, None


async def get_service_health(
    args: ServiceHealthArgs, *, client: httpx.AsyncClient, game_url: str, event_processor_url: str
) -> dict:
    urls = {"game-server": game_url, "event-processor": event_processor_url}
    services = (args.service,) if args.service else SERVICES
    observations = await asyncio.gather(*(
        _probe(client, service, urls[service], endpoint)
        for service in services for endpoint in ("/health", "/ready")
    ))
    records = [record for record, _ in observations if record is not None]
    errors = [error for _, error in observations if error is not None]
    result = {"status": "ok" if records else "error", "records": records}
    if errors:
        result["error"] = "; ".join(errors)
    return result
