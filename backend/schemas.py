from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input: dict[str, Any] = Field(default_factory=dict)


class WorkflowResponse(BaseModel):
    id: str
    name: str
    description: str
    version: int
    definition: dict[str, Any] | None = None
    created_at: datetime


class HandlerResponse(BaseModel):
    type: str
    description: str
    input_schema: dict[str, Any]


class WorkflowUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    definition: dict[str, Any]


class WorkflowDuplicate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=120)


class RunCreated(BaseModel):
    id: str
    status: str
    definition_id: str


class AttemptResponse(BaseModel):
    id: str
    task_id: str
    attempt_number: int
    worker_id: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    duration_seconds: float | None
    error: str | None
    error_classification: str | None = None
    output: dict[str, Any]


class EventResponse(BaseModel):
    id: str
    run_id: str
    task_id: str | None
    event_type: str
    payload: dict[str, Any]
    created_at: datetime


class TaskResponse(BaseModel):
    id: str
    run_id: str
    key: str
    type: str
    status: str
    input: dict[str, Any]
    output: dict[str, Any]
    dependencies: list[str]
    attempt_count: int
    max_attempts: int
    worker_id: str | None
    heartbeat_at: datetime | None
    lease_expires_at: datetime | None = None
    started_at: datetime | None
    finished_at: datetime | None
    duration_seconds: float | None
    error: str | None
    error_classification: str | None = None
    retry_policy: str = "exponential"
    retry_base_seconds: float = 1
    max_retry_delay_seconds: float = 300
    retry_on: list[str] = Field(default_factory=lambda: ["RETRYABLE", "TIMEOUT", "LEASE_EXPIRED"])
    compensation_type: str | None = None
    compensation_input: dict[str, Any] = Field(default_factory=dict)


class RunResponse(BaseModel):
    id: str
    definition_id: str
    definition_version: int = 1
    status: str
    input: dict[str, Any]
    output: dict[str, Any]
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    cancel_requested: bool
    definition_snapshot: dict[str, Any]
    tasks: list[TaskResponse]
    attempts: list[AttemptResponse]
    events: list[EventResponse]
    compensations: list[dict[str, Any]] = Field(default_factory=list)

