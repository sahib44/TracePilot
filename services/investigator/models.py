from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, JsonValue, StringConstraints, model_validator


def valid_text(value: str) -> str:
    if "\x00" in value:
        raise ValueError("Text must not contain null characters")
    value.encode("utf-8")
    return value


Description = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1500), AfterValidator(valid_text)]
FindingText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=400), AfterValidator(valid_text)]
RequestId = Annotated[str, StringConstraints(pattern=r"^[!-~]{1,128}$")]
TraceId = Annotated[str, StringConstraints(to_lower=True, pattern=r"^[a-fA-F0-9]{32}$")]
EvidenceId = Annotated[str, StringConstraints(pattern=r"^E[1-9][0-9]{0,2}$")]
Confidence = Literal["high", "medium", "low", "undetermined"]
Status = Literal["completed", "insufficient_evidence", "partial"]


class IncidentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: Description | None = None
    request_id: RequestId | None = None
    trace_id: TraceId | None = None
    context: Description | None = None

    @model_validator(mode="after")
    def require_incident(self):
        if not (self.description or self.request_id or self.trace_id or self.context):
            raise ValueError("Provide an incident description, request ID, trace ID, or context")
        if self.trace_id == "0" * 32:
            raise ValueError("Trace ID must not be all zeros")
        return self


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observation: FindingText
    evidence_ids: list[EvidenceId] = Field(min_length=1, max_length=6)


class Findings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suspected_root_cause: FindingText | None = None
    confidence: Confidence = "undetermined"
    confirmed_observations: list[Observation] = Field(default_factory=list, max_length=6)
    supporting_evidence: list[EvidenceId] = Field(default_factory=list, max_length=10)
    missing_information: list[FindingText] = Field(default_factory=list, max_length=5)
    recommended_next_action: FindingText | None = None

    @model_validator(mode="after")
    def require_support(self):
        if self.suspected_root_cause is None:
            if self.supporting_evidence or self.confidence != "undetermined":
                raise ValueError("A result without a suspected cause must use undetermined confidence")
        elif not self.supporting_evidence or self.confidence == "undetermined":
            raise ValueError("A suspected cause must have supporting evidence and a confidence level")
        return self


class EvidenceRecord(BaseModel):
    id: EvidenceId
    tool: str
    record: dict[str, JsonValue]


class ToolStep(BaseModel):
    tool: str
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    started_at: datetime
    duration_ms: int = Field(ge=0)
    status: Literal["ok", "error"]
    evidence_ids: list[EvidenceId] = Field(default_factory=list)
    error: str | None = None
    truncated: bool = False


class InvestigationResult(Findings):
    id: UUID
    incident: IncidentInput
    status: Status
    started_at: datetime
    finished_at: datetime
    duration_ms: int = Field(ge=0)
    stop_reason: Literal[
        "timeout", "tool_call_limit", "model_unavailable", "malformed_model_response", "invalid_evidence"
    ] | None = None
    timeline: list[ToolStep] = Field(default_factory=list)
    evidence: list[EvidenceRecord] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_evidence_references(self):
        evidence_ids = {item.id for item in self.evidence}
        references = set(self.supporting_evidence)
        for observation in self.confirmed_observations:
            references.update(observation.evidence_ids)
        for step in self.timeline:
            references.update(step.evidence_ids)
        if references - evidence_ids:
            raise ValueError("Investigation references evidence that was not retrieved")
        return self
