"""教师作业解析草稿；不属于正式题库、证据或辅导任务。"""

from datetime import UTC, date, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.api import DimensionResult, HomeworkImportResult
from app.contracts.entities import Task
from app.contracts.enums import MasteryState


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


class IssueResolution(DraftModel):
    action: Literal["corrected", "accepted"]
    by_teacher_id: str
    at: datetime
    note: str | None = None


class Issue(DraftModel):
    issue_id: str = Field(default_factory=lambda: uuid4().hex)
    code: str
    field: str
    message: str
    blocking: bool = True
    resolution: IssueResolution | None = None


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
    status: Literal["unmatched", "ambiguous", "proposed", "confirmed"] = "unmatched"


class Record(DraftModel):
    record_id: str
    question_id: str
    identity: IdentityMatch
    student_answer: str | None = None
    answer_kind: Literal["expression", "steps", "text", "proof", "mixed", "unknown"] = "unknown"
    source_refs: list[SourceRef] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)
    review_status: Literal["pending", "confirmed", "skipped", "published"] = "pending"
    publication_id: str | None = None


class ParseError(DraftModel):
    source_id: str
    page: int | None = None
    code: str
    message: str
    retryable: bool = True


class Extraction(DraftModel):
    questions: list[Question] = Field(default_factory=list)
    records: list[Record] = Field(default_factory=list)


class Edit(DraftModel):
    revision: int
    target_id: str
    field: str
    before: Any
    after: Any
    by_teacher_id: str
    at: datetime
    note: str | None = None


class HomeworkDraft(Extraction):
    schema_version: Literal["0.1"] = "0.1"
    draft_id: str
    # Older personal drafts have no class or stable creator ID until explicitly bound.
    class_id: str | None = None
    created_by_username: str
    created_by_teacher_id: str | None = None
    title: str
    revision: int = 1
    status: Literal["parsing", "review", "parse_failed", "published"] = "parsing"
    sources: list[DraftSource] = Field(default_factory=list)
    parse_errors: list[ParseError] = Field(default_factory=list)
    original_extraction: Extraction | None = None
    edits: list[Edit] = Field(default_factory=list)
    publication_ids: list[str] = Field(default_factory=list)
    active_publication_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class DraftSummary(DraftModel):
    draft_id: str
    class_id: str | None = None
    title: str
    revision: int
    status: Literal["parsing", "review", "parse_failed", "published"]
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


class UpdateModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QuestionUpdate(UpdateModel):
    question_id: str
    stem: str | None = None
    correct_answer: str | None = None
    knowledge_point_id: str | None = None
    item_id: str | None = None


class RecordUpdate(UpdateModel):
    record_id: str
    student_id: str | None = None
    student_answer: str | None = None
    review_status: Literal["pending", "confirmed", "skipped"] | None = None
    note: str | None = None


class ResolveIssue(UpdateModel):
    issue_id: str
    action: Literal["corrected", "accepted"]
    note: str | None = None


class DraftUpdate(UpdateModel):
    expected_revision: int
    class_id: str | None = None
    question_updates: list[QuestionUpdate] = Field(default_factory=list, max_length=5000)
    record_updates: list[RecordUpdate] = Field(default_factory=list, max_length=5000)
    issue_resolutions: list[ResolveIssue] = Field(default_factory=list, max_length=10000)


class PublishSelection(UpdateModel):
    expected_revision: int
    record_ids: list[str] = Field(min_length=1, max_length=5000)


class ReviewProblem(DraftModel):
    record_id: str
    field: str
    message: str


class PublishPreview(DraftModel):
    draft_id: str
    revision: int
    selected_records: int
    students: int
    items: int
    answers: int
    remaining_records: int
    blocking_issues: list[ReviewProblem]
    can_publish: bool


class PublicationError(DraftModel):
    code: str
    message: str
    retryable: bool


class RecordResult(DraftModel):
    record_id: str
    item_id: str
    evidence_id: int


class Workflow(DraftModel):
    event_id: int | None = None
    state: Literal["pending", "planned", "failed"] = "pending"
    task_ids: list[int] = Field(default_factory=list)
    error: PublicationError | None = None


class HomeworkTaskProgress(DraftModel):
    task: Task
    student_name: str | None
    student_number: str | None
    knowledge_point_name: str
    mastery_state: MasteryState | None
    verification: list[DimensionResult]
    failed: bool
    teacher_takeover: bool
    can_work: bool


class PublicationProgress(DraftModel):
    publication_id: str
    class_id: str
    workflow: Workflow
    tasks: list[HomeworkTaskProgress]


class Publication(DraftModel):
    publication_id: str
    draft_id: str
    class_id: str
    confirmed_revision: int
    record_ids: list[str]
    confirmed_by_teacher_id: str
    confirmed_at: datetime
    teaching_date: date
    status: Literal["publishing", "published", "failed"]
    confirmed_snapshot: Extraction
    import_result: HomeworkImportResult | None = None
    record_results: list[RecordResult] = Field(default_factory=list)
    workflow: Workflow = Field(default_factory=Workflow)
    error: PublicationError | None = None
