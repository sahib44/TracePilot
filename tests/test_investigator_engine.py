import asyncio
import json
import logging
from uuid import uuid4

import httpx
import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from services.investigator.engine import ToolSpec, investigate
from services.investigator.models import IncidentInput, InvestigationResult
from services.investigator.monitoring import QueryMetricsArgs

TRACE_ID = "12345678901234567890123456789012"


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str


def call(name, arguments):
    return {"function": {"name": name, "arguments": arguments}}


def assistant(*, content="", calls=None, **completion):
    return {"message": {"role": "assistant", "content": content, "tool_calls": calls}, **completion}


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

    async def mock(request):
        assert request.url == "http://127.0.0.1:11434/api/chat"
        requests.append(json.loads(request.content))
        next_response = next(responses)
        if callable(next_response):
            next_response = await next_response()
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
    assert requests[0]["think"] is False
    assert [tool["function"]["name"] for tool in requests[0]["tools"]] == ["search_logs", "get_traces"]
    previous_call, tool_reply = requests[1]["messages"][-2:]
    assert requests[1]["think"] is True
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
    result, requests = run([response])
    assert len(requests) == 1
    assert result.status == "partial"
    assert result.stop_reason == "malformed_model_response"
    assert result.suspected_root_cause is None


@pytest.mark.parametrize("completion", [
    {"done_reason": "length", "done": True, "eval_count": 1024},
    {"done": True, "eval_count": 1024},
    {"done": False, "eval_count": 80},
])
def test_incomplete_first_turn_recovers_without_accepting_findings(completion, caplog):
    with caplog.at_level(logging.INFO, logger="tracepilot"):
        result, requests = run([
            assistant(content='{"missing_information":["Premature findings"]}', **completion),
            assistant(calls=[call("search_logs", {"request_id": "incident-1"})]),
            findings(),
        ], tools=search_logs(), max_tool_calls=1)

    first, recovery, synthesis = requests
    assert first["think"] is recovery["think"] is False
    assert first["tools"] == recovery["tools"]
    assert "format" not in first
    assert recovery["format"]["properties"]["name"]["enum"] == ["search_logs"]
    assert first["options"]["num_predict"] == 1024
    assert recovery["options"]["num_predict"] <= first["options"]["num_predict"]
    assert first["messages"][0] != recovery["messages"][0]
    assert first["messages"][1] == recovery["messages"][1]
    assert first["messages"][-1] == recovery["messages"][-1] == {
        "role": "assistant", "content": "<think>\n\n</think>\n\n",
    }
    assert "Premature findings" not in json.dumps(recovery["messages"])
    assert "tools" not in synthesis
    assert result.status == "completed"
    assert [step.tool for step in result.timeline] == ["search_logs"]
    assert [record.id for record in result.evidence] == ["E1"]
    assert f"eval_count={completion['eval_count']}" in caplog.text
    assert f"done={completion['done']}".lower() in caplog.text.lower()
    assert "retry_outcome=scheduled" in caplog.text
    assert "retry_outcome=succeeded" in caplog.text
    assert f"stage={'generation_limit' if completion['done'] else 'incomplete_response'}" in caplog.text
    if completion.get("done_reason") == "length":
        assert "done_reason=length" in caplog.text


def test_explicit_completion_at_token_budget_preserves_valid_findings():
    result, requests = run([
        assistant(content='{"missing_information":["No direct evidence"]}',
                  done_reason="stop", done=True, eval_count=1024),
    ], tools=search_logs())

    assert len(requests) == 1
    assert result.status == "insufficient_evidence"
    assert result.stop_reason is None
    assert result.missing_information == ["No direct evidence"]


@pytest.mark.parametrize("initial_reply", [
    assistant(content="  "),
    {"message": []},
    assistant(calls={"function": {"name": "search_logs"}}),
    assistant(calls=[{"function": None}]),
    assistant(calls=[call("search_logs", '{"request_id":"incident-1"}')]),
])
def test_invalid_initial_reply_gets_exactly_one_recovery_attempt(initial_reply):
    result, requests = run([
        initial_reply,
        assistant(content=""),
    ], tools=search_logs())

    assert len(requests) == 2
    assert result.status == "partial"
    assert result.stop_reason == "malformed_model_response"
    assert result.timeline == result.evidence == []
    assert "retry" in " ".join(result.missing_information).lower()


@pytest.mark.parametrize("unusable_reply", [
    assistant(content='{"missing_information":["No evidence"]}'),
    assistant(content='{"name":"search_logs","arguments":{},"extra":true}'),
    assistant(content='{"name":"unknown_tool","arguments":{}}'),
    assistant(calls=[call("unknown_tool", {})]),
    assistant(calls=[call("search_logs", {"other": 42})]),
])
def test_recovery_requires_a_usable_diagnostic_tool_call(unusable_reply, caplog):
    result, requests = run([assistant(), unusable_reply], tools=search_logs())

    assert len(requests) == 2
    assert result.status == "partial"
    assert result.stop_reason == "malformed_model_response"
    assert result.timeline == result.evidence == []
    assert "usable diagnostic tool" in result.missing_information[0]
    assert "stage=tool_selection" in caplog.text
    assert "retry_outcome=failed" in caplog.text


def test_description_only_incident_recovers_with_a_structured_metric_selection():
    async def retrieve(args):
        assert args.metric == "error_rate"
        return {"status": "ok", "records": [{"service": "game-server", "value": 1}]}

    result, requests = run([
        assistant(done_reason="length", done=True, eval_count=1024),
        assistant(content='{"name":"query_metrics","arguments":{"metric":"error_rate"}}'),
        assistant(content='{"missing_information":["No correlated request or trace"]}'),
    ], tools={"query_metrics": ToolSpec(QueryMetricsArgs, retrieve, "Query metrics")},
        incident=IncidentInput(description="Investigate recent HTTP 5xx errors"), max_tool_calls=1)

    assert requests[0]["think"] is requests[1]["think"] is False
    assert result.status == "insufficient_evidence"
    assert result.stop_reason is None
    assert [step.tool for step in result.timeline] == ["query_metrics"]
    assert result.timeline[0].status == "ok"
    assert result.evidence[0].record["value"] == 1


def test_recovery_failure_logs_metadata_without_untrusted_text(caplog):
    incident_secret = "private-incident-secret"
    model_secret = "unrestricted-model-reply"
    completion_secret = "untrusted-completion-reason"
    with caplog.at_level(logging.INFO, logger="tracepilot"):
        result, _ = run([
            assistant(content=model_secret, done_reason=completion_secret, done=True, eval_count=37),
            assistant(calls={"secret": model_secret}, done_reason="stop", done=True, eval_count=29),
        ], tools=search_logs(), incident=IncidentInput(description=incident_secret))

    assert result.status == "partial"
    assert "stage=findings" in caplog.text
    assert "stage=tool_calls" in caplog.text
    assert "done_reason=stop" in caplog.text
    assert "eval_count=37" in caplog.text
    assert "eval_count=29" in caplog.text
    assert "retry_outcome=failed" in caplog.text
    for secret in (incident_secret, model_secret, completion_secret):
        assert secret not in caplog.text
        assert secret not in " ".join(result.missing_information)


def test_final_findings_with_token_limit_are_rejected_and_evidence_is_preserved():
    result, requests = run([
        assistant(calls=[call("search_logs", {"request_id": "incident-1"})]),
        {**findings(), "done": True, "done_reason": "length", "eval_count": 512},
    ], tools=search_logs(), max_tool_calls=1)

    assert len(requests) == 2
    assert result.status == "partial"
    assert result.suspected_root_cause is None
    assert [record.id for record in result.evidence] == ["E1"]


def test_recovery_dispatch_respects_tool_call_limit():
    log_call = call("search_logs", {"request_id": "incident-1"})
    result, requests = run([
        assistant(content=""),
        assistant(calls=[log_call, log_call]),
        assistant(calls=[log_call]),
    ], tools=search_logs(), max_tool_calls=1)

    assert len(requests) == 3
    assert "tools" not in requests[2]
    assert result.stop_reason == "tool_call_limit"
    assert [step.tool for step in result.timeline] == ["search_logs"]
    assert [record.id for record in result.evidence] == ["E1"]


def test_recovery_shares_the_investigation_deadline():
    cancelled = []

    async def slow_initial_reply():
        await asyncio.sleep(0.1)
        return assistant(content="")

    async def stalled_recovery():
        try:
            await asyncio.sleep(0.15)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return assistant(calls=[call("search_logs", {"request_id": "incident-1"})])

    result, requests = run([
        slow_initial_reply,
        stalled_recovery,
    ], tools=search_logs(), timeout_seconds=0.2)

    assert len(requests) == 2
    assert cancelled == [True]
    assert result.status == "partial"
    assert result.stop_reason == "timeout"
    assert result.timeline == result.evidence == []


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
