import math
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator


class EventType(StrEnum):
    PLAYER_JOINED = "player_joined"
    PLAYER_LEFT = "player_left"
    MATCH_STARTED = "match_started"
    SCORE_UPDATED = "score_updated"
    MATCH_ENDED = "match_ended"


class EventInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: EventType
    payload: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("payload")
    @classmethod
    def validate_payload(cls, payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
        # Check JSON values that the Python decoder accepts but HTTPX or JSONB rejects.
        pending: list[JsonValue] = [payload]
        while pending:
            value = pending.pop()
            if isinstance(value, dict):
                pending.extend(value)
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
            elif isinstance(value, str):
                if "\x00" in value:
                    raise ValueError("Payload strings must not contain null characters")
                value.encode("utf-8")
            elif isinstance(value, float) and not math.isfinite(value):
                raise ValueError("Payload numbers must be finite")
        return payload


class ProcessEventInput(EventInput):
    session_id: UUID


class EventRecord(ProcessEventInput):
    id: UUID
    request_id: str
    created_at: datetime
