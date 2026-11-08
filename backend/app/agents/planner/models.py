"""Planner-private model output and execution metadata; no public contract changes."""

from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.agents import PlannedTask, PlannerOutput


class PlanningPolicy(BaseModel):
    class_review_min_students: int = Field(default=3, ge=2)
    retest_delay_days: int = Field(default=3, ge=1)


class TaskContext(BaseModel):
    root_knowledge_point_id: str
    context_knowledge_point_id: str | None = None
    issue_misconception_id: str | None = None


class Candidate(BaseModel):
    id: str
    task: PlannedTask
    context: TaskContext
    required: bool = True
    allowed_backtrack_ids: list[str] = []
    goal_gap: float = 0
    prerequisite_blockers: int = 0
    days_to_deadline: int | None = None
    relapse_risk: bool = False


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    include: bool
    reason: str = Field(min_length=1)
    knowledge_point_id: str
    due_date: date


class PlanningDecisions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decisions: list[Decision]
    summary: str = Field(min_length=1)


class PlanningResult(PlannerOutput):
    """Extra checkpoint-only context, consumed by orchestration before public validation."""

    contexts: list[TaskContext] = []
