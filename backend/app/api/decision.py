"""Agent 决策证据视图与知识路径。"""

from fastapi import APIRouter, HTTPException, Request
from sqlmodel import Session, select

from app.api.stub import not_implemented
from app.contracts.api import DiagnosisView, DimensionResult, TimelineEntry
from app.contracts.entities import Evidence, KnowledgePoint, Mastery
from app.ledger import queries

router = APIRouter(tags=["decision"])


@router.get("/tasks/{task_id}/diagnosis")
def get_diagnosis(task_id: int) -> DiagnosisView:
    """假设面板：候选错因、各假设的预测、学生的实际回答、支持或排除。"""
    not_implemented("T09")


@router.get("/students/{student_id}/timeline")
def get_timeline(student_id: str, request: Request) -> list[TimelineEntry]:
    with Session(request.app.state.orchestrator.engine) as session:
        return queries.timeline(session, student_id)


@router.get("/students/{student_id}/mastery")
def get_mastery(student_id: str, request: Request) -> list[Mastery]:
    with Session(request.app.state.orchestrator.engine) as session:
        return list(session.exec(select(Mastery).where(Mastery.student_id == student_id)).all())


@router.get("/students/{student_id}/verification")
def get_verification(
    student_id: str, knowledge_point_id: str, request: Request
) -> list[DimensionResult]:
    """六维验证结果及判定方式。"""
    with Session(request.app.state.orchestrator.engine) as session:
        return queries.verification(session, student_id, knowledge_point_id)


@router.get("/evidence/{evidence_id}")
def get_evidence(evidence_id: int, request: Request) -> Evidence:
    with Session(request.app.state.orchestrator.engine) as session:
        evidence = session.get(Evidence, evidence_id)
        if evidence is None:
            raise HTTPException(status_code=404, detail="证据不存在")
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
