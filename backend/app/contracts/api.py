"""HTTP 接口的请求与响应模型。能直接返回实体的接口直接用实体，不在这里重复定义。"""

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, model_validator

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


class TaskOverride(BaseModel):
    """教师修改或覆盖一条计划任务；被覆盖的计划不再被 Agent 改回（T16、T24）。"""

    due_date: date | None = None  # 为空时不改到期日
    cancel: bool = False
    note: str = ""  # 修改理由，记入教师处理


class TaskControl(BaseModel):
    """教师对一条任务的控制状态，驾驶舱据此标记接管和覆盖。"""

    task_id: int
    taken_over: bool  # 教师接管期间 Agent 不再向该学生发出教学动作
    overridden: bool
    note: str = ""


class TakeoverRequest(BaseModel):
    note: str = ""


class AdoptMisconception(BaseModel):
    """采纳一条未建模错因，进入错因库；可同时修改描述。"""

    description: str | None = None


# ---------- 学生辅导台 ----------


class ChatTurn(BaseModel):
    role: Literal["assistant", "user"]
    content: str
    item: Item | None = None  # 这条消息附带的题目
    hint_level: int | None = Field(
        default=None, ge=1, le=3
    )  # 分层提示：1 思路方向、2 关键步骤、3 具体计算
    solution: bool = False  # 三层提示用完后的完整解答


class MessageCreate(BaseModel):
    content: str = ""  # 学生的回复；作答时为 LaTeX，补图时可为空
    answering_item_id: str | None = None
    photo_ids: list[str] = Field(default_factory=list, max_length=3)


class QuestionCreate(BaseModel):
    """学生主动提问：题目文字/LaTeX 或题图，可附上自己的尝试或卡住的位置。"""

    content: str = ""
    attempt: str = ""
    photo_ids: list[str] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def _needs_content_or_photo(self):
        if not self.content.strip() and not self.photo_ids:
            raise ValueError("请提供题目文字或题目图片")
        return self


class QuestionPhotoResult(BaseModel):
    """题目照片识别结果；原图已保存，可随提问或追问再次交给模型查看。"""

    latex: str
    doubts: list[str] = Field(default_factory=list)
    requires_confirmation: bool = True
    photo_id: str


class AnswerTarget(BaseModel):
    phase: Literal["idle", "processing", "waiting", "failed", "closed"]
    item_id: str | None = None
    prompt: str = ""
    stem: str = ""
    turn: int = 0
    accepts_text: bool = False  # Some item-bound turns accept a description of a stuck step.


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
