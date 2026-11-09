from datetime import UTC, datetime
from uuid import UUID

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from services.investigator import main


SUMMARY = {
    "id": UUID("6cb9bc48-f86e-480e-92c1-f8169ae74873"),
    "incident": {"description": "Investigate the game event", "request_id": "known-request"},
    "status": "partial",
    "started_at": datetime(2026, 11, 9, tzinfo=UTC),
    "suspected_root_cause": None,
    "confidence": "undetermined",
    "duration_ms": 1000,
}


@pytest.fixture
def initialized(monkeypatch):
    async def initialize():
        pass

    monkeypatch.setattr(main, "initialize_store", initialize)
    monkeypatch.setenv("GAME_SERVER_PORT", "8000")
    monkeypatch.setenv("EVENT_PROCESSOR_PORT", "8001")


def test_history_uses_literal_search_parameters_and_reads_only_summary_columns(monkeypatch, initialized):
    term = "x_%' OR 1=1 --"
    calls = []

    class Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            pass

        async def execute(self, query, params):
            assert term not in query
            assert "strpos(lower(" in query
            assert "ORDER BY started_at ASC, id ASC" in query
            assert "LIMIT %s OFFSET %s" in query
            assert "result->'incident' AS incident" in query
            assert "evidence" not in query
            assert params[:5] == ("partial", "partial", term, term, 2)
            calls.append(params[-1])
            return self

        async def fetchall(self):
            if calls[-1] == 6:
                return [{"total": 7, **SUMMARY}]
            return [{"total": 7, "id": None}]

    async def connect():
        return Connection()

    monkeypatch.setattr(main, "store_connection", connect)
    with TestClient(main.create_app()) as client:
        params = {"limit": 2, "offset": 6, "search": term, "status": "partial", "sort": "oldest"}
        result = client.get("/investigations", params=params).raise_for_status().json()
        assert result["total"] == 7
        assert result["limit"] == 2
        assert result["offset"] == 6
        item, = result["items"]
        assert set(item) == set(SUMMARY)
        assert item["id"] == str(SUMMARY["id"])
        assert item["incident"]["request_id"] == "known-request"
        params["offset"] = 8
        empty = client.get("/investigations", params=params).raise_for_status().json()
        assert empty == {"items": [], "total": 7, "limit": 2, "offset": 8}


@pytest.mark.parametrize("path", [
    "/investigations?limit=51",
    "/investigations?offset=10001",
    "/investigations?status=running",
    "/investigations?sort=created_at",
    "/investigations?search=%00",
    "/dashboard?window=1440",
    "/dashboard?query=up",
])
def test_dashboard_and_history_reject_unbounded_or_unsupported_inputs(path, initialized):
    with TestClient(main.create_app()) as client:
        assert client.get(path).status_code == 422


@pytest.mark.parametrize(("window", "step"), [(15, 15), (60, 60)])
def test_dashboard_reports_real_health_snapshots_and_range_gaps(monkeypatch, initialized, window, step):
    async def recent():
        return [main.InvestigationSummary.model_validate(SUMMARY)], None

    def monitoring(request):
        path = request.url.path
        if path in ("/health", "/ready"):
            return httpx.Response(503 if request.url.port == 8001 and path == "/ready" else 200)
        assert path == "/api/v1/query_range"
        params = request.url.params
        start, end = int(params["start"]), int(params["end"])
        assert end - start == window * 60
        assert params["step"] == str(step)
        assert params["limit"] == "2"
        assert params["timeout"] == "2s"
        query = params["query"]
        assert 'job="tracepilot"' in query
        assert 'route!~"/(health|ready)"' in query
        assert "[5m]" in query
        if "http_requests_total" in query:
            assert "rate(" in query
            series = [{"metric": {"service": "game-server"}, "values": [[start, "0"], [end, "1.2"]]}]
        elif "http_request_errors_total" in query:
            assert 'status_code=~"5.."' in query
            series = [{"metric": {"service": "game-server"}, "values": [[end, "NaN"]]}]
        else:
            assert query.startswith("histogram_quantile(0.95,")
            series = [{"metric": {"service": "game-server"}, "values": [[end - step, "0.08"]]}]
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "matrix", "result": series}})

    monkeypatch.setattr(main, "recent_results", recent)
    with TestClient(main.create_app(transport=httpx.MockTransport(monitoring))) as client:
        result = client.get("/dashboard", params={"window": window}).raise_for_status().json()
    assert result["window_minutes"] == window
    assert result["step_seconds"] == step
    assert result["metric_errors"] == {}
    assert result["health_error"] is None
    assert result["recent_error"] is None
    assert result["recent_investigations"][0]["id"] == str(SUMMARY["id"])
    game, processor = result["services"]
    assert game["service"] == "game-server"
    assert game["liveness"]["ok"] and game["readiness"]["ok"]
    assert processor["service"] == "event-processor"
    assert processor["liveness"]["ok"] and not processor["readiness"]["ok"]
    assert processor["readiness"]["status_code"] == 503
    empty = {"value": None, "points": []}
    assert processor["metrics"] == dict.fromkeys(("throughput", "error_rate", "latency"), empty)
    assert game["metrics"]["error_rate"] == empty
    throughput = game["metrics"]["throughput"]
    assert throughput["value"] == 1.2
    assert len(throughput["points"]) == 61
    assert [point["value"] for point in throughput["points"][:2]] == [0.0, None]
    end = int(datetime.fromisoformat(result["observed_at"]).timestamp())
    assert throughput["points"][-1] == {"timestamp": end, "value": 1.2}
    assert game["metrics"]["latency"]["value"] is None
    assert game["metrics"]["latency"]["points"][-2]["value"] == 0.08


def test_outages_are_isolated_and_errors_hide_upstream_details(monkeypatch, initialized):
    async def unavailable():
        raise psycopg.OperationalError("private database details")

    def monitoring(request):
        if request.url.path == "/api/v1/query_range":
            return httpx.Response(503, text="private prometheus details")
        if request.url.port == 8001:
            raise httpx.ConnectError("private service details")
        return httpx.Response(200)

    monkeypatch.setattr(main, "store_connection", unavailable)
    with TestClient(main.create_app(transport=httpx.MockTransport(monitoring))) as client:
        dashboard = client.get("/dashboard")
        history = client.get("/investigations")
    result = dashboard.raise_for_status().json()
    assert "private" not in dashboard.text
    assert result["metric_errors"] == dict.fromkeys(("throughput", "error_rate", "latency"), "Prometheus returned HTTP 503.")
    assert result["recent_error"] == "Investigation storage unavailable"
    assert result["recent_investigations"] == []
    assert "event-processor /health could not be reached" in result["health_error"]
    game, processor = result["services"]
    assert game["liveness"]["ok"]
    assert processor["liveness"] is None and processor["readiness"] is None
    assert history.status_code == 503
    assert history.json() == {"detail": "Investigation storage unavailable"}
