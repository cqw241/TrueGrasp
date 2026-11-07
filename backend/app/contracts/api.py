"""HTTP 接口的请求与响应模型。能直接返回实体的接口直接用实体，不在这里重复定义。"""

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from app.contracts.entities import Evidence, Item, Task
from app.contracts.enums import (
    Dimension,
    HypothesisStatus,
    JudgedBy,
    MasteryState,
    TimelineKind,
)
from app.contracts.values import LaTeX, Prediction

# ---------- 教学时钟（T07） ----------


class ClockState(BaseModel):
    teaching_date: date
    lecture: int  # 当前讲到第几讲


class AdvanceClock(BaseModel):
    days: int = Field(default=1, ge=1)


class EnterLecture(BaseModel):
    lecture: int = Field(ge=1, le=12)


# ---------- 教师驾驶舱 ----------


class GoalCreate(BaseModel):
    knowledge_point_ids: list[str]
    target_state: MasteryState = MasteryState.INITIAL
    target_ratio: float = Field(default=0.9, gt=0, le=1)
    deadline: date
    max_rounds: int = Field(default=2, ge=1)


class HomeworkImportResult(BaseModel):
    event_id: int
    students: int
    items: int
    evidence: int


class ClassOverview(BaseModel):
    students: int
    goal_met: int  # 达到当前目标的人数
    goal_ratio: float
    escalated: int
    by_state: dict[MasteryState, int]


class HeatCell(BaseModel):
    """全书知识热力图的一格。"""

    knowledge_point_id: str
    lecture: int
    section: int
    name: str
    assessed: int  # 有掌握状态的学生数
    mastered_ratio: float  # 达到“初步掌握”及以上的比例
    misconception_students: int  # 有已确认错因的学生数


class CommonIssue(BaseModel):
    """共性问题与课上讲解建议，对应一条 CLASS_REVIEW 任务。"""

    task_id: int
    knowledge_point_id: str
    misconception_id: str
    description: str
    student_ids: list[str]
    suggestion: str


class Escalation(BaseModel):
    task: Task
    student_name: str
    evidence: list[Evidence]  # 证据链，按时间排序
    suggestion: str


class ResolveEscalation(BaseModel):
    note: str


# ---------- 学生辅导台 ----------


class ChatTurn(BaseModel):
    role: Literal["assistant", "user"]
    content: str
    item: Item | None = None  # 这条消息附带的题目


class MessageCreate(BaseModel):
    content: str  # 学生的回复；作答时为 LaTeX
    answering_item_id: str | None = None


class AnswerTarget(BaseModel):
    phase: Literal["idle", "processing", "waiting", "failed", "closed"]
    item_id: str | None = None
    prompt: str = ""
    stem: str = ""
    turn: int = 0


class Accepted(BaseModel):
    """异步处理已受理，结果通过 SSE 推送。"""

    task_id: int


# ---------- 决策证据视图 ----------


class ProbeRecord(BaseModel):
    """一道判别追问题：各假设的预测与学生的实际回答。"""

    item_id: str
    stem: str
    correct_answer: LaTeX
    predictions: list[Prediction]
    student_answer: str | None  # 尚未作答时为空
    matched_misconception_id: str | None
    judged_by: JudgedBy | None


class HypothesisView(BaseModel):
    hypothesis_id: int
    misconception_id: str
    description: str
    status: HypothesisStatus
    unmodeled: bool


class DiagnosisView(BaseModel):
    task_id: int
    student_id: str
    knowledge_point_id: str
    error_evidence: Evidence
    hypotheses: list[HypothesisView]
    probes: list[ProbeRecord]  # 按提问顺序


class TimelineEntry(BaseModel):
    teaching_date: date
    kind: TimelineKind
    knowledge_point_id: str
    title: str
    detail: str = ""
    task_id: int | None = None
    evidence_ids: list[int] = []


class DimensionResult(BaseModel):
    dimension: Dimension
    passed: bool | None  # 尚未验证时为空
    judged_by: JudgedBy | None
    evidence_id: int | None
