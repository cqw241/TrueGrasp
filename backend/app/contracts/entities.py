"""业务实体（技术文档第 4 节），用 SQLModel 定义，存在 SQLite 业务库中。

技术文档列出 9 个实体；另加 Student 和 TeachingGoal，供学生名单和教师目标使用。
JSON 列读写时按声明的类型校验和还原。JSON 列不追踪原地修改，更新时整体赋新值。
业务库开启了外键约束；实体之间没有声明 relationship，同一事务里先 flush 被引用的行。
"""

from datetime import date
from typing import Any

from pydantic import TypeAdapter
from sqlalchemy import JSON, Column
from sqlalchemy.types import TypeDecorator
from sqlmodel import Field, SQLModel

from app.contracts.enums import (
    ContentSource,
    Dimension,
    EventType,
    EvidenceKind,
    ExamTag,
    HypothesisStatus,
    ItemPurpose,
    ItemSource,
    JudgedBy,
    MasteryState,
    TaskStatus,
    TaskType,
)
from app.contracts.values import ItemSpec, LaTeX, MisconceptionExample, Prediction


class _PydanticJSON(TypeDecorator):
    impl = JSON
    cache_ok = True

    def __init__(self, tp: Any) -> None:
        super().__init__()
        self._adapter = TypeAdapter(tp)

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        return self._adapter.dump_python(self._adapter.validate_python(value), mode="json")

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        return None if value is None else self._adapter.validate_python(value)


def _json_list(tp: Any) -> Any:
    return Field(default_factory=list, sa_column=Column(_PydanticJSON(list[tp]), nullable=False))


def _json(tp: Any, *, nullable: bool = False) -> Any:
    if nullable:
        return Field(default=None, sa_column=Column(_PydanticJSON(tp | None), nullable=True))
    return Field(default_factory=dict, sa_column=Column(_PydanticJSON(tp), nullable=False))


class Student(SQLModel, table=True):
    __tablename__ = "student"

    id: str = Field(primary_key=True)
    name: str


class TeachingGoal(SQLModel, table=True):
    """教师设定的目标，如“周五前 90% 初步掌握、两轮不通过转人工”。"""

    __tablename__ = "teaching_goal"

    id: int | None = Field(default=None, primary_key=True)
    knowledge_point_ids: list[str] = _json_list(str)
    target_state: MasteryState = MasteryState.INITIAL
    target_ratio: float = 0.9
    deadline: date
    max_rounds: int = 2  # 辅导轮次上限，超过即转交教师
    created_on: date


class KnowledgePoint(SQLModel, table=True):
    __tablename__ = "knowledge_point"

    id: str = Field(primary_key=True)  # 如 calc.ch2.chain_rule_multi
    lecture: int  # 讲，1 到 12
    section: int  # 节
    name: str
    prerequisite_ids: list[str] = _json_list(str)
    exam_tags: list[ExamTag] = _json_list(ExamTag)


class Misconception(SQLModel, table=True):
    """错因。ID 全书统一，跨章节跟进靠它匹配。"""

    __tablename__ = "misconception"

    id: str = Field(primary_key=True)  # 如 calc.ch2.chain.miss_inner
    knowledge_point_id: str = Field(foreign_key="knowledge_point.id", index=True)
    family: str  # 错因族，策略卡可按族复用
    description: str
    triggers: list[str] = _json_list(str)
    examples: list[MisconceptionExample] = _json_list(MisconceptionExample)
    probe_item_ids: list[str] = _json_list(str)  # 判别追问题
    strategy_ids: list[str] = _json_list(str)
    recurs_in: list[str] = _json_list(str)  # 可能复发的知识点
    source: ContentSource = ContentSource.TEACHER


class StrategyCard(SQLModel, table=True):
    __tablename__ = "strategy_card"

    id: str = Field(primary_key=True)
    misconception_ids: list[str] = _json_list(str)
    families: list[str] = _json_list(str)  # 适用的错因族
    steps: list[str] = _json_list(str)  # 教学步骤，具体讲解由模型按步骤现场生成
    constraints: list[str] = _json_list(str)
    rubric: list[str] = _json_list(str)  # 解释题的评分要点


class Item(SQLModel, table=True):
    __tablename__ = "item"

    id: str = Field(primary_key=True)
    knowledge_point_id: str = Field(foreign_key="knowledge_point.id", index=True)
    purpose: ItemPurpose
    dimension: Dimension | None = None  # 只有验证题有
    stem: str  # 题干，公式用 LaTeX
    spec: ItemSpec | None = _json(ItemSpec, nullable=True)  # 解释、证明等题型为空
    answer: LaTeX
    predictions: list[Prediction] = _json_list(Prediction)
    rubric: list[str] = _json_list(str)
    fingerprint: str | None = Field(default=None, index=True)
    source: ItemSource


class Hypothesis(SQLModel, table=True):
    """一次错误背后的一个错因假设，状态只由程序根据比对结果更新。"""

    __tablename__ = "hypothesis"

    id: int | None = Field(default=None, primary_key=True)
    student_id: str = Field(foreign_key="student.id", index=True)
    task_id: int = Field(foreign_key="task.id", index=True)
    error_evidence_id: int = Field(foreign_key="evidence.id")  # 引发诊断的那条错误作答
    misconception_id: str  # 错因 ID 或 SLIP
    status: HypothesisStatus = HypothesisStatus.PENDING
    evidence_ids: list[int] = _json_list(int)  # 支持或排除它的证据


class Evidence(SQLModel, table=True):
    """证据账本，只追加、不修改。教师处理也记为一条单独的证据。"""

    __tablename__ = "evidence"

    id: int | None = Field(default=None, primary_key=True)
    student_id: str = Field(foreign_key="student.id", index=True)
    knowledge_point_id: str = Field(foreign_key="knowledge_point.id", index=True)
    kind: EvidenceKind
    dimension: Dimension | None = None
    task_id: int | None = Field(default=None, foreign_key="task.id", index=True)
    item_id: str | None = Field(default=None, foreign_key="item.id")
    student_answer: str = ""  # LaTeX；解释题为文字
    correct_answer: LaTeX = ""
    predictions: list[Prediction] = _json_list(Prediction)  # 作答时各假设的预测
    is_correct: bool | None = None  # 无法判定时为空
    matched_misconception_id: str | None = None  # 命中的预测所属错因，可为 SLIP
    judged_by: JudgedBy
    reason: str = ""  # 判定理由；教师处理时为处理说明
    teaching_date: date


class Mastery(SQLModel, table=True):
    __tablename__ = "mastery"

    student_id: str = Field(foreign_key="student.id", primary_key=True)
    knowledge_point_id: str = Field(foreign_key="knowledge_point.id", primary_key=True)
    state: MasteryState
    evidence_ids: list[int] = _json_list(int)  # 当前状态依据的证据
    updated_on: date


class Task(SQLModel, table=True):
    """由规划 Agent 创建，教学时钟检查是否到期。"""

    __tablename__ = "task"

    id: int | None = Field(default=None, primary_key=True)
    type: TaskType
    student_id: str | None = Field(default=None, foreign_key="student.id", index=True)
    knowledge_point_id: str = Field(foreign_key="knowledge_point.id")
    misconception_ids: list[str] = _json_list(str)  # 针对的已确认错因
    evidence_ids: list[int] = _json_list(int)  # 创建依据；转交教师时即证据链
    reason: str = ""  # 规划 Agent 给出的理由，教师端展示
    due_date: date | None = None
    round: int = 1  # 第几轮辅导
    status: TaskStatus = TaskStatus.PENDING
    created_on: date


class Event(SQLModel, table=True):
    """唤起规划 Agent 的事件。payload 结构见 contracts.events 中对应的模型。"""

    __tablename__ = "event"

    id: int | None = Field(default=None, primary_key=True)
    type: EventType
    student_id: str | None = Field(default=None, foreign_key="student.id", index=True)
    payload: dict[str, Any] = _json(dict[str, Any])
    teaching_date: date
    processed: bool = Field(default=False, index=True)
