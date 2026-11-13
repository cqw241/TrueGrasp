"""教师驾驶舱与教学时钟。"""

from fastapi import APIRouter, HTTPException, Request, UploadFile
from openai import OpenAIError
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app import teacher_collaboration as collaboration
from app.agents.planner.queries import common_issues
from app.classroom import Classroom, ClassSession, owned_classes
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


def selected_class(request: Request, class_id: str | None, *, required: bool = False) -> str | None:
    teacher_id = request.state.principal.teacher_id
    with Session(request.app.state.orchestrator.engine) as session:
        owned = list(session.exec(owned_classes(teacher_id).order_by(Classroom.name, Classroom.id)))
    if class_id is not None:
        if class_id not in owned:
            raise HTTPException(404, "班级不存在")
        return class_id
    if required and teacher_id is not None and len(owned) != 1:
        raise HTTPException(422, "请选择班级")
    return owned[0] if owned else None


def class_read(request: Request, class_id: str | None, *, active_only: bool = True) -> Session:
    selected = selected_class(request, class_id)
    if selected is None and request.state.principal.teacher_id is not None:
        selected = ""  # A teacher without classes may not read unassigned rows.
    return ClassSession(request.app.state.orchestrator.engine, selected, active_only=active_only)


@router.get("/clock")
def get_clock(request: Request, class_id: str | None = None) -> ClockState:
    return request.app.state.clock.state(
        selected_class(request, class_id) or ("" if request.state.principal.teacher_id else None)
    )


@router.post("/clock/advance")
async def advance_clock(
    body: AdvanceClock, request: Request, class_id: str | None = None
) -> ClockState:
    """推进教学日期，扫描到期任务并生成事件。"""
    try:
        return await request.app.state.clock.advance(
            body.days, class_id=selected_class(request, class_id, required=True)
        )
    except ClockError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/clock/lecture")
async def enter_lecture(
    body: EnterLecture, request: Request, class_id: str | None = None
) -> ClockState:
    """课程进入新章节，按三条规则创建章前预检。"""
    return await request.app.state.clock.enter_lecture(
        body.lecture, class_id=selected_class(request, class_id, required=True)
    )


@router.get("/goals")
def list_goals(request: Request, class_id: str | None = None) -> list[TeachingGoal]:
    with class_read(request, class_id) as session:
        return list(session.exec(select(TeachingGoal).order_by(TeachingGoal.id.desc())))


@router.post("/goals")
async def create_goal(
    body: GoalCreate, request: Request, class_id: str | None = None
) -> TeachingGoal:
    return await collaboration.set_goal(
        request.app.state.orchestrator,
        body,
        class_id=selected_class(request, class_id, required=True),
    )


@router.post("/homework")
async def import_homework(
    file: UploadFile, request: Request, class_id: str | None = None
) -> HomeworkImportResult:
    """导入结构化作业 CSV，学生答案为 LaTeX。"""
    runtime = request.app.state.orchestrator
    data = await file.read(MAX_CSV_BYTES + 1)
    class_id = selected_class(request, class_id, required=True)
    teaching_date = request.app.state.clock.state(class_id).teaching_date

    async def ingest(llm) -> HomeworkImportResult:
        return await HomeworkImporter(runtime.engine, llm).import_rows(
            rows, teaching_date, class_id=class_id
        )

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
def class_overview(request: Request, class_id: str | None = None) -> ClassOverview:
    with class_read(request, class_id) as session:
        return queries.class_overview(session)


@router.get("/class/heatmap")
def class_heatmap(request: Request, class_id: str | None = None) -> list[HeatCell]:
    with class_read(request, class_id) as session:
        return queries.heatmap(session)


@router.get("/class/issues")
def class_issues(request: Request, class_id: str | None = None) -> list[CommonIssue]:
    """共性问题与课上讲解建议。"""
    with class_read(request, class_id, active_only=False) as session:
        return common_issues(session)


@router.get("/tasks")
def list_tasks(
    request: Request,
    student_id: str | None = None,
    type: TaskType | None = None,
    status: TaskStatus | None = None,
    class_id: str | None = None,
) -> list[Task]:
    """干预计划；学生辅导台也用它取任务卡。"""
    principal = request.state.principal
    if principal.role == "student":
        student_id = principal.student_id
    query = select(Task).order_by(Task.id.desc())
    if student_id is not None:
        query = query.where(Task.student_id == student_id)
    if type is not None:
        query = query.where(Task.type == type)
    if status is not None:
        query = query.where(Task.status == status)
    if principal.role == "teacher" and principal.teacher_id is not None:
        allowed = (
            [selected_class(request, class_id)]
            if class_id is not None
            else owned_classes(principal.teacher_id)
        )
        query = query.where(Task.class_id.in_(allowed))
    with Session(request.app.state.orchestrator.engine) as session:
        return list(session.exec(query))


@router.get("/tasks/controls")
def list_task_controls(request: Request, class_id: str | None = None) -> list[TaskControl]:
    """教师接管或覆盖过的任务。"""
    with class_read(request, class_id) as session:
        return [
            TaskControl.model_validate(row, from_attributes=True)
            for row in session.exec(
                select(TeacherTaskControl)
                .join(Task, Task.id == TeacherTaskControl.task_id)
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
def list_pending_misconceptions(
    request: Request, class_id: str | None = None
) -> list[Misconception]:
    """辅导中标记的未建模错因，待教师审核。"""
    with class_read(request, class_id) as session:
        return list(
            session.exec(
                select(Misconception)
                .where(
                    Misconception.source == ContentSource.UNMODELED,
                    Misconception.class_id == session.info["class_id"],
                )
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
def list_escalations(request: Request, class_id: str | None = None) -> list[Escalation]:
    with class_read(request, class_id, active_only=False) as session:
        return collaboration.list_escalations(session)


@router.post("/escalations/{task_id}/resolve")
async def resolve_escalation(task_id: int, body: ResolveEscalation, request: Request) -> Task:
    """教师处理转交，记为一条单独的证据，不覆盖历史。"""
    return await collaboration.resolve_escalation(
        request.app.state.orchestrator, task_id, body.note
    )
