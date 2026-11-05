"""Agent 决策证据视图与知识路径。"""

from fastapi import APIRouter

from app.api.stub import not_implemented
from app.contracts.api import DiagnosisView, DimensionResult, TimelineEntry
from app.contracts.entities import Evidence, KnowledgePoint, Mastery

router = APIRouter(tags=["decision"])


@router.get("/tasks/{task_id}/diagnosis")
def get_diagnosis(task_id: int) -> DiagnosisView:
    """假设面板：候选错因、各假设的预测、学生的实际回答、支持或排除。"""
    not_implemented("T09")


@router.get("/students/{student_id}/timeline")
def get_timeline(student_id: str) -> list[TimelineEntry]:
    not_implemented("T19")


@router.get("/students/{student_id}/mastery")
def get_mastery(student_id: str) -> list[Mastery]:
    not_implemented("T19")


@router.get("/students/{student_id}/verification")
def get_verification(student_id: str, knowledge_point_id: str) -> list[DimensionResult]:
    """六维验证结果及判定方式。"""
    not_implemented("T19")


@router.get("/evidence/{evidence_id}")
def get_evidence(evidence_id: int) -> Evidence:
    not_implemented("T19")


@router.get("/knowledge/points")
def list_points(lecture: int | None = None) -> list[KnowledgePoint]:
    not_implemented("T04")


@router.get("/knowledge/path")
def get_path(source: str, target: str) -> list[KnowledgePoint]:
    """跨章节知识路径，如第 1→2→9 讲。"""
    not_implemented("T04")
