import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from services.investigator.engine import ToolSpec, investigate
from services.investigator.models import IncidentInput, InvestigationResult

TRACE_ID = "12345678901234567890123456789012"


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str


def call(name, arguments):
    return {"function": {"name": name, "arguments": arguments}}


def assistant(*, content="", calls=None):
    return {"message": {"role": "assistant", "content": content, "tool_calls": calls}}


def findings(**changes):
    return assistant(content=json.dumps({
        "suspected_root_cause": "The processor likely exceeded the caller's deadline.",
        "confidence": "medium",
        "confirmed_observations": [{"observation": "The caller returned 504.", "evidence_ids": ["E1"]}],
        "supporting_evidence": ["E1"],
        "missing_information": [],
        "recommended_next_action": "Verify the operation outcome before retrying.",
        **changes,
    }))


def run(responses, tools=None, incident=None, **options):
    responses = iter(responses)
    requests = []

    def mock(request):
        assert request.url == "http://127.0.0.1:11434/api/chat"
        requests.append(json.loads(request.content))
        next_response = next(responses)
        if isinstance(next_response, Exception):
            raise next_response
        if isinstance(next_response, httpx.Response):
            return next_response
        return httpx.Response(200, json=next_response)

    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(mock)) as client:
            return await investigate(
                incident or IncidentInput(request_id="incident-1"),
                investigation_id=uuid4(), client=client, ollama_url="http://127.0.0.1:11434",
                tools=tools or {}, **options,
            )

    return asyncio.run(execute()), requests


def search_logs():
    async def retrieve(args):
        assert args.request_id == "incident-1"
        return {"status": "ok", "records": [
            {"request_id": "incident-1", "trace_id": TRACE_ID, "status_code": 504, "delay_seconds": 2},
        ]}

    return {"search_logs": ToolSpec(Args, retrieve, "Search bounded application logs")}


def test_ollama_native_tool_exchange_and_validated_findings():
    class TraceArgs(BaseModel):
        trace_id: str

    async def retrieve_trace(args):
        assert args.trace_id == TRACE_ID
        return {"status": "ok", "records": [
            {"trace_id": TRACE_ID, "spans": [
                {"operation": "INSERT game_events", "db_operation": "INSERT", "status": "unset"},
                {"operation": "simulate processor delay", "delay_seconds": 2},
            ]},
        ]}

    result, requests = run([
        assistant(calls=[call("search_logs", {"request_id": "incident-1"})]),
        assistant(calls=[call("get_traces", {"trace_id": TRACE_ID})]),
        findings(confidence="high", supporting_evidence=["E1", "E2"]),
    ], tools={**search_logs(), "get_traces": ToolSpec(TraceArgs, retrieve_trace, "Get one trace")})

    assert len(requests) == 3
    assert requests[0]["model"] == "qwen3:4b"
    assert requests[0]["stream"] is False
    assert requests[0]["think"] is True
    assert [tool["function"]["name"] for tool in requests[0]["tools"]] == ["search_logs", "get_traces"]
    previous_call, tool_reply = requests[1]["messages"][-2:]
    assert previous_call["tool_calls"][0]["function"]["name"] == "search_logs"
    assert tool_reply["role"] == "tool"
    assert tool_reply["tool_name"] == "search_logs"
    assert json.loads(tool_reply["content"])["records"][0]["id"] == "E1"
    synthesis = json.loads(requests[2]["messages"][1]["content"])
    assert [item["tool"] for item in synthesis["tool_results"]] == ["search_logs", "get_traces"]
    assert [item["records"][0]["id"] for item in synthesis["tool_results"]] == ["E1", "E2"]
    assert synthesis["verified_write_evidence_ids"] == ["E2"]
    assert result.status == "completed"
    assert result.confidence == "high"
    assert result.supporting_evidence == ["E1", "E2"]
    assert [record.id for record in result.evidence] == ["E1", "E2"]
    assert result.evidence[1].record["spans"][0]["operation"] == "INSERT game_events"
    assert requests[2]["think"] is False and "format" in requests[2]
    assert all(step.duration_ms >= 0 and step.status == "ok" for step in result.timeline)
    assert InvestigationResult.model_validate_json(result.model_dump_json()) == result


def test_tool_limit_counts_malformed_calls_and_reserves_final_turn():
    malformed = [call("shell", {"command": "forbidden"}), call("search_logs", {"other": 42})]
    result, requests = run([
        assistant(calls=malformed), assistant(calls=[call("search_logs", {"request_id": "incident-1"})]),
    ], tools=search_logs(), max_tool_calls=2)

    assert result.status == "partial"
    assert result.stop_reason == "tool_call_limit"
    assert [step.error for step in result.timeline] == ["Unknown diagnostic tool", "Invalid diagnostic tool arguments"]
    assert len(requests) == 2
    assert "tools" not in requests[1]
    assert requests[1]["format"]["type"] == "object"
    assert "forbidden" not in json.dumps(requests[1])


@pytest.mark.parametrize("response", [assistant(content="not JSON"), {"message": []}])
def test_malformed_model_reply_returns_partial(response):
    result, _ = run([response])
    assert result.status == "partial"
    assert result.stop_reason == "malformed_model_response"
    assert result.suspected_root_cause is None


@pytest.mark.parametrize("error", [httpx.ConnectError("private details"), httpx.Response(503)])
def test_ollama_failure_preserves_retrieved_evidence(error):
    result, _ = run([
        assistant(calls=[call("search_logs", {"request_id": "incident-1"})]), error,
    ], tools=search_logs())
    assert result.stop_reason == "model_unavailable"
    assert result.evidence[0].id == result.timeline[0].evidence_ids[0] == "E1"
    assert "private details" not in result.model_dump_json()


def test_deadline_cancels_a_slow_tool_and_records_partial_step():
    async def slow(_):
        await asyncio.sleep(60)

    result, _ = run(
        [assistant(calls=[call("search_logs", {"request_id": "incident-1"})])],
        tools={"search_logs": ToolSpec(Args, slow, "Slow tool")}, timeout_seconds=0.1,
    )
    assert result.status == "partial"
    assert result.stop_reason == "timeout"
    assert result.timeline[0].status == "error"
    assert result.evidence == []


@pytest.mark.parametrize("bad_field", ["cause", "observation"])
def test_unknown_evidence_references_are_never_returned(bad_field):
    changes = {"supporting_evidence": ["E9"]} if bad_field == "cause" else {
        "confirmed_observations": [{"observation": "Invented", "evidence_ids": ["E9"]}],
    }
    result, _ = run([
        assistant(calls=[call("search_logs", {"request_id": "incident-1"})]), findings(**changes),
    ], tools=search_logs())
    assert result.stop_reason == "invalid_evidence"
    assert result.suspected_root_cause is None
    assert result.supporting_evidence == result.confirmed_observations == []
    assert [item.id for item in result.evidence] == ["E1"]


@pytest.mark.parametrize("tool_name", ["query_metrics", "search_logs"])
def test_aggregate_or_unrelated_evidence_cannot_support_incident_cause(tool_name):
    async def retrieve(_):
        return {"status": "ok", "records": [{"request_id": "different-incident", "status_code": 504}]}

    result, _ = run([
        assistant(calls=[call(tool_name, {"request_id": "incident-1"})]), findings(confidence="high"),
    ], tools={tool_name: ToolSpec(Args, retrieve, "Get aggregate or unrelated evidence")})
    assert result.status == "insufficient_evidence"
    assert result.confidence == "undetermined"
    assert result.suspected_root_cause is None
    assert result.supporting_evidence == []
    assert result.missing_information


def test_tool_outage_and_mixed_response_preserve_errors_and_safe_records():
    async def unavailable(_):
        return {"status": "error", "records": [{"invented": True}], "error": "Metrics unavailable"}

    async def partial(_):
        return {"status": "ok", "records": [{"health": "ok\x00\ud800"}], "error": "Readiness unavailable"}

    tools = {
        "query_metrics": ToolSpec(Args, unavailable, "Query metrics"),
        "get_service_health": ToolSpec(Args, partial, "Check service probes"),
    }
    result, _ = run([
        assistant(calls=[call(name, {"request_id": "incident-1"}) for name in tools]),
        assistant(content='{"missing_information":["Readiness and metrics unavailable"]}'),
    ], tools=tools)
    assert result.status == "insufficient_evidence"
    assert [step.status for step in result.timeline] == ["error", "ok"]
    assert [step.error for step in result.timeline] == ["Metrics unavailable", "Readiness unavailable"]
    assert len(result.evidence) == 1
    assert result.evidence[0].record["health"] == "ok\ufffd?"
    assert result.recommended_next_action


def test_incident_input_requires_context_or_a_valid_identifier():
    assert IncidentInput(context="repeated timeouts").context == "repeated timeouts"
    assert IncidentInput(trace_id="AB" * 16).trace_id == "ab" * 16
    for fields in ({}, {"trace_id": "0" * 32}, {"request_id": "white space"}, {"description": "bad\x00text"}):
        with pytest.raises(ValidationError):
            IncidentInput(**fields)
