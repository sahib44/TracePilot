from copy import deepcopy

import pytest

from evaluation.criteria import SCENARIOS, grade


REQUEST_ID = "eval-request"
TRACE_ID = "a" * 32


def result(cause, observations, records, *, supporting=None):
    evidence = [
        {"id": f"E{index}", "tool": tool, "record": record}
        for index, (tool, record) in enumerate(records, start=1)
    ]
    return {
        "status": "completed",
        "suspected_root_cause": cause,
        "confirmed_observations": [
            {"observation": text, "evidence_ids": refs} for text, refs in observations
        ],
        "supporting_evidence": supporting or ["E1"],
        "evidence": evidence,
        "timeline": [
            {"tool": item["tool"], "status": "ok", "evidence_ids": [item["id"]]} for item in evidence
        ],
        "duration_ms": 750,
    }


def score(mode, investigation, **kwargs):
    return grade(SCENARIOS[mode], investigation, request_id=REQUEST_ID, trace_id=TRACE_ID, **kwargs)


def trace(*spans):
    return {
        "trace_id": TRACE_ID,
        "started_at": "2026-01-01T00:00:00Z",
        "duration_ms": 2050,
        "span_count": len(spans),
        "spans": list(spans),
    }


def log(service, event, **fields):
    return {"service": service, "event": event, "request_id": REQUEST_ID, "trace_id": TRACE_ID, **fields}


def test_processor_logs_distinguish_internal_error_from_propagated_status():
    logs = [
        log("event-processor", "Simulating processor failure", error={"type": "SimulatedProcessorError"}),
        log("event-processor", "request_completed", status_code=500),
        log("game-server", "request_completed", status_code=502),
    ]
    investigation = result(
        "Simulated processor failure in event-processor",
        [
            ("The processor returned HTTP 500.", ["E2"]),
            ("The caller returned a 502.", ["E3"]),
        ],
        [("search_logs", record) for record in logs],
    )
    verdict = score("processor_error", investigation, ground_truth={"logs": logs})
    assert not verdict["failures"]
    assert verdict["tool_calls"] == verdict["successful_tool_calls"] == 3
    assert verdict["duration_ms"] == 750

    # A symptom and a perfectly valid citation cannot make the suspected cause correct.
    investigation["suspected_root_cause"] = "502 Bad Gateway"
    assert not score("processor_error", investigation)["root_cause_correct"]
    investigation["suspected_root_cause"] = "There was no simulated processor failure"
    assert not score("processor_error", investigation)["root_cause_correct"]


def test_postgresql_cause_requires_specific_error_and_snapshot_agreement():
    retrieved = trace(
        {"service": "game-server", "kind": "server", "http_status": 503},
        {
            "service": "event-processor", "db_system": "postgresql", "db_operation": "SELECT",
            "status": "error", "error_type": "DivisionByZero", "sqlstate": "22012",
        },
    )
    investigation = result(
        "Division by zero in PostgreSQL during SELECT",
        [("The request returned 503.", ["E1"]), ("The database raised SQLSTATE 22012.", ["E1"])],
        [("get_traces", retrieved)],
    )
    assert not score("database_error", investigation, ground_truth={"trace": retrieved})["failures"]
    assert not score(
        "database_error", {**investigation, "suspected_root_cause": "Database unavailable"}
    )["root_cause_correct"]

    forged = deepcopy(investigation)
    forged["evidence"][0]["record"]["spans"][1]["sqlstate"] = "ZZ999"
    verdict = score("database_error", forged, ground_truth={"trace": retrieved})
    assert verdict["invalid_citation_ids"] == ["E1"]
    assert not verdict["citations_valid"]
    assert not verdict["supporting_evidence_correct"]

    invented_write = deepcopy(investigation)
    invented_write["confirmed_observations"].append({
        "observation": "Write operation completed successfully (no write failure observed)",
        "evidence_ids": ["E1"],
    })
    verdict = score("database_error", invented_write)
    assert verdict["citations_valid"]
    assert not verdict["observations_grounded"]
    assert verdict["observation_checks"][-1]["unsupported"] == ["write_completed"]


@pytest.mark.parametrize("observation", ["Event submission failed.", "Caller failed to submit event."])
def test_generic_submission_failure_is_grounded_without_satisfying_numeric_outcome(observation):
    retrieved = trace(
        {"service": "game-server", "kind": "server", "http_status": 502},
        {"service": "event-processor", "kind": "server", "http_status": 500},
        {"service": "event-processor", "error_type": "SimulatedProcessorError"},
    )
    investigation = result(
        "Simulated processor failure",
        [(observation, ["E1"]), ("Event processor returned 500.", ["E1"])],
        [("get_traces", retrieved)],
    )
    verdict = score("processor_error", investigation)
    assert verdict["observations_grounded"]
    assert verdict["missing_outcome_claims"] == ["http:game-server:502"]
    assert not verdict["outcome_correct"]


def test_generic_downstream_failure_does_not_satisfy_numeric_outcome():
    retrieved = trace(
        {"service": "game-server", "kind": "server", "http_status": 502},
        {"service": "event-processor", "kind": "server", "http_status": 500},
        {"service": "event-processor", "error_type": "SimulatedProcessorError"},
    )
    investigation = result(
        "Simulated processor failure",
        [("Caller returned 502.", ["E1"]), ("Event processor service failed to process event.", ["E1"])],
        [("get_traces", retrieved)],
    )
    verdict = score("processor_error", investigation)
    assert verdict["observations_grounded"]
    assert verdict["missing_outcome_claims"] == ["http:event-processor:500"]


def test_generic_downstream_success_requires_cited_status_and_claimed_duration():
    retrieved = trace(
        {
            "service": "event-processor", "kind": "server",
            "http_status": 201, "duration_ms": 2039.886,
        },
    )
    investigation = result(
        "Event processor delay",
        [("event-processor POST /process-event succeeded (2039.886ms)", ["E1"])],
        [("get_traces", retrieved)],
    )
    verdict = score("delay", investigation)
    assert verdict["observations_grounded"]
    retrieved["spans"][0]["duration_ms"] = 15
    verdict = score("delay", investigation)
    assert verdict["observation_checks"][0]["unsupported"] == ["downstream_duration_ms:2039.886"]


def test_timeout_requires_separate_grounded_caller_and_write_outcomes():
    retrieved = trace(
        {"service": "game-server", "kind": "server", "http_status": 504, "end_offset_ms": 1000},
        {"service": "game-server", "kind": "client", "error_type": "ReadTimeout"},
        {"service": "event-processor", "failure_mode": "delay", "delay_seconds": 2},
        {
            "service": "event-processor", "db_operation": "INSERT", "status": "unset", "end_offset_ms": 2000,
        },
        {"service": "event-processor", "kind": "server", "http_status": 201},
    )
    investigation = result(
        "Downstream operation delay in event-processor",
        [
            ("Caller (game-server) failed to submit event due to 504 timeout.", ["E1"]),
            ("The database write completed after the 504.", ["E1"]),
        ],
        [("get_traces", retrieved)],
    )
    assert not score("delay", investigation, ground_truth={"trace": retrieved})["failures"]
    assert not score(
        "delay", {**investigation, "suspected_root_cause": "ReadTimeout in game-server"}
    )["root_cause_correct"]
    early_write = deepcopy(investigation)
    early_write["confirmed_observations"][1]["observation"] = "The database write completed."
    early_write["evidence"][0]["record"]["spans"][3]["end_offset_ms"] = 900
    verdict = score("delay", early_write)
    assert verdict["observations_grounded"]
    assert verdict["missing_outcome_claims"] == ["late_write"]
    assert not verdict["outcome_correct"]

    bad_observation = deepcopy(investigation)
    bad_observation["confirmed_observations"][1]["observation"] = "The insert was rolled back."
    verdict = score("delay", bad_observation)
    assert verdict["citations_valid"]
    assert not verdict["observations_grounded"]
    assert not verdict["outcome_correct"]

    hypothetical = deepcopy(investigation)
    hypothetical["confirmed_observations"].append({
        "observation": "The write likely completed.", "evidence_ids": ["E1"],
    })
    assert not score("delay", hypothetical)["observations_separated"]


@pytest.mark.parametrize("defect", ["invented", "unissued", "foreign", "aggregate", "unsupported_status"])
def test_citation_checks_separate_provenance_and_support(defect):
    investigation = result(
        "Simulated processor failure",
        [("The processor returned HTTP 500.", ["E1"])],
        [("search_logs", log("event-processor", "request_completed", status_code=500))],
    )
    if defect == "invented":
        investigation["supporting_evidence"] = ["E9"]
    if defect == "unissued":
        investigation["timeline"][0]["status"] = "error"
    if defect == "foreign":
        investigation["evidence"][0]["record"]["request_id"] = "unrelated"
    if defect == "aggregate":
        investigation["evidence"][0]["tool"] = "query_metrics"
        investigation["timeline"][0]["tool"] = "query_metrics"
    if defect == "unsupported_status":
        investigation["confirmed_observations"][0]["observation"] = "The caller returned 500."
    verdict = score("processor_error", investigation)
    assert verdict["root_cause_correct"]
    assert verdict["citations_valid"] == (defect in {"aggregate", "unsupported_status"})
    assert not verdict["supporting_evidence_correct"]
    assert verdict["observations_grounded"] == (defect == "invented")
