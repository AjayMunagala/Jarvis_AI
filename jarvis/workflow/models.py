"""Domain models and DAG specifications for workflow orchestration (v3.2)."""

from collections import defaultdict, deque
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class WorkflowStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class StepType(StrEnum):
    TOOL = "tool"
    ASSISTANT = "assistant"
    TRANSFORM = "transform"
    NOOP = "noop"


class StrictSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


# ------------------------------------------------------------- DAG Spec

class WorkflowStepDefinition(StrictSchema):
    step_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_\-]+$")
    name: str = Field(min_length=1, max_length=200)
    step_type: StepType = StepType.NOOP
    action: str = Field(default="", max_length=200)
    parameters: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list, max_length=50)


class DAGSpec(StrictSchema):
    steps: list[WorkflowStepDefinition] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def validate_dag(self) -> "DAGSpec":
        step_ids = {s.step_id for s in self.steps}
        if len(step_ids) != len(self.steps):
            raise ValueError("All step_ids within a workflow DAG must be unique.")

        # Check that all dependencies reference known step_ids
        for step in self.steps:
            for dep in step.depends_on:
                if dep == step.step_id:
                    raise ValueError(f"Step '{step.step_id}' cannot depend on itself.")
                if dep not in step_ids:
                    raise ValueError(
                        f"Step '{step.step_id}' depends on unknown step '{dep}'."
                    )

        # Cycle detection using Kahn's algorithm (topological sort)
        in_degree: dict[str, int] = {s.step_id: 0 for s in self.steps}
        adjacency: dict[str, list[str]] = defaultdict(list)

        for step in self.steps:
            for dep in step.depends_on:
                adjacency[dep].append(step.step_id)
                in_degree[step.step_id] += 1

        queue: deque[str] = deque([sid for sid, deg in in_degree.items() if deg == 0])
        visited_count = 0

        while queue:
            curr = queue.popleft()
            visited_count += 1
            for neighbor in adjacency[curr]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if visited_count != len(self.steps):
            raise ValueError("Cycle detected in workflow DAG. Workflows must be strictly acyclic.")

        return self

    def topological_levels(self) -> list[list[WorkflowStepDefinition]]:
        """Group steps into topological execution levels (steps in the same level are parallelizable)."""
        step_map = {s.step_id: s for s in self.steps}
        in_degree: dict[str, int] = {s.step_id: len(s.depends_on) for s in self.steps}
        adjacency: dict[str, list[str]] = defaultdict(list)

        for step in self.steps:
            for dep in step.depends_on:
                adjacency[dep].append(step.step_id)

        levels: list[list[WorkflowStepDefinition]] = []
        current_level = [sid for sid, deg in in_degree.items() if deg == 0]

        while current_level:
            levels.append([step_map[sid] for sid in sorted(current_level)])
            next_level: list[str] = []
            for sid in current_level:
                for neighbor in adjacency[sid]:
                    in_degree[neighbor] -= 1
                    if in_degree[neighbor] == 0:
                        next_level.append(neighbor)
            current_level = next_level

        return levels


# ------------------------------------------------------------- Entities

class WorkflowDefinition(StrictSchema):
    id: UUID
    owner_id: UUID
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    dag_spec: DAGSpec
    created_at: datetime
    updated_at: datetime


class WorkflowRun(StrictSchema):
    id: UUID
    workflow_id: UUID | None = None
    owner_id: UUID
    status: WorkflowStatus = WorkflowStatus.PENDING
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, Any] = Field(default_factory=dict)
    error_message: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class WorkflowStepRun(StrictSchema):
    id: UUID
    run_id: UUID
    step_id: str
    step_name: str
    status: StepStatus = StepStatus.PENDING
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, Any] = Field(default_factory=dict)
    error_message: str | None = None
    recovery_metadata: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime | None = None
    completed_at: datetime | None = None


# ------------------------------------------------------------- Requests

class WorkflowDefinitionCreateRequest(StrictSchema):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    dag_spec: DAGSpec


class WorkflowRunTriggerRequest(StrictSchema):
    inputs: dict[str, Any] = Field(default_factory=dict)


# ------------------------------------------------------------- Responses & Pages

class WorkflowDefinitionPage(StrictSchema):
    items: list[WorkflowDefinition]
    total: int = Field(ge=0)


class WorkflowRunPage(StrictSchema):
    items: list[WorkflowRun]
    total: int = Field(ge=0)


class WorkflowRunDetail(StrictSchema):
    run: WorkflowRun
    steps: list[WorkflowStepRun]
