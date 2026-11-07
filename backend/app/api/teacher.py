"""教师驾驶舱与教学时钟。"""

from datetime import date

from fastapi import APIRouter, HTTPException, Request, UploadFile
from openai import OpenAIError
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.agents.planner.queries import common_issues
from app.api.stub import not_implemented
from app.config import get_settings
from app.contracts.api import (
    AdoptMisconception,
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
    TakeoverRequest,
    TaskControl,
    TaskOverride,
)
from app.contracts.entities import Misconception, Task, TeachingGoal
from app.contracts.enums import TaskStatus, TaskType
from app.ingest import HomeworkImporter, IngestError, parse_csv
from app.ingest.service import MAX_CSV_BYTES
from app.ledger import queries
from app.llm.client import LLMOutputError, OpenAICompatClient

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
async def import_homework(file: UploadFile, request: Request) -> HomeworkImportResult:
    """导入结构化作业 CSV，学生答案为 LaTeX。"""
    runtime = request.app.state.orchestrator
    data = await file.read(MAX_CSV_BYTES + 1)
    teaching_date = runtime.teaching_date() if runtime.teaching_date else date.today()

    async def ingest(llm) -> HomeworkImportResult:
        return await HomeworkImporter(runtime.engine, llm).import_rows(rows, teaching_date)

    try:
        rows = parse_csv(data)
        # 可注入与其他 Agent 相同的 LLMClient，默认使用 T06 的配置与实现。
        llm = getattr(request.app.state, "homework_llm", None)
        if llm is not None:
            result = await ingest(llm)
        else:
            async with OpenAICompatClient(get_settings()) as client:
                result = await ingest(client)
    except IngestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="导入数据与现有记录冲突，整批已回滚") from exc
    except (OpenAIError, LLMOutputError) as exc:
        raise HTTPException(status_code=502, detail="作业模型分析失败，整批未导入") from exc
    runtime.recover_pending()
    return result


@router.get("/class/overview")
def class_overview(request: Request) -> ClassOverview:
    with Session(request.app.state.orchestrator.engine) as session:
        return queries.class_overview(session)


@router.get("/class/heatmap")
def class_heatmap(request: Request) -> list[HeatCell]:
    with Session(request.app.state.orchestrator.engine) as session:
        return queries.heatmap(session)


@router.get("/class/issues")
def class_issues(request: Request) -> list[CommonIssue]:
    """共性问题与课上讲解建议。"""
    with Session(request.app.state.orchestrator.engine) as session:
        return common_issues(session)


@router.get("/tasks")
def list_tasks(
    request: Request,
    student_id: str | None = None,
    type: TaskType | None = None,
    status: TaskStatus | None = None,
) -> list[Task]:
    """干预计划；学生辅导台也用它取任务卡。"""
    query = select(Task).order_by(Task.id.desc())
    if student_id is not None:
        query = query.where(Task.student_id == student_id)
    if type is not None:
        query = query.where(Task.type == type)
    if status is not None:
        query = query.where(Task.status == status)
    with Session(request.app.state.orchestrator.engine) as session:
        return list(session.exec(query))


@router.get("/tasks/controls")
def list_task_controls() -> list[TaskControl]:
    """教师接管或覆盖过的任务。"""
    not_implemented("T24")


@router.patch("/tasks/{task_id}")
def override_task(task_id: int, body: TaskOverride) -> Task:
    """教师修改或覆盖计划，优先于 Agent 计划。"""
    not_implemented("T16")


@router.post("/tasks/{task_id}/takeover")
def take_over_task(task_id: int, body: TakeoverRequest) -> TaskControl:
    """教师接管辅导，暂停对应的辅导会话。"""
    not_implemented("T24")


@router.post("/tasks/{task_id}/handback")
def hand_back_task(task_id: int) -> TaskControl:
    """交还 Agent，会话从暂停处恢复。"""
    not_implemented("T24")


@router.get("/misconceptions/pending")
def list_pending_misconceptions() -> list[Misconception]:
    """辅导中标记的未建模错因，待教师审核。"""
    not_implemented("T16")


@router.post("/misconceptions/{misconception_id}/adopt")
def adopt_misconception(misconception_id: str, body: AdoptMisconception) -> Misconception:
    """采纳待审核错因，进入错因库，后续诊断可以检索到。"""
    not_implemented("T16")


@router.get("/escalations")
def list_escalations() -> list[Escalation]:
    not_implemented("T16")


@router.post("/escalations/{task_id}/resolve")
def resolve_escalation(task_id: int, body: ResolveEscalation) -> Task:
    """教师处理转交，记为一条单独的证据，不覆盖历史。"""
    not_implemented("T16")
