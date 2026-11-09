"""Run the repeatable incident evaluation against local Compose and Qwen."""

import argparse
import asyncio
import json
import os
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from tempfile import NamedTemporaryFile
from time import monotonic

import httpx
import psycopg

from evaluation.criteria import SCENARIOS, grade
from evaluation.incidents import DESCRIPTION, check_persistence, local_client, trigger_incident, verify_telemetry
from services.investigator.engine import MODEL

CHECKS = (
    "root_cause_correct", "citations_valid", "supporting_evidence_correct",
    "observations_grounded", "observations_separated", "outcome_correct",
)
DEFAULT_OUTPUT = Path("evaluation/results.json")


def summarize(runs: list[dict]) -> dict:
    scores = [run["score"] for run in runs if "score" in run]
    tool_calls = sum(score["tool_calls"] for score in scores)
    successful_calls = sum(score["successful_tool_calls"] for score in scores)
    return {
        "attempted_runs": len(runs),
        "graded_runs": len(scores),
        "run_errors": sum("error" in run for run in runs),
        "checks_passed": {check: sum(bool(score[check]) for score in scores) for check in CHECKS},
        "root_cause_accuracy": mean(bool(score["root_cause_correct"]) for score in scores) if scores else None,
        "average_duration_ms": round(mean(score["duration_ms"] for score in scores), 1) if scores else None,
        "average_tool_calls": round(mean(score["tool_calls"] for score in scores), 2) if scores else None,
        "tool_calls": tool_calls,
        "successful_tool_calls": successful_calls,
        "tool_call_success_rate": successful_calls / tool_calls if tool_calls else None,
    }


def checkpoint(output: Path, report: dict) -> None:
    runs = report["runs"]
    report["summary"] = {
        "overall": summarize(runs),
        "by_scenario": {
            mode: summarize([run for run in runs if run["scenario"] == mode])
            for mode in report["scenarios"]
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=output.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(report, stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.close()
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)


def model_details() -> dict:
    with local_client("OLLAMA_PORT", 11434) as ollama:
        version = ollama.get("/api/version").raise_for_status().json()["version"]
        models = ollama.get("/api/tags").raise_for_status().json()["models"]
        installed = next((model for model in models if model.get("name") == MODEL), None)
        if installed is None:
            raise RuntimeError(f"Local Ollama is missing {MODEL}")
        return {"name": MODEL, "digest": installed.get("digest"), "ollama_version": version}


def evaluate(repetitions: int, output: Path, mode: str | None = None) -> bool:
    scenarios = {mode: SCENARIOS[mode]} if mode else SCENARIOS
    with local_client("INVESTIGATOR_PORT", 8002, timeout=330) as investigator:
        investigator.get("/health").raise_for_status()
        report = {
            "schema_version": 1,
            "status": "running",
            "started_at": datetime.now(UTC).isoformat(),
            "model": model_details(),
            "repetitions_per_scenario": repetitions,
            "planned_runs": repetitions * len(scenarios),
            "incident_description": DESCRIPTION,
            "scenarios": {mode: asdict(scenario) for mode, scenario in scenarios.items()},
            "runs": [],
        }
        checkpoint(output, report)
        for repetition in range(1, repetitions + 1):
            for mode, scenario in scenarios.items():
                run = {"scenario": mode, "repetition": repetition}
                stage = "fixture"
                try:
                    observed = trigger_incident(mode, scenario.caller_status)
                    run["request_id"] = observed["request_id"]
                    write = check_persistence(observed, scenario.writes)
                    ground_truth = asyncio.run(verify_telemetry(observed, mode, scenario.downstream_status, write))
                    run["ground_truth"] = ground_truth
                    stage = "investigation"
                    started = monotonic()
                    result = investigator.post("/investigations", json={
                        "description": DESCRIPTION,
                        "request_id": ground_truth["request_id"],
                        "trace_id": ground_truth["trace_id"],
                    }).raise_for_status().json()
                    run["wall_duration_ms"] = round((monotonic() - started) * 1000)
                    run["result"] = result
                    stage = "grading"
                    run["score"] = grade(
                        scenario, result, request_id=ground_truth["request_id"],
                        trace_id=ground_truth["trace_id"], ground_truth=ground_truth,
                    )
                    stage = "persistence"
                    saved = investigator.get(f"/investigations/{result['id']}").raise_for_status().json()
                    if saved != result:
                        raise RuntimeError("The stored investigation differed from the returned result")
                except (httpx.HTTPError, psycopg.Error, RuntimeError, ValueError, KeyError) as error:
                    # Exclude raw database errors, which can contain connection details.
                    detail = type(error).__name__ if isinstance(error, psycopg.Error) else str(error)
                    run["error"] = {"stage": stage, "detail": detail}
                report["runs"].append(run)
                checkpoint(output, report)
                failures = run.get("score", {}).get("failures", [])
                print(f"{mode} {repetition}/{repetitions} {run.get('error') or failures or 'passed'}", flush=True)

        report["status"] = "complete"
        report["finished_at"] = datetime.now(UTC).isoformat()
        checkpoint(output, report)
        print(json.dumps(report["summary"]["overall"], indent=2), flush=True)
        return all(
            "error" not in run and all(run["score"][check] for check in CHECKS)
            and run["score"]["successful_tool_calls"] > 0
            for run in report["runs"]
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=int, default=2, help="Sequential runs per scenario (minimum 2)")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="JSON report path")
    parser.add_argument("--scenario", choices=SCENARIOS, help="Run just one scenario for a focused follow-up")
    options = parser.parse_args()
    if options.repetitions < 2:
        parser.error("--repetitions must be at least 2")
    try:
        passed = evaluate(options.repetitions, options.output, options.scenario)
    except (httpx.HTTPError, RuntimeError, ValueError, KeyError) as error:
        parser.exit(1, f"Evaluation could not start: {error}\n")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
