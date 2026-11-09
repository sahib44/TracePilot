"""Exercise the local Qwen model and Compose stack when explicitly enabled."""

import asyncio
import json
import os
import re
from time import sleep

import pytest

from evaluation.incidents import DESCRIPTION, check_persistence, local_client, trigger_incident
from services.investigator.local_tools import SearchLogsArgs, search_logs

pytestmark = pytest.mark.skipif(
    os.getenv("TRACEPILOT_LIVE_INVESTIGATOR") != "1",
    reason="requires the local investigator, Ollama and the Compose stack",
)


@pytest.mark.parametrize(
    ("mode", "status_code"), [("processor_error", 502), ("database_error", 503), ("delay", 504)]
)
def test_qwen_investigates_a_real_incident(mode, status_code):
    observed = trigger_incident(mode, status_code)
    request_id = observed["request_id"]
    check_persistence(observed, writes=mode == "delay")
    with local_client("INVESTIGATOR_PORT", 8002, timeout=330) as investigator:
        # Give the existing OTLP batch exporter a chance to flush before the model queries Jaeger.
        sleep(1)
        incident = {"description": DESCRIPTION}
        if mode != "database_error":
            incident["request_id"] = request_id
        if mode != "processor_error":
            logs = asyncio.run(search_logs(SearchLogsArgs(request_id=request_id)))
            assert logs["status"] == "ok"
            incident["trace_id"] = next(record["trace_id"] for record in logs["records"] if record.get("trace_id"))
        result = investigator.post("/investigations", json=incident).raise_for_status().json()

        print(json.dumps({
            "mode": mode,
            "request_id": request_id,
            "id": result["id"],
            "status": result["status"],
            "stop_reason": result["stop_reason"],
            "duration_ms": result["duration_ms"],
            "suspected_root_cause": result["suspected_root_cause"],
            "confidence": result["confidence"],
            "timeline": result["timeline"],
            "confirmed_observations": result["confirmed_observations"],
        }), flush=True)
        assert result["status"] == "completed"
        assert result["supporting_evidence"]
        assert any(
            (evidence["tool"] == "search_logs" and evidence["record"].get("request_id") == request_id)
            or (evidence["tool"] == "get_traces" and evidence["record"].get("trace_id") == incident.get("trace_id"))
            for evidence in result["evidence"]
        )
        retrieved = investigator.get(f"/investigations/{result['id']}").raise_for_status().json()
        assert retrieved == result

        if mode == "delay":
            observations = [item["observation"].lower() for item in result["confirmed_observations"]]
            assert any(
                re.search(r"\b(committed|persisted|inserted|wrote|written|saved)\b", observation)
                or (
                    re.search(r"\b(write|writing|insert|database operation)\b", observation)
                    and re.search(r"\b(completed|succeeded|successful|successfully)\b", observation)
                )
                for observation in observations
            )
            findings = json.dumps({
                "suspected_root_cause": result["suspected_root_cause"],
                "confirmed_observations": result["confirmed_observations"],
            }).lower()
            assert "504" in findings or "timed out" in findings or "timeout" in findings
            assert not any(text in findings for text in (
                "did not commit", "never committed", "was rolled back", "did not persist",
            ))
