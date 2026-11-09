from uuid import UUID, uuid4

import httpx
import psycopg
from fastapi.testclient import TestClient

from services.investigator import main


def test_partial_investigation_is_saved_and_retrievable(monkeypatch):
    saved = {}
    initialized = []

    class Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            pass

        async def execute(self, query, params=None):
            if "CREATE TABLE IF NOT EXISTS investigations" in query:
                initialized.append(True)
            elif "INSERT INTO investigations" in query:
                assert "%s" in query
                investigation_id, result = params
                saved[investigation_id] = result.obj
            else:
                assert "WHERE id = %s" in query
                self.result = saved.get(params[0])
            return self

        async def fetchone(self):
            return {"result": self.result} if self.result is not None else None

    async def connect():
        return Connection()

    def ollama_unavailable(request):
        assert request.url.path == "/api/chat"
        raise httpx.ConnectError("local model unavailable", request=request)

    monkeypatch.setattr(main, "store_connection", connect)
    with TestClient(main.create_app(transport=httpx.MockTransport(ollama_unavailable))) as client:
        assert client.get("/health").status_code == 200
        assert client.post("/investigations", json={}).status_code == 422
        invalid = client.post(
            "/investigations", content='{"description":NaN}',
            headers={"Content-Type": "application/json"},
        )
        assert invalid.status_code == 422
        response = client.post("/investigations", json={"request_id": "known-request"})
        assert response.status_code == 201
        result = response.json()
        assert result["status"] == "partial"
        assert result["confidence"] == "undetermined"
        assert saved[UUID(result["id"])] == result
        assert client.get(f"/investigations/{result['id']}").json() == result
        assert client.get(f"/investigations/{uuid4()}").status_code == 404

    assert initialized == [True]


def test_storage_outage_is_reported_without_database_details(monkeypatch):
    async def initialize():
        pass

    async def unavailable():
        raise psycopg.OperationalError("private database details")

    monkeypatch.setattr(main, "initialize_store", initialize)
    monkeypatch.setattr(main, "store_connection", unavailable)
    with TestClient(main.create_app()) as client:
        response = client.get(f"/investigations/{uuid4()}")

    assert response.status_code == 503
    assert response.json() == {"detail": "Investigation storage unavailable"}
