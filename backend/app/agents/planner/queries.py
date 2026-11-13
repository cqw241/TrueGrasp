"""Evidence-backed discovery shared by planning and the teacher's common-issue view."""

from collections import defaultdict

from sqlmodel import Session, select

from app.classroom import mastery_for
from app.contracts.api import CommonIssue
from app.contracts.entities import Evidence, Misconception, Task
from app.contracts.enums import EvidenceKind, TaskStatus, TaskType
from app.contracts.values import SLIP
from app.ledger.queries import RANK, confirmed_hypotheses
from app.orchestration.storage import PlanningTaskContext


def homework_groups(session: Session, kp_ids: set[str]) -> dict:
    """Latest homework per student/point, refined by a uniquely supported diagnosis."""
    rows = session.exec(
        select(Evidence)
        .where(Evidence.kind == EvidenceKind.HOMEWORK, Evidence.knowledge_point_id.in_(kp_ids))
        .order_by(Evidence.teaching_date, Evidence.id)
    ).all()
    latest = {(row.student_id, row.knowledge_point_id): row for row in rows}
    confirmed = {h.error_evidence_id: h for h in confirmed_hypotheses(session)}
    groups = defaultdict(list)
    for row in latest.values():
        if row.is_correct is not False:
            continue
        mastery = mastery_for(session, row.student_id, row.knowledge_point_id, row.class_id)
        if mastery and mastery.state in RANK:
            continue
        hypothesis = confirmed.get(row.id)
        cause = hypothesis.misconception_id if hypothesis else row.matched_misconception_id
        if cause in (None, SLIP):
            continue
        mis = session.get(Misconception, cause)
        if mis is None or mis.knowledge_point_id != row.knowledge_point_id:
            continue
        evidence_ids = [row.id]
        if hypothesis:
            evidence_ids.extend(hypothesis.evidence_ids)
        groups[(row.knowledge_point_id, cause)].append(
            (row.student_id, sorted(set(evidence_ids)), hypothesis is not None)
        )
    return groups


def common_issues(session: Session) -> list[CommonIssue]:
    result = []
    for task in session.exec(
        select(Task)
        .where(Task.type == TaskType.CLASS_REVIEW, Task.status != TaskStatus.CANCELLED)
        .order_by(Task.id.desc())
    ):
        context = session.get(PlanningTaskContext, task.id)
        cause = context.issue_misconception_id if context else None
        if cause is None and len(task.misconception_ids) == 1:
            cause = task.misconception_ids[0]
        mis = session.get(Misconception, cause) if cause else None
        if mis is None:
            continue
        students = sorted(
            {
                row.student_id
                for row in session.exec(select(Evidence).where(Evidence.id.in_(task.evidence_ids)))
                if row.knowledge_point_id == task.knowledge_point_id
            }
        )
        result.append(
            CommonIssue(
                task_id=task.id,
                knowledge_point_id=task.knowledge_point_id,
                misconception_id=mis.id,
                description=mis.description,
                student_ids=students,
                suggestion=task.reason,
            )
        )
    return result
