from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from psycopg import OperationalError
from psycopg.errors import DivisionByZero, ForeignKeyViolation

from services.event_processor import main as processor
from services.game_server import main as game


class DatabaseStub:
    def __init__(self, *results):
        self.results = iter(results)
        self.calls = []

    async def connect(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        pass

    async def execute(self, sql, params=None):
        self.calls.append((sql, params))
        self.result = next(self.results)
        if isinstance(self.result, Exception):
            raise self.result
        if callable(self.result):
            self.result = self.result(params)
        return self

    async def fetchone(self):
        return self.result

    async def fetchall(self):
        return self.result


def game_app(handler=None):
    transport = httpx.MockTransport(handler) if handler else None
    return game.create_app(processor_transport=transport)


def session_row(player_id="player-42"):
    return {
        "id": uuid4(),
        "player_id": player_id,
        "status": "active",
        "created_at": datetime.now(UTC),
    }


def event_row(session_id, request_id, event_type="score_updated", payload=None):
    return {
        "id": uuid4(),
        "session_id": session_id,
        "event_type": event_type,
        "payload": {} if payload is None else payload,
        "request_id": request_id,
        "created_at": datetime.now(UTC),
    }


@pytest.mark.parametrize("service", [game, processor])
def test_health_and_database_readiness(monkeypatch, service):
    checks = []
    processor_checks = []
    expected_table = "game_sessions" if service is game else "game_events"

    async def database_ready(table):
        checks.append(table)
        if len(checks) == 2:
            raise OperationalError("database unavailable")

    def processor_ready(request):
        processor_checks.append((request.method, request.url.path))
        return httpx.Response(200, json={"status": "ready"})

    monkeypatch.setattr(service, "check_database", database_ready)
    app = game_app(processor_ready) if service is game else processor.create_app()
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
        assert checks == []

        ready = client.get("/ready")
        assert ready.status_code == 200
        assert ready.json()["status"] == "ready"
        assert client.get("/ready").status_code == 503

    assert checks == [expected_table, expected_table]
    assert processor_checks == ([("GET", "/ready")] if service is game else [])


def test_game_readiness_reports_unavailable_processor(monkeypatch):
    async def database_ready(table):
        pass

    def unavailable(request):
        assert request.url.path == "/ready"
        raise httpx.ConnectError("processor unavailable", request=request)

    monkeypatch.setattr(game, "check_database", database_ready)
    with TestClient(game_app(unavailable)) as client:
        assert client.get("/ready").status_code == 503
        assert client.get("/health").status_code == 200


def test_create_retrieve_and_count_sessions(monkeypatch):
    player_id = "player's account"
    row = session_row(player_id)
    counts = {"total_sessions": 2, "active_sessions": 1}
    db = DatabaseStub(row, row, None, counts)
    monkeypatch.setattr(game, "connect", db.connect)

    with TestClient(game_app()) as client:
        assert client.post("/sessions", json={"player_id": "   "}).status_code == 422
        created = client.post("/sessions", json={"player_id": player_id})
        assert created.status_code == 201
        session = created.json()
        assert session["id"] == str(row["id"])
        assert session["player_id"] == player_id
        assert session["status"] == "active"
        assert datetime.fromisoformat(session["created_at"]) == row["created_at"]

        retrieved = client.get(f"/sessions/{session['id']}")
        assert retrieved.status_code == 200
        assert retrieved.json() == session
        missing = uuid4()
        assert client.get(f"/sessions/{missing}").status_code == 404
        assert client.get("/sessions/not-a-uuid").status_code == 422
        assert client.get("/stats").json() == counts

    assert player_id not in db.calls[0][0]
    assert db.calls[0][1][1] == player_id
    assert db.calls[1][1] == (row["id"],)
    assert db.calls[2][1] == (missing,)


@pytest.mark.parametrize("request_id", ["test-correlation-id", None])
def test_game_forwards_event_and_request_id(monkeypatch, request_id):
    session_id = uuid4()

    def inserted_event(params):
        event_id, stored_session_id, event_type, payload, stored_request_id = params
        return {
            "id": event_id,
            "session_id": stored_session_id,
            "event_type": event_type,
            "payload": payload.obj,
            "request_id": stored_request_id,
            "created_at": datetime.now(UTC),
        }

    db = DatabaseStub(inserted_event)
    monkeypatch.setattr(processor, "connect", db.connect)
    transport = httpx.ASGITransport(app=processor.create_app(enable_failure_simulation=False))
    with TestClient(game.create_app(processor_transport=transport)) as client:
        response = client.post(
            f"/sessions/{session_id}/events",
            json={"event_type": "score_updated", "payload": {"score": 42}},
            headers={"X-Request-ID": request_id} if request_id else {},
        )

    assert response.status_code == 201
    assert len(db.calls) == 1
    _, params = db.calls[0]
    event = response.json()
    assert event["id"] == str(params[0])
    assert event["session_id"] == str(session_id)
    assert event["event_type"] == "score_updated"
    assert event["payload"] == {"score": 42}
    correlation_id = response.headers["X-Request-ID"]
    assert correlation_id == params[4] == event["request_id"]
    if request_id:
        assert correlation_id == request_id
    else:
        UUID(correlation_id)


@pytest.mark.parametrize(
    "body", [{}, {"event_type": "unknown"}, {"event_type": "player_joined", "payload": [1]}]
)
def test_both_services_reject_invalid_events(monkeypatch, body):
    session_id = uuid4()

    def unexpected_processor_call(request):
        pytest.fail("An invalid request reached the Event Processor")

    async def unexpected_database_call():
        pytest.fail("An invalid request reached the database")

    monkeypatch.setattr(processor, "connect", unexpected_database_call)
    with TestClient(game_app(unexpected_processor_call)) as client:
        assert client.post(f"/sessions/{session_id}/events", json=body).status_code == 422
    with TestClient(processor.create_app()) as client:
        response = client.post("/process-event", json={"session_id": str(session_id), **body})
        assert response.status_code == 422


@pytest.mark.parametrize(
    ("upstream_result", "status"),
    [(404, 404), (500, 502), (503, 503), (httpx.ReadTimeout, 504), (httpx.ConnectError, 503)],
)
def test_game_handles_processor_errors(upstream_result, status):
    def handler(request):
        if isinstance(upstream_result, int):
            return httpx.Response(upstream_result, json={"detail": "processor error"})
        raise upstream_result("processor request failed", request=request)

    with TestClient(game_app(handler)) as client:
        response = client.post(
            f"/sessions/{uuid4()}/events",
            json={"event_type": "player_joined"},
            headers={"X-Request-ID": "failed-event"},
        )
        assert response.status_code == status
        assert response.headers["X-Request-ID"] == "failed-event"
        assert client.get("/health").status_code == 200


@pytest.mark.parametrize(
    "event_type", ["player_joined", "player_left", "match_started", "score_updated", "match_ended"]
)
def test_processor_stores_event_and_reports_counts(monkeypatch, event_type):
    session_id = uuid4()
    request_id = "saved-event"
    payload = {"score": 42, "metadata": {"source": "test"}}
    row = event_row(session_id, request_id, event_type, payload)
    db = DatabaseStub(row, [{"event_type": event_type, "count": 1}])
    monkeypatch.setattr(processor, "connect", db.connect)

    with TestClient(processor.create_app()) as client:
        response = client.post(
            "/process-event",
            json={"session_id": str(session_id), "event_type": event_type, "payload": payload},
            headers={"X-Request-ID": request_id},
        )
        assert response.status_code == 201
        event = response.json()
        assert event["id"] == str(row["id"])
        assert event["session_id"] == str(session_id)
        assert event["event_type"] == event_type
        assert event["payload"] == payload
        assert event["request_id"] == response.headers["X-Request-ID"] == request_id
        assert datetime.fromisoformat(event["created_at"]) == row["created_at"]
        assert client.get("/stats").json() == {
            "total_events": 1,
            "events_by_type": {event_type: 1},
        }

    sql, params = db.calls[0]
    assert "INSERT INTO game_events" in sql
    assert str(session_id) not in sql
    assert session_id in params
    assert request_id in params


def test_processor_handles_invalid_and_missing_session(monkeypatch):
    db = DatabaseStub(ForeignKeyViolation("session missing"))
    monkeypatch.setattr(processor, "connect", db.connect)
    with TestClient(processor.create_app()) as client:
        invalid = {"session_id": "not-a-uuid", "event_type": "player_joined"}
        assert client.post("/process-event", json=invalid).status_code == 422
        missing = {**invalid, "session_id": str(uuid4())}
        assert client.post("/process-event", json=missing).status_code == 404

    assert len(db.calls) == 1


def test_game_handles_database_error(monkeypatch):
    async def unavailable():
        raise OperationalError("database unavailable")

    monkeypatch.setattr(game, "connect", unavailable)
    with TestClient(game_app()) as client:
        response = client.post("/sessions", json={"player_id": "player-42"})
        assert response.status_code == 503
        assert client.get("/health").status_code == 200


def test_development_failure_modes_are_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ENABLE_FAILURE_SIMULATION", raising=False)
    with TestClient(processor.create_app()) as client:
        assert client.get("/dev/failure").status_code == 404
        assert client.put("/dev/failure", json={"mode": "processor_error"}).status_code == 404


def test_processor_simulates_failures_and_recovers(monkeypatch):
    session_id = uuid4()
    body = {"session_id": str(session_id), "event_type": "player_joined"}
    db = DatabaseStub(
        DivisionByZero("division by zero"), event_row(session_id, "recovered", "player_joined")
    )
    monkeypatch.setattr(processor, "connect", db.connect)

    with TestClient(processor.create_app(enable_failure_simulation=True)) as client:
        assert client.get("/dev/failure").json()["mode"] == "none"
        assert client.put("/dev/failure", json={"mode": "invalid"}).status_code == 422
        assert client.put("/dev/failure", json={"mode": "processor_error"}).status_code == 200
        assert client.post("/process-event", json=body).status_code == 500
        assert db.calls == []

        assert client.put("/dev/failure", json={"mode": "database_error"}).status_code == 200
        assert client.post("/process-event", json=body).status_code == 503
        assert len(db.calls) == 1
        assert "INSERT" not in db.calls[0][0]

        assert client.put("/dev/failure", json={"mode": "none"}).status_code == 200
        assert client.post(
            "/process-event", json=body, headers={"X-Request-ID": "recovered"}
        ).status_code == 201
