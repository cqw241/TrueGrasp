"""Read models for planner and teacher/student views."""

from collections import Counter, defaultdict
from datetime import date

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


def confirmed_hypotheses(session: Session) -> list[Hypothesis]:
    """与 T08 一致：同一次错误诊断只剩一个未排除候选，且该候选受支持，才确诊。"""
    groups = defaultdict(list)
    for hypothesis in session.exec(select(Hypothesis).order_by(Hypothesis.id)):
        groups[(hypothesis.task_id, hypothesis.error_evidence_id)].append(hypothesis)
    result = []
    for group in groups.values():
        alive = [h for h in group if h.status != HypothesisStatus.REFUTED]
        if len(alive) == 1 and alive[0].status == HypothesisStatus.SUPPORTED:
            result.append(alive[0])
    return result


def current_goal(session: Session, kp_id: str | None = None) -> TeachingGoal | None:
    goals = session.exec(select(TeachingGoal).order_by(TeachingGoal.id.desc())).all()
    return next(
        (goal for goal in goals if kp_id is None or kp_id in goal.knowledge_point_ids), None
    )


def has_open_escalation(session: Session, student_id: str, kp_id: str) -> bool:
    return (
        session.exec(
            select(Task.id).where(
                Task.student_id == student_id,
                Task.knowledge_point_id == kp_id,
                Task.type == TaskType.ESCALATION,
                Task.status.in_((TaskStatus.PENDING, TaskStatus.ACTIVE)),
            )
        ).first()
        is not None
    )


def last_escalation_closure(
    session: Session, student_id: str, kp_id: str
) -> tuple[date, int] | None:
    # A teacher's resolution/cancellation is the last control record for a
    # closed escalation. Adoption records are associated with a misconception.
    row = session.exec(
        select(Evidence.teaching_date, Evidence.id)
        .join(Task, Task.id == Evidence.task_id)
        .where(
            Evidence.student_id == student_id,
            Evidence.knowledge_point_id == kp_id,
            Evidence.kind == EvidenceKind.TEACHER,
            Evidence.matched_misconception_id == None,  # noqa: E711
            Task.type == TaskType.ESCALATION,
            Task.status.in_((TaskStatus.DONE, TaskStatus.CANCELLED)),
        )
        .order_by(Evidence.teaching_date.desc(), Evidence.id.desc())
    ).first()
    return (row[0], row[1]) if row else None


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
    escalated = len(
        set(
            session.exec(
                select(Task.student_id).where(
                    Task.type == TaskType.ESCALATION,
                    Task.status.in_((TaskStatus.PENDING, TaskStatus.ACTIVE)),
                )
            )
        )
    )
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
    # Orchestration uses the ledger service; defer this import to avoid a cycle.
    from app.orchestration.storage import BACKTRACK_REASON_PREFIX, PlanningTaskContext

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
            question = task is not None and task.type == TaskType.QUESTION
            result.append(
                TimelineEntry(
                    teaching_date=row.teaching_date,
                    kind=kind,
                    knowledge_point_id=row.knowledge_point_id,
                    title="主动提问"
                    if question and row.reason.startswith("学生主动提问")
                    else kind.value,
                    detail=row.reason,
                    task_id=row.task_id,
                    evidence_ids=[row.id],
                )
            )
    backtrack_candidates = [
        task
        for task in tasks
        if task.type == TaskType.VERIFICATION
        and task.status in (TaskStatus.ACTIVE, TaskStatus.DONE)
        and task.reason.startswith(BACKTRACK_REASON_PREFIX)
    ]
    contexts = (
        {
            row.task_id: row
            for row in session.exec(
                select(PlanningTaskContext).where(
                    PlanningTaskContext.task_id.in_([task.id for task in backtrack_candidates])
                )
            )
        }
        if backtrack_candidates
        else {}
    )
    for task in tasks:
        context = contexts.get(task.id)
        if context is not None and context.root_knowledge_point_id != task.knowledge_point_id:
            first_answer = next(
                (
                    row
                    for row in rows
                    if row.task_id == task.id and row.kind == EvidenceKind.VERIFICATION
                ),
                None,
            )
            event_date = first_answer.teaching_date if first_answer else task.created_on
            date_label = "首条作答日期" if first_answer else "任务创建日期"
            entry = TimelineEntry(
                teaching_date=event_date,
                kind=TimelineKind.BACKTRACK,
                knowledge_point_id=task.knowledge_point_id,
                title="先修回溯",
                detail=f"{date_label}：{event_date}；{task.reason}",
                task_id=task.id,
                evidence_ids=task.evidence_ids,
            )
            # A same-day entry check must follow its backtrack, not precede it.
            position = next(
                (
                    index
                    for index, row in enumerate(result)
                    if row.task_id == task.id and row.teaching_date == event_date
                ),
                len(result),
            )
            result.insert(position, entry)
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
