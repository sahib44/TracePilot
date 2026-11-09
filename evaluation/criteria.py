"""Deterministic rubric for the three reproducible TracePilot incidents.

The phrase rules recognize a bounded set of claims. They do not establish
general natural-language entailment. Keep the original findings and evidence
in the evaluation artifact so unrecognized wording can be reviewed.
"""

import re
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Scenario:
    name: str
    expected_root_cause: str
    expected_supporting_evidence: tuple[str, ...]
    caller_status: int
    downstream_status: int
    writes: bool
    cause_pattern: str
    anchor: str


SCENARIOS = {
    "processor_error": Scenario(
        "processor_error",
        "An internal failure in the Event Processor caused HTTP 500 and propagated as HTTP 502.",
        ("SimulatedProcessorError or simulated processor failure", "Processor 500 and caller 502", "No event row"),
        502,
        500,
        False,
        r"simulatedprocessorerror|(?:simulat\w*|internal).{0,36}processor.{0,24}(?:error|fail)"
        r"|processor.{0,36}(?:simulat\w*|internal).{0,24}(?:error|fail)"
        r"|simulat\w*.{0,18}(?:failure|error).{0,30}processor"
        r"|internal (?:server )?(?:error|failure).{0,30}processor"
        r"|simulat\w* processor (?:failure|error)",
        "processor_error",
    ),
    "database_error": Scenario(
        "database_error",
        "A PostgreSQL division-by-zero error aborted the database operation.",
        ("PostgreSQL DivisionByZero or SQLSTATE 22012", "Processor and caller 503", "No event row"),
        503,
        503,
        False,
        r"division[\s-]*by[\s-]*zero|\b22012\b|divid(?:e|ing) by zero",
        "database_error",
    ),
    "delay": Scenario(
        "delay",
        "The downstream processor delay exceeded the caller deadline, then the write succeeded.",
        ("Processor delay and caller 504", "Processor 201 and successful INSERT after caller response"),
        504,
        201,
        True,
        r"(?:processor|downstream).{0,55}(?:delay|slow|latency|exceed\w* (?:the )?(?:deadline|timeout))"
        r"|(?:delay|slow|latency).{0,55}(?:processor|downstream)",
        "processor_delay",
    ),
}

APPROVED_TOOLS = {"search_logs", "get_traces", "query_metrics", "get_service_health"}
SPECULATION = re.compile(r"\b(?:likely|probably|possibly|may|might|could|suspect\w*|hypothes\w*|"
                         r"root cause|caus(?:e|ed|es|ing)|because|due to|suggests?)\b")
NEGATION = re.compile(r"\b(?:not|never|no)\b|ruled out|unrelated to|instead of")
WRITE = re.compile(r"\b(?:write|writing|insert|insertion|persist\w*|commit\w*|sav\w*|written|wrote)\b")
WRITE_SUCCESS = re.compile(r"\b(?:committed|persisted|inserted|written|wrote|saved|completed|"
                           r"succeeded|successful(?:ly)?)\b")
STATUS_CODE = re.compile(r"\b(?:201|[45]\d\d)\b")
HTTP_FAILURE_DESCRIPTION = re.compile(
    r"\bdue to (?:an? )?(?:http )?(?:status(?: code)? )?[45]\d\d\b"
)
CALLER_FAILURE = re.compile(
    r"\b(?:event submission|(?:caller|client|game[ -]server) request|the request) (?:has )?failed\b"
    r"|\b(?:caller|client|game[ -]server) failed to submit (?:an? |the )?event\b"
)
DOWNSTREAM = r"(?:event[ -]processor|downstream processor)(?: service)?"
DOWNSTREAM_FAILURE = re.compile(
    rf"\b{DOWNSTREAM}(?: (?:post )?/process-event| request)? (?:failed to process|failed\b)"
)
DOWNSTREAM_SUCCESS = re.compile(
    rf"\b{DOWNSTREAM}(?: (?:post )?/process-event| request)? (?:succeeded|successfully processed)\b"
)
PARENTHETICAL_DURATION = re.compile(r"\((\d+(?:\.\d+)?)\s*ms\)")


def _cause_correct(scenario: Scenario, result: dict) -> bool:
    cause = (result.get("suspected_root_cause") or "").casefold()
    if result.get("status") != "completed":
        return False
    for clause in re.split(r"[.;!]|\bbut\b", cause):
        match = re.search(scenario.cause_pattern, clause)
        if match and not NEGATION.search(clause):
            return True
    return False


def _status_fact(service, status) -> str:
    return f"http:{service}:{status}"


def _parse_time(value) -> datetime | None:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
            return parsed if parsed.tzinfo is not None else None
        except ValueError:
            pass
    return None


def _facts(evidence: list[dict]) -> set[str]:
    facts = set()
    caller_ends = []
    writes = []
    for item in evidence:
        record = item["record"]
        if item["tool"] == "search_logs":
            service = record.get("service")
            event = record.get("event")
            status = record.get("status_code")
            error = record.get("error")
            timestamp = _parse_time(record.get("timestamp"))
            if event == "request_completed" and status is not None:
                facts.add(_status_fact(service, status))
                if service == "game-server" and status == 504 and timestamp is not None:
                    caller_ends.append(("logs", timestamp))
            if service == "event-processor":
                if event == "Event processing started":
                    facts.add("processor_started")
                if event == "Simulating processor delay":
                    facts.add("processor_delay")
                if event == "Event persisted":
                    facts.add("write_completed")
                    if timestamp is not None:
                        writes.append(("logs", timestamp))
                if isinstance(error, dict):
                    if error.get("type") == "SimulatedProcessorError":
                        facts.update(("processor_error", "write_failed"))
                    if error.get("type") == "DivisionByZero" or error.get("sqlstate") == "22012":
                        facts.update(("database_error", "write_failed"))
            if service == "game-server" and event == "Event processor timed out":
                facts.add("read_timeout")
            if service == "game-server" and event == "Event processor returned an unexpected status":
                if error == "HTTP 500":
                    facts.add(_status_fact("event-processor", 500))
        elif item["tool"] == "get_traces":
            for span in record.get("spans", []):
                service = span.get("service")
                status = span.get("http_status")
                error = span.get("error_type")
                mode = span.get("failure_mode")
                end = span.get("end_offset_ms")
                if status is not None:
                    # A Game Server client span records the upstream response.
                    target = "event-processor" if service == "game-server" and span.get("kind") == "client" else service
                    facts.add(_status_fact(target, status))
                    if target == "game-server" and status == 504 and isinstance(end, (int, float)):
                        caller_ends.append(("trace", end))
                if service == "event-processor" and span.get("kind") == "server":
                    facts.add("processor_started")
                    duration = span.get("duration_ms")
                    if isinstance(duration, (int, float)):
                        facts.add(f"downstream_duration_ms:{float(duration)}")
                if error == "SimulatedProcessorError" or mode == "processor_error":
                    facts.update(("processor_error", "write_failed"))
                if span.get("db_system") == "postgresql" and (error == "DivisionByZero" or span.get("sqlstate") == "22012"):
                    facts.update(("database_error", "write_failed"))
                if mode == "delay":
                    facts.add("processor_delay")
                if error in {"ReadTimeout", "TimeoutException"}:
                    facts.add("read_timeout")
                if span.get("db_operation") == "INSERT" and span.get("status") in {"unset", "ok"}:
                    facts.add("write_completed")
                    if isinstance(end, (int, float)):
                        writes.append(("trace", end))
    if any(write_source == caller_source and write > caller
           for write_source, write in writes for caller_source, caller in caller_ends):
        facts.add("late_write")
    if any(fact.startswith(("http:game-server:4", "http:game-server:5")) for fact in facts):
        facts.add("caller_failed")
    if any(fact.startswith(("http:event-processor:4", "http:event-processor:5")) for fact in facts):
        facts.add("downstream_failed")
    if any(fact.startswith("http:event-processor:2") for fact in facts):
        facts.add("downstream_succeeded")
    return facts


def _claimed_status(text: str, code: str, start: int, end: int) -> str:
    before = text[:start]
    after = text[end:]
    caller = r"game[ -]server|caller|client(?:-facing)?|gateway"
    downstream = r"event[ -]processor|processor|downstream|upstream"
    source = re.search(rf"(?P<caller>{caller})|(?P<downstream>{downstream})", after)
    if source and re.match(r"^[\w ]{0,24}\b(?:from|by|on|in|at)\s*$", after[:source.start()]):
        return _status_fact("game-server" if source.lastgroup == "caller" else "event-processor", code)
    sources = list(re.finditer(rf"(?P<caller>{caller})|(?P<downstream>{downstream})", before))
    if sources and start - sources[-1].end() < 45:
        target = "game-server" if sources[-1].lastgroup == "caller" else "event-processor"
    else:
        target = "event-processor" if code in {"201", "500"} else "game-server"
    return _status_fact(target, code)


def _claims(observation: str) -> set[str]:
    text = observation.casefold()
    claims = {
        _claimed_status(text, match.group(), match.start(), match.end())
        for match in STATUS_CODE.finditer(text)
    }
    if CALLER_FAILURE.search(text):
        claims.add("caller_failed")
    if DOWNSTREAM_FAILURE.search(text):
        claims.add("downstream_failed")
    if DOWNSTREAM_SUCCESS.search(text):
        claims.add("downstream_succeeded")
        if duration := PARENTHETICAL_DURATION.search(text):
            claims.add(f"downstream_duration_ms:{float(duration.group(1))}")
    if re.search(SCENARIOS["processor_error"].cause_pattern, text):
        claims.add("processor_error")
    if re.search(SCENARIOS["database_error"].cause_pattern, text):
        claims.add("database_error")
    if re.search(SCENARIOS["delay"].cause_pattern, text):
        claims.add("processor_delay")
    if re.search(r"\bread[ -]?timeout\b|\btimed out\b", text):
        claims.add("read_timeout")
    if re.search(r"\b(?:processing started|started processing|processing began)\b", text):
        claims.add("processor_started")
    if WRITE.search(text):
        if re.search(r"\b(?:not|never)\s+(?:been\s+)?(?:written|commit(?:ted)?|persist(?:ed)?|saved|"
                     r"complet(?:e|ed)|succeed(?:ed)?|successful)\b"
                     r"|\b(?:failed|aborted|rolled back)\b.{0,35}\b(?:write|transaction|insert)"
                     r"|\b(?:write|transaction|insert).{0,35}\b(?:failed|aborted|rolled back)\b"
                     r"|\bno (?:event|row) was (?:written|inserted|persisted|committed|saved)\b"
                     r"|\bno (?:write|insert)\b(?!\s+(?:failure|error|problem|issue)\b)", text):
            claims.add("write_failed")
        elif WRITE_SUCCESS.search(text):
            claims.add("write_completed")
            if re.search(r"after (?:the )?(?:caller|client|gateway|http|504|request timed out|timeout)", text):
                claims.add("late_write")
    return claims


def _separated_observation(text: str, grounded_claims: set[str]) -> bool:
    text = text.casefold()
    if any(claim.startswith("http:") for claim in grounded_claims):
        text = HTTP_FAILURE_DESCRIPTION.sub("", text)
    return SPECULATION.search(text) is None


def _same_incident(item: dict, request_id: str, trace_id: str) -> bool:
    record = item["record"]
    if item["tool"] == "get_traces":
        return record.get("trace_id") == trace_id
    return (
        item["tool"] == "search_logs" and record.get("request_id") == request_id
        and record.get("trace_id") == trace_id
    )


def _matches_snapshot(item: dict, ground_truth: dict | None) -> bool:
    if ground_truth is None:
        return True
    record = item["record"]
    if item["tool"] == "search_logs":
        return record in ground_truth["logs"]
    if item["tool"] == "get_traces":
        snapshot = ground_truth["trace"]
        return all(
            record.get(key) == snapshot.get(key)
            for key in ("trace_id", "started_at", "duration_ms", "span_count")
        ) and bool(record.get("spans")) and all(span in snapshot["spans"] for span in record["spans"])
    return True


def grade(
    scenario: Scenario, result: dict, *, request_id: str, trace_id: str, ground_truth: dict | None = None,
) -> dict:
    evidence = {item["id"]: item for item in result["evidence"]}
    timeline = result["timeline"]
    observations = result["confirmed_observations"]
    supporting_ids = result["supporting_evidence"]
    cited_ids = set(supporting_ids) | {ref for observation in observations for ref in observation["evidence_ids"]}
    issued = [
        (ref, step["tool"])
        for step in timeline if step["status"] == "ok" and step["tool"] in APPROVED_TOOLS
        for ref in step["evidence_ids"]
    ]
    invalid_ids = sorted(
        ref for ref in cited_ids if ref not in evidence or issued.count((ref, evidence[ref]["tool"])) != 1
        or (
            evidence[ref]["tool"] in {"search_logs", "get_traces"}
            and not (_same_incident(evidence[ref], request_id, trace_id)
                     and _matches_snapshot(evidence[ref], ground_truth))
        )
    )
    citations_valid = bool(cited_ids) and not invalid_ids and len(evidence) == len(result["evidence"])
    direct_support = [
        evidence[ref] for ref in supporting_ids if ref in evidence and ref not in invalid_ids
        and _same_incident(evidence[ref], request_id, trace_id)
    ]
    supporting_evidence_correct = (
        citations_valid and len(direct_support) == len(supporting_ids) and scenario.anchor in _facts(direct_support)
    )
    caller_outcome = _status_fact("game-server", scenario.caller_status)
    grounded_claims = set()
    outcome_records = []
    observation_checks = []
    for item in observations:
        claims = _claims(item["observation"])
        records = [
            evidence[ref] for ref in item["evidence_ids"] if ref in evidence and ref not in invalid_ids
        ]
        supported = _facts(records)
        grounded_claims.update(claims & supported)
        if claims & supported & {caller_outcome, "write_completed"}:
            outcome_records.extend(records)
        observation_checks.append({
            "observation": item["observation"],
            "evidence_ids": item["evidence_ids"],
            "recognized": sorted(claims),
            "unsupported": sorted(claims - supported),
            "grounded": bool(claims) and len(records) == len(item["evidence_ids"]) and claims <= supported,
            "separated": _separated_observation(item["observation"], claims & supported),
        })
    outcomes = {caller_outcome}
    if scenario.name == "processor_error":
        outcomes.add(_status_fact("event-processor", scenario.downstream_status))
    if scenario.writes:
        outcomes.update(("write_completed", "late_write"))
        if "late_write" in _facts(outcome_records):
            grounded_claims.add("late_write")
    checks = {
        "root_cause_correct": _cause_correct(scenario, result),
        "citations_valid": citations_valid,
        "supporting_evidence_correct": supporting_evidence_correct,
        "observations_grounded": bool(observation_checks) and all(item["grounded"] for item in observation_checks),
        "observations_separated": bool(observation_checks) and all(item["separated"] for item in observation_checks),
        "outcome_correct": outcomes <= grounded_claims,
    }
    return {
        **checks,
        "invalid_citation_ids": invalid_ids,
        "observation_checks": observation_checks,
        "missing_outcome_claims": sorted(outcomes - grounded_claims),
        "tool_calls": len(timeline),
        "successful_tool_calls": sum(step["status"] == "ok" and step["tool"] in APPROVED_TOOLS for step in timeline),
        "duration_ms": result["duration_ms"],
        "failures": [name for name, passed in checks.items() if not passed],
    }
