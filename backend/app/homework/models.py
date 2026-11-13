"""教师作业解析草稿；不属于正式题库、证据或辅导任务。"""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DraftModel(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


class DraftApiError(BaseModel):
    detail: str | dict | list[dict]


class SourceRef(DraftModel):
    source_id: str
    page: int | None = None
    row: int | None = None


class DraftSource(DraftModel):
    source_id: str
    kind: Literal["file", "text"]
    filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    page_count: int | None = None


class Issue(DraftModel):
    code: str
    field: str
    message: str
    blocking: bool = True


class KnowledgeCandidate(DraftModel):
    id: str
    reason: str = ""


class Question(DraftModel):
    question_id: str
    number: str | None = None
    item_id: str | None = None
    stem: str | None = None
    correct_answer: str | None = None
    rubric: list[str] = Field(default_factory=list)
    knowledge_point_id: str | None = None
    knowledge_candidates: list[KnowledgeCandidate] = Field(default_factory=list)
    source_refs: list[SourceRef] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)


class IdentityMatch(DraftModel):
    observed_name: str | None = None
    observed_student_id: str | None = None
    student_number: str | None = None
    student_id: str | None = None
    candidate_student_ids: list[str] = Field(default_factory=list)
    status: Literal["unmatched"] = "unmatched"


class Record(DraftModel):
    record_id: str
    question_id: str
    identity: IdentityMatch
    student_answer: str | None = None
    answer_kind: Literal["expression", "steps", "text", "proof", "mixed", "unknown"] = "unknown"
    source_refs: list[SourceRef] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)
    review_status: Literal["pending"] = "pending"


class ParseError(DraftModel):
    source_id: str
    page: int | None = None
    code: str
    message: str
    retryable: bool = True


class Extraction(DraftModel):
    questions: list[Question] = Field(default_factory=list)
    records: list[Record] = Field(default_factory=list)


class HomeworkDraft(Extraction):
    schema_version: Literal["0.1"] = "0.1"
    draft_id: str
    # OPT-01 尚未提供班级身份；不得拿用户名伪装成稳定 teacher_id。
    class_id: str | None = None
    created_by_username: str
    title: str
    revision: int = 1
    status: Literal["parsing", "review", "parse_failed"] = "parsing"
    sources: list[DraftSource] = Field(default_factory=list)
    parse_errors: list[ParseError] = Field(default_factory=list)
    original_extraction: Extraction | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class DraftSummary(DraftModel):
    draft_id: str
    title: str
    revision: int
    status: Literal["parsing", "review", "parse_failed"]
    created_at: datetime
    updated_at: datetime


class RetrySources(BaseModel):
    expected_revision: int
    source_ids: list[str] = Field(min_length=1, max_length=7)


class ExtractedAnswer(BaseModel):
    """单次模型提取：身份只是原文线索，ID/来源由服务端生成。"""

    number: str | None = None
    stem: str | None = None
    correct_answer: str | None = None
    student_name: str | None = None
    student_number: str | None = None
    student_answer: str | None = None
    answer_kind: Literal["expression", "steps", "text", "proof", "mixed", "unknown"] = "unknown"
    knowledge_point_id: str | None = None
    doubts: list[str] = Field(default_factory=list)


class PageExtraction(BaseModel):
    answers: list[ExtractedAnswer] = Field(default_factory=list, max_length=5000)
