"""Read models for planner and teacher/student views."""

from collections import Counter

from sqlmodel import Session, select

from app.contracts.api import ClassOverview, DimensionResult, HeatCell, TimelineEntry
from app.contracts.entities import (
    Evidence,
    Hypothesis,
    KnowledgePoint,
    Mastery,
    Student,
    Task,
    TeachingGoal,
)
from app.contracts.enums import (
    Dimension,
    EvidenceKind,
    HypothesisStatus,
    MasteryState,
    TaskStatus,
    TaskType,
    TimelineKind,
)

RANK = {MasteryState.INITIAL: 1, MasteryState.CONSOLIDATED: 2, MasteryState.INTEGRATED: 3}


def current_goal(session: Session, kp_id: str | None = None) -> TeachingGoal | None:
    goals = session.exec(select(TeachingGoal).order_by(TeachingGoal.id.desc())).all()
    return next(
        (goal for goal in goals if kp_id is None or kp_id in goal.knowledge_point_ids), None
    )


def class_overview(session: Session) -> ClassOverview:
    students = session.exec(select(Student)).all()
    goal = current_goal(session)
    rows = session.exec(select(Mastery)).all()
    lookup = {(row.student_id, row.knowledge_point_id): row.state for row in rows}
    goal_met = 0
    states = Counter()
    for student in students:
        relevant = [row.state for row in rows if row.student_id == student.id]
        if relevant:
            state = min(relevant, key=lambda s: RANK.get(s, 0))
            states[state] += 1
        if (
            goal
            and goal.knowledge_point_ids
            and all(
                RANK.get(lookup.get((student.id, kp)), 0) >= RANK.get(goal.target_state, 99)
                for kp in goal.knowledge_point_ids
            )
        ):
            goal_met += 1
    escalated = len({row.student_id for row in rows if row.state == MasteryState.ESCALATED})
    return ClassOverview(
        students=len(students),
        goal_met=goal_met,
        goal_ratio=goal_met / len(students) if students else 0,
        escalated=escalated,
        by_state=dict(states),
    )


def heatmap(session: Session) -> list[HeatCell]:
    points = session.exec(
        select(KnowledgePoint).order_by(
            KnowledgePoint.lecture, KnowledgePoint.section, KnowledgePoint.id
        )
    ).all()
    rows = session.exec(select(Mastery)).all()
    hypotheses = session.exec(
        select(Hypothesis).where(Hypothesis.status == HypothesisStatus.SUPPORTED)
    ).all()
    tasks = {t.id: t for t in session.exec(select(Task)).all()}
    result = []
    for point in points:
        assessed = [r for r in rows if r.knowledge_point_id == point.id]
        confirmed = {
            h.student_id
            for h in hypotheses
            if h.task_id in tasks
            and tasks[h.task_id].knowledge_point_id == point.id
            and h.misconception_id != "slip"
        }
        result.append(
            HeatCell(
                knowledge_point_id=point.id,
                lecture=point.lecture,
                section=point.section,
                name=point.name,
                assessed=len(assessed),
                mastered_ratio=sum(r.state in RANK for r in assessed) / len(assessed)
                if assessed
                else 0,
                misconception_students=len(confirmed),
            )
        )
    return result


def verification(session: Session, student_id: str, kp_id: str) -> list[DimensionResult]:
    rows = session.exec(
        select(Evidence)
        .where(
            Evidence.student_id == student_id,
            Evidence.knowledge_point_id == kp_id,
            Evidence.kind == EvidenceKind.VERIFICATION,
        )
        .order_by(Evidence.teaching_date, Evidence.id)
    ).all()
    latest = {row.dimension: row for row in rows}
    return [
        DimensionResult(
            dimension=dimension,
            passed=row.is_correct if (row := latest.get(dimension)) else None,
            judged_by=row.judged_by if row else None,
            evidence_id=row.id if row else None,
        )
        for dimension in Dimension
    ]


def timeline(session: Session, student_id: str) -> list[TimelineEntry]:
    tasks = session.exec(select(Task).where(Task.student_id == student_id)).all()
    task_map = {t.id: t for t in tasks}
    rows = session.exec(
        select(Evidence)
        .where(Evidence.student_id == student_id)
        .order_by(Evidence.teaching_date, Evidence.id)
    ).all()
    kinds = {TaskType.RETEST: TimelineKind.RETEST, TaskType.PRECHECK: TimelineKind.PRECHECK}
    result = []
    for row in rows:
        task = task_map.get(row.task_id)
        kind = {
            EvidenceKind.DIAGNOSIS: TimelineKind.DIAGNOSIS,
            EvidenceKind.PRACTICE: TimelineKind.INTERVENTION,
            EvidenceKind.VERIFICATION: kinds.get(
                task.type if task else None, TimelineKind.VERIFICATION
            ),
        }.get(row.kind)
        if kind is not None:
            result.append(
                TimelineEntry(
                    teaching_date=row.teaching_date,
                    kind=kind,
                    knowledge_point_id=row.knowledge_point_id,
                    title=kind.value,
                    detail=row.reason,
                    task_id=row.task_id,
                    evidence_ids=[row.id],
                )
            )
    for task in tasks:
        if task.type == TaskType.ESCALATION and task.status != TaskStatus.CANCELLED:
            result.append(
                TimelineEntry(
                    teaching_date=task.created_on,
                    kind=TimelineKind.ESCALATION,
                    knowledge_point_id=task.knowledge_point_id,
                    title="转交教师",
                    detail=task.reason,
                    task_id=task.id,
                    evidence_ids=task.evidence_ids,
                )
            )
    return sorted(result, key=lambda entry: entry.teaching_date)
