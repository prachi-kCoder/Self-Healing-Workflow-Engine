"""Workflow definition validation and state transition rules."""
from collections import defaultdict, deque
from enum import StrEnum
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TaskStatus(StrEnum):
    BLOCKED = "BLOCKED"
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    RETRY_WAIT = "RETRY_WAIT"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SKIPPED = "SKIPPED"


class WorkflowStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TASK_TRANSITIONS = {
    TaskStatus.BLOCKED: {TaskStatus.PENDING, TaskStatus.READY, TaskStatus.SKIPPED, TaskStatus.CANCELLED},
    TaskStatus.PENDING: {TaskStatus.READY, TaskStatus.CANCELLED, TaskStatus.SKIPPED},
    TaskStatus.READY: {TaskStatus.RUNNING, TaskStatus.CANCELLED},
    TaskStatus.RUNNING: {TaskStatus.SUCCEEDED, TaskStatus.RETRY_WAIT, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.READY},
    TaskStatus.RETRY_WAIT: {TaskStatus.READY, TaskStatus.CANCELLED},
    TaskStatus.SUCCEEDED: set(),
    TaskStatus.FAILED: {TaskStatus.READY},
    TaskStatus.CANCELLED: set(),
    TaskStatus.SKIPPED: {TaskStatus.BLOCKED, TaskStatus.PENDING},
}


def transition(current: TaskStatus, target: TaskStatus) -> None:
    if target not in TASK_TRANSITIONS[current]:
        raise ValueError(f"Illegal task transition: {current} -> {target}")


class CompensationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str = Field(min_length=1, max_length=120)
    input: dict[str, Any] = Field(default_factory=dict)


class TaskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_.-]+$")
    type: str = Field(min_length=1, max_length=120)
    depends_on: list[str] = Field(default_factory=list, max_length=100)
    input: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = Field(default=30, ge=1, le=3600)
    max_attempts: int = Field(default=3, ge=1, le=20)
    retry_base_seconds: float = Field(default=1, ge=0, le=3600)
    retry_policy: Literal["exponential", "fixed"] = "exponential"
    max_retry_delay_seconds: float = Field(default=300, ge=0, le=3600)
    retry_on: list[Literal["RETRYABLE", "TIMEOUT", "LEASE_EXPIRED"]] = Field(
        default_factory=lambda: ["RETRYABLE", "TIMEOUT", "LEASE_EXPIRED"], max_length=3
    )
    compensation: CompensationSpec | None = None


class WorkflowSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)
    metadata: dict[str, Any] = Field(default_factory=dict)
    tasks: list[TaskSpec] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_graph(self):
        try:
            encoded = json.dumps(self.model_dump(mode="json"), allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ValueError("Workflow metadata and task inputs must contain valid JSON values") from exc
        if len(encoded.encode("utf-8")) > 1_000_000:
            raise ValueError("Workflow definition exceeds the 1 MB size limit")
        for task in self.tasks:
            size = len(json.dumps(task.input, allow_nan=False, separators=(",", ":")).encode("utf-8"))
            if size > 256_000:
                raise ValueError(f"Task {task.key} input exceeds the 256 KB size limit")
        by_key = {task.key: task for task in self.tasks}
        if len(by_key) != len(self.tasks):
            raise ValueError("Task keys must be unique")
        if any(len(set(task.retry_on)) != len(task.retry_on) for task in self.tasks):
            raise ValueError("retry_on entries must be unique")
        children: dict[str, list[str]] = defaultdict(list)
        degree = {key: 0 for key in by_key}
        for task in self.tasks:
            if task.key in task.depends_on:
                raise ValueError(f"Task {task.key} cannot depend on itself")
            if len(set(task.depends_on)) != len(task.depends_on):
                raise ValueError(f"Task {task.key} has duplicate dependencies")
            for parent in task.depends_on:
                if parent not in by_key:
                    raise ValueError(f"Task {task.key} references unknown dependency {parent}")
                children[parent].append(task.key)
                degree[task.key] += 1
        ready = deque(key for key, count in degree.items() if count == 0)
        visited = 0
        while ready:
            key = ready.popleft()
            visited += 1
            for child in children[key]:
                degree[child] -= 1
                if degree[child] == 0:
                    ready.append(child)
        if visited != len(by_key):
            raise ValueError("Workflow dependencies contain a cycle")
        return self
