"""Tutor-private model outputs and durable interaction state."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.values import ItemSpec, Prediction


class CandidateSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    misconception_ids: list[str] = Field(default_factory=list, max_length=2)
    unmodeled_description: str = ""
    unmodeled_family: str = ""
    reason: str


class QuestionDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stem: str = Field(min_length=1)
    spec: ItemSpec
    answer: str = Field(min_length=1)
    predictions: list[Prediction] = Field(default_factory=list)


class QuestionReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    valid: bool | None
    reason: str


class AnswerReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_correct: bool | None
    matches: dict[str, bool | None] = Field(default_factory=dict)
    reason: str


class TeachingState(BaseModel):
    phase: Literal["diagnosis", "practice", "finished"] = "diagnosis"
    probe_count: int = 0
    candidate_ids: list[str] = Field(default_factory=list)
    hypothesis_ids: list[int] = Field(default_factory=list)
    confirmed_id: str | None = None
    awaiting_item_id: str | None = None
    probe_item_ids: list[str] = Field(default_factory=list)
    strategy_ids: list[str] = Field(default_factory=list)
    practice_failures: int = 0
