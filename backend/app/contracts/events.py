"""两类事件。

- 领域事件：存为 Event 行，唤起规划 Agent。每种 EventType 对应一个 payload 模型。
- 推送事件 StreamEvent：通过 ``GET /api/events`` 的 SSE 推给前端。只带 ID 和关键字段，
  前端收到后按需用 REST 接口重新拉取详情；只有 token 携带正文。
"""

from datetime import date
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, Field, TypeAdapter

from app.contracts.entities import Event
from app.contracts.enums import EventType, HypothesisStatus, MasteryState, TaskStatus, TaskType

# ---------- 领域事件的 payload ----------


class HomeworkImported(BaseModel):
    type: Literal[EventType.HOMEWORK_IMPORTED] = EventType.HOMEWORK_IMPORTED
    knowledge_point_ids: list[str]  # 这次作业涉及的知识点
    evidence_ids: list[int]  # 导入时为每份作答写入的证据


class AnswerSubmitted(BaseModel):
    type: Literal[EventType.ANSWER_SUBMITTED] = EventType.ANSWER_SUBMITTED
    task_id: int
    evidence_id: int


class StudentQuestion(BaseModel):
    type: Literal[EventType.STUDENT_QUESTION] = EventType.STUDENT_QUESTION
    text: str


class TaskDue(BaseModel):
    type: Literal[EventType.TASK_DUE] = EventType.TASK_DUE
    task_id: int


class LectureEntered(BaseModel):
    type: Literal[EventType.LECTURE_ENTERED] = EventType.LECTURE_ENTERED
    lecture: int


class TeacherInstruction(BaseModel):
    type: Literal[EventType.TEACHER_INSTRUCTION] = EventType.TEACHER_INSTRUCTION
    action: Literal["set_goal", "resolve_escalation"]
    ref_id: int  # TeachingGoal.id 或 Task.id
    note: str = ""


EventPayload = Annotated[
    HomeworkImported
    | AnswerSubmitted
    | StudentQuestion
    | TaskDue
    | LectureEntered
    | TeacherInstruction,
    Field(discriminator="type"),
]
_payload_adapter: TypeAdapter[EventPayload] = TypeAdapter(EventPayload)


def make_event(payload: EventPayload, teaching_date: date, student_id: str | None = None) -> Event:
    return Event(
        type=payload.type,
        student_id=student_id,
        payload=payload.model_dump(mode="json"),
        teaching_date=teaching_date,
    )


def read_payload(event: Event) -> EventPayload:
    return _payload_adapter.validate_python(event.payload)


# ---------- 推送给前端的事件 ----------


class TokenDelta(BaseModel):
    """辅导 Agent 的流式文本片段。"""

    type: Literal["token"] = "token"
    task_id: int
    text: str


class MessageDone(BaseModel):
    """一条辅导消息结束，前端可以重新拉取该任务的对话。"""

    type: Literal["message_done"] = "message_done"
    task_id: int


class HypothesisUpdated(BaseModel):
    type: Literal["hypothesis_updated"] = "hypothesis_updated"
    student_id: str
    task_id: int
    hypothesis_id: int
    misconception_id: str
    status: HypothesisStatus


class EvidenceAdded(BaseModel):
    type: Literal["evidence_added"] = "evidence_added"
    student_id: str
    evidence_id: int
    knowledge_point_id: str


class MasteryChanged(BaseModel):
    type: Literal["mastery_changed"] = "mastery_changed"
    student_id: str
    knowledge_point_id: str
    state: MasteryState


class TaskChanged(BaseModel):
    type: Literal["task_changed"] = "task_changed"
    task_id: int
    student_id: str | None
    task_type: TaskType
    status: TaskStatus


class ClockAdvanced(BaseModel):
    type: Literal["clock_advanced"] = "clock_advanced"
    teaching_date: date
    lecture: int


StreamEvent = Annotated[
    TokenDelta
    | MessageDone
    | HypothesisUpdated
    | EvidenceAdded
    | MasteryChanged
    | TaskChanged
    | ClockAdvanced,
    Field(discriminator="type"),
]


class EventPublisher(Protocol):
    """SSE 事件总线的发布端，由 app.orchestration 实现（T03）。"""

    async def publish(self, event: StreamEvent) -> None: ...
