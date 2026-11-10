"""教师驾驶舱与教学时钟。"""

from datetime import date

from fastapi import APIRouter, HTTPException, Request, UploadFile
from openai import OpenAIError
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app import teacher_collaboration as collaboration
from app.agents.planner.queries import common_issues
from app.clock.service import ClockError
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
from app.contracts.enums import ContentSource, TaskStatus, TaskType
from app.ingest import HomeworkImporter, IngestError, parse_csv
from app.ingest.service import MAX_CSV_BYTES
from app.ledger import queries
from app.llm.client import LLMOutputError, OpenAICompatClient
from app.orchestration.storage import TeacherTaskControl

router = APIRouter(tags=["teacher"])


@router.get("/clock")
def get_clock(request: Request) -> ClockState:
    return request.app.state.clock.state()


@router.post("/clock/advance")
async def advance_clock(body: AdvanceClock, request: Request) -> ClockState:
    """推进教学日期，扫描到期任务并生成事件。"""
    try:
        return await request.app.state.clock.advance(body.days)
    except ClockError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/clock/lecture")
async def enter_lecture(body: EnterLecture, request: Request) -> ClockState:
    """课程进入新章节，按三条规则创建章前预检。"""
    return await request.app.state.clock.enter_lecture(body.lecture)


@router.get("/goals")
def list_goals(request: Request) -> list[TeachingGoal]:
    with Session(request.app.state.orchestrator.engine) as session:
        return list(session.exec(select(TeachingGoal).order_by(TeachingGoal.id.desc())))


@router.post("/goals")
async def create_goal(body: GoalCreate, request: Request) -> TeachingGoal:
    return collaboration.set_goal(request.app.state.orchestrator, body)


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
    if request.state.principal.role == "student":
        student_id = request.state.principal.student_id
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
def list_task_controls(request: Request) -> list[TaskControl]:
    """教师接管或覆盖过的任务。"""
    with Session(request.app.state.orchestrator.engine) as session:
        return [
            TaskControl.model_validate(row, from_attributes=True)
            for row in session.exec(
                select(TeacherTaskControl)
                .where(TeacherTaskControl.taken_over | TeacherTaskControl.overridden)
                .order_by(TeacherTaskControl.task_id)
            )
        ]


@router.patch("/tasks/{task_id}")
async def override_task(task_id: int, body: TaskOverride, request: Request) -> Task:
    """教师修改或覆盖计划，优先于 Agent 计划。"""
    task = await collaboration.override_task(request.app.state.orchestrator, task_id, body)
    if body.due_date is not None and not body.cancel:
        await request.app.state.clock.scan()
    return task


@router.post("/tasks/{task_id}/takeover")
async def take_over_task(task_id: int, body: TakeoverRequest, request: Request) -> TaskControl:
    """教师接管辅导，暂停对应的辅导会话。"""
    return await collaboration.set_takeover(
        request.app.state.orchestrator, task_id, taken_over=True, note=body.note
    )


@router.post("/tasks/{task_id}/handback")
async def hand_back_task(task_id: int, request: Request) -> TaskControl:
    """交还 Agent，会话从暂停处恢复。"""
    return await collaboration.set_takeover(
        request.app.state.orchestrator, task_id, taken_over=False
    )


@router.get("/misconceptions/pending")
def list_pending_misconceptions(request: Request) -> list[Misconception]:
    """辅导中标记的未建模错因，待教师审核。"""
    with Session(request.app.state.orchestrator.engine) as session:
        return list(
            session.exec(
                select(Misconception)
                .where(Misconception.source == ContentSource.UNMODELED)
                .order_by(Misconception.id)
            )
        )


@router.post("/misconceptions/{misconception_id}/adopt")
async def adopt_misconception(
    misconception_id: str, body: AdoptMisconception, request: Request
) -> Misconception:
    """采纳待审核错因，进入错因库，后续诊断可以检索到。"""
    return await collaboration.adopt_misconception(
        request.app.state.orchestrator, request.app.state.knowledge, misconception_id, body
    )


@router.get("/escalations")
def list_escalations(request: Request) -> list[Escalation]:
    with Session(request.app.state.orchestrator.engine) as session:
        return collaboration.list_escalations(session)


@router.post("/escalations/{task_id}/resolve")
async def resolve_escalation(task_id: int, body: ResolveEscalation, request: Request) -> Task:
    """教师处理转交，记为一条单独的证据，不覆盖历史。"""
    return await collaboration.resolve_escalation(
        request.app.state.orchestrator, task_id, body.note
    )
