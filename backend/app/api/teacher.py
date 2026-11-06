"""教师驾驶舱与教学时钟。"""

from fastapi import APIRouter, Request, UploadFile
from sqlmodel import Session

from app.api.stub import not_implemented
from app.contracts.api import (
    AdvanceClock,
    ClassOverview,
    ClockState,
    CommonIssue,
    EnterLecture,
    Escalation,
    GoalCreate,
    HeatCell,
    HomeworkImportResult,
    ResolveEscalation,
)
from app.contracts.entities import Task, TeachingGoal
from app.contracts.enums import TaskStatus, TaskType
from app.ledger import queries

router = APIRouter(tags=["teacher"])


@router.get("/clock")
def get_clock() -> ClockState:
    not_implemented("T07")


@router.post("/clock/advance")
def advance_clock(body: AdvanceClock) -> ClockState:
    """推进教学日期，扫描到期任务并生成事件。"""
    not_implemented("T07")


@router.post("/clock/lecture")
def enter_lecture(body: EnterLecture) -> ClockState:
    """课程进入新章节，按三条规则创建章前预检。"""
    not_implemented("T07")


@router.get("/goals")
def list_goals() -> list[TeachingGoal]:
    not_implemented("T16")


@router.post("/goals")
def create_goal(body: GoalCreate) -> TeachingGoal:
    not_implemented("T16")


@router.post("/homework")
def import_homework(file: UploadFile) -> HomeworkImportResult:
    """导入结构化作业 CSV，学生答案为 LaTeX。"""
    not_implemented("T17")


@router.get("/class/overview")
def class_overview(request: Request) -> ClassOverview:
    with Session(request.app.state.orchestrator.engine) as session:
        return queries.class_overview(session)


@router.get("/class/heatmap")
def class_heatmap(request: Request) -> list[HeatCell]:
    with Session(request.app.state.orchestrator.engine) as session:
        return queries.heatmap(session)


@router.get("/class/issues")
def class_issues() -> list[CommonIssue]:
    """共性问题与课上讲解建议。"""
    not_implemented("T08")


@router.get("/tasks")
def list_tasks(
    student_id: str | None = None,
    type: TaskType | None = None,
    status: TaskStatus | None = None,
) -> list[Task]:
    """干预计划；学生辅导台也用它取任务卡。"""
    not_implemented("T08")


@router.get("/escalations")
def list_escalations() -> list[Escalation]:
    not_implemented("T16")


@router.post("/escalations/{task_id}/resolve")
def resolve_escalation(task_id: int, body: ResolveEscalation) -> Task:
    """教师处理转交，记为一条单独的证据，不覆盖历史。"""
    not_implemented("T16")
