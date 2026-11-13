"""Agent 决策证据视图与知识路径。"""

from fastapi import APIRouter, HTTPException, Request
from sqlmodel import Session, select

from app.agents.tutor.queries import diagnosis
from app.classroom import ClassSession, active_class, mastery_rows, owned_classes
from app.contracts.api import DiagnosisView, DimensionResult, TimelineEntry
from app.contracts.entities import Evidence, KnowledgePoint, Mastery, Task
from app.contracts.enums import TaskStatus, TaskType
from app.ledger import queries

router = APIRouter(tags=["decision"])


def _question_in_progress(task: Task | None, request: Request) -> bool:
    return (
        request.state.principal.role == "student"
        and task is not None
        and task.type == TaskType.QUESTION
        and task.status in (TaskStatus.PENDING, TaskStatus.ACTIVE)
    )


@router.get("/tasks/{task_id}/diagnosis")
def get_diagnosis(task_id: int, request: Request) -> DiagnosisView:
    """假设面板：候选错因、各假设的预测、学生的实际回答、支持或排除。"""
    with Session(request.app.state.orchestrator.engine) as session:
        if _question_in_progress(session.get(Task, task_id), request):
            raise HTTPException(status_code=403, detail="请完成当前辅导后再查看诊断")
        result = diagnosis(session, task_id)
        if result is None:
            raise HTTPException(status_code=404, detail="任务不存在或尚无诊断记录")
        return result


@router.get("/students/{student_id}/timeline")
def get_timeline(student_id: str, request: Request) -> list[TimelineEntry]:
    principal = request.state.principal
    engine = request.app.state.orchestrator.engine
    if principal.role == "student" or principal.teacher_id is None:
        with Session(engine) as session:
            return queries.timeline(session, student_id)
    with Session(engine) as session:
        class_ids = list(session.exec(owned_classes(principal.teacher_id)))
    rows = []
    for class_id in class_ids:
        with ClassSession(engine, class_id, active_only=False) as session:
            rows.extend(queries.timeline(session, student_id))
    return sorted(rows, key=lambda row: row.teaching_date)


@router.get("/students/{student_id}/mastery")
def get_mastery(student_id: str, request: Request) -> list[Mastery]:
    return student_projection(
        request, student_id, lambda session: list(session.exec(mastery_rows(session, student_id)))
    )


def student_projection(request: Request, student_id: str, read):
    principal = request.state.principal
    engine = request.app.state.orchestrator.engine
    with Session(engine) as session:
        class_id = active_class(session, student_id)
        if principal.role == "teacher" and principal.teacher_id is not None:
            owned = list(session.exec(owned_classes(principal.teacher_id)))
            if class_id not in owned:
                from app.classroom import Enrollment

                class_id = (
                    session.exec(
                        select(Enrollment.class_id)
                        .where(Enrollment.student_id == student_id, Enrollment.class_id.in_(owned))
                        .order_by(Enrollment.class_id)
                    ).first()
                    or ""
                )
    with ClassSession(engine, class_id, active_only=False) as session:
        return read(session)


@router.get("/students/{student_id}/verification")
def get_verification(
    student_id: str, knowledge_point_id: str, request: Request
) -> list[DimensionResult]:
    """六维验证结果及判定方式。"""
    return student_projection(
        request,
        student_id,
        lambda session: queries.verification(session, student_id, knowledge_point_id),
    )


@router.get("/evidence/{evidence_id}")
def get_evidence(evidence_id: int, request: Request) -> Evidence:
    with Session(request.app.state.orchestrator.engine) as session:
        evidence = session.get(Evidence, evidence_id)
        if evidence is None:
            raise HTTPException(status_code=404, detail="证据不存在")
        if evidence.task_id is not None and _question_in_progress(
            session.get(Task, evidence.task_id), request
        ):
            raise HTTPException(status_code=403, detail="请完成当前辅导后再查看证据")
        return evidence


@router.get("/knowledge/points")
def list_points(request: Request, lecture: int | None = None) -> list[KnowledgePoint]:
    knowledge = request.app.state.knowledge
    return knowledge.all_points() if lecture is None else knowledge.lecture_points(lecture)


@router.get("/knowledge/path")
def get_path(request: Request, source: str, target: str) -> list[KnowledgePoint]:
    """跨章节知识路径，如第 1→2→9 讲；不连通时为空列表。"""
    knowledge = request.app.state.knowledge
    for kp_id in (source, target):
        if kp_id not in knowledge:
            raise HTTPException(status_code=404, detail=f"知识点不存在：{kp_id}")
    return knowledge.path(source, target)
