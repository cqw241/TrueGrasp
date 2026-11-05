"""三个 Agent 与编排层之间的输入输出（技术文档第 2 节）。

编排层（T03）负责构造输入、写入数据库和推送事件；各 Agent 只返回决策。
Agent 内部调用模型时用的提示词和结构化输出模型，由各 Agent 自行定义。
"""

from datetime import date
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from app.contracts.entities import Item
from app.contracts.enums import Dimension, JudgedBy, TaskType, TutorOutcome
from app.contracts.events import EventPublisher
from app.contracts.llm import ChatMessage

# ---------- 教学规划 Agent（T08） ----------


class PlannerInput(BaseModel):
    event_id: int  # 唤起这次规划的 Event
    teaching_date: date


class PlannedTask(BaseModel):
    type: TaskType
    student_id: str | None  # CLASS_REVIEW 为空
    knowledge_point_id: str
    misconception_ids: list[str] = []
    evidence_ids: list[int] = []  # 决策依据；ESCALATION 时即交给教师的证据链
    reason: str  # 教师端展示的理由
    due_date: date | None = None
    round: int = 1


class PlannerOutput(BaseModel):
    tasks: list[PlannedTask] = []
    cancel_task_ids: list[int] = []  # 新证据出现后不再需要的任务
    summary: str = ""


class Planner(Protocol):
    async def plan(self, inp: PlannerInput) -> PlannerOutput: ...


# ---------- 诊断辅导 Agent（T09） ----------


class TutorTask(BaseModel):
    task_id: int
    student_id: str
    knowledge_point_id: str
    error_evidence_id: int | None  # 引发辅导的错误作答
    allowed_backtrack_ids: list[str]  # 允许回退到的先修知识点
    round: int


class TutorTurnInput(BaseModel):
    task: TutorTask
    history: list[ChatMessage]  # 本任务的对话，来自 LangGraph checkpoint
    student_message: str | None  # None 表示开场，由辅导 Agent 先发言
    answering_item_id: str | None = None  # 这条消息是在回答哪道题


class TutorTurn(BaseModel):
    outcome: TutorOutcome
    message: str  # 本轮完整回复；生成时已通过 TokenDelta 流式推送
    awaiting_item_id: str | None = None  # 等学生回答的题目
    confirmed_misconception_ids: list[str] = []  # REQUEST_VERIFICATION 时必填
    replan_reason: str = ""


class Tutor(Protocol):
    async def turn(self, inp: TutorTurnInput, publish: EventPublisher) -> TutorTurn:
        """一轮辅导。假设状态的更新、判别追问题和作答证据的写入在 Agent 内部由程序完成；
        Agent 不能修改掌握状态。"""
        ...


# ---------- 独立验证 Agent（T18） ----------


class ConfirmedMisconception(BaseModel):
    id: str
    description: str


class VerifierInput(BaseModel):
    """验证 Agent 能看到的全部信息（白名单），由编排层构造。

    不含辅导对话、讲解内容、学生在辅导中的作答，也不含学生身份；多出的字段直接报错。
    """

    model_config = ConfigDict(extra="forbid")

    knowledge_point_id: str
    dimensions: list[Dimension]
    confirmed_misconceptions: list[ConfirmedMisconception]
    used_fingerprints: list[str]
    context_knowledge_point_id: str | None = None  # 跨章节应用题所在的后续知识点


class Judgement(BaseModel):
    is_correct: bool | None
    matched_misconception_id: str | None = None
    judged_by: JudgedBy
    reason: str


class Verifier(Protocol):
    async def compose(self, inp: VerifierInput) -> list[Item]:
        """每个维度出一道题，均已通过出题质量门槛；未通过时退回种子题。"""
        ...

    async def judge(self, item: Item, student_answer: str) -> Judgement:
        """工具优先判定，判不了时由模型按 item.rubric 评阅。"""
        ...
