"""Deterministic mastery transitions in the caller's business transaction."""

from datetime import date

from sqlalchemy import Engine, text
from sqlmodel import Session, select

from app.contracts.entities import Evidence, Hypothesis, Mastery, Misconception, Task
from app.contracts.enums import (
    ContentSource,
    Dimension,
    EvidenceKind,
    HypothesisStatus,
    MasteryState,
    TaskType,
)
from app.contracts.values import SLIP
from app.ledger.queries import current_goal, has_open_escalation, last_escalation_closure


def protect_evidence(engine: Engine) -> None:
    """Protect the ledger even from bulk SQL and other sessions."""
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TRIGGER IF NOT EXISTS evidence_no_replace BEFORE INSERT ON evidence "
                "WHEN EXISTS (SELECT 1 FROM evidence WHERE id = NEW.id) BEGIN "
                "SELECT RAISE(ABORT, 'evidence is append-only'); END"
            )
        )
        for operation in ("UPDATE", "DELETE"):
            connection.execute(
                text(
                    f"CREATE TRIGGER IF NOT EXISTS evidence_no_{operation.lower()} "
                    f"BEFORE {operation} ON evidence BEGIN "
                    "SELECT RAISE(ABORT, 'evidence is append-only'); END"
                )
            )


def _set_state(
    session: Session,
    student_id: str,
    kp_id: str,
    state: MasteryState,
    teaching_date: date,
    evidence_ids: list[int],
) -> Mastery:
    mastery = session.get(Mastery, (student_id, kp_id))
    if mastery is None:
        mastery = Mastery(
            student_id=student_id, knowledge_point_id=kp_id, state=state, updated_on=teaching_date
        )
    elif teaching_date < mastery.updated_on:
        return mastery
    mastery.state = state
    mastery.updated_on = teaching_date
    mastery.evidence_ids = list(dict.fromkeys([*mastery.evidence_ids, *evidence_ids]))
    session.add(mastery)
    session.flush()
    return mastery


def start_tutoring(session: Session, task: Task, teaching_date: date) -> None:
    if task.type == TaskType.TUTORING and not has_open_escalation(
        session, task.student_id, task.knowledge_point_id
    ):
        _set_state(
            session,
            task.student_id,
            task.knowledge_point_id,
            MasteryState.TUTORING,
            teaching_date,
            task.evidence_ids,
        )


def escalation_is_eligible(session: Session, task: Task, *, basis: str | None = None) -> bool:
    if task.type != TaskType.ESCALATION or task.student_id is None:
        return False
    goal = current_goal(session, task.knowledge_point_id)
    round_limit = goal is not None and task.round >= goal.max_rounds
    if basis == "round_limit":
        return round_limit
    unknown_ids = {
        m.id
        for m in session.exec(
            select(Misconception).where(Misconception.source == ContentSource.UNMODELED)
        ).all()
    }
    unmodeled = bool(unknown_ids.intersection(task.misconception_ids)) and any(
        (e := session.get(Evidence, eid)) is not None
        and e.is_correct is None
        and e.kind != EvidenceKind.TEACHER
        and e.student_id == task.student_id
        and e.knowledge_point_id == task.knowledge_point_id
        for eid in task.evidence_ids
    )
    return unmodeled if basis == "unmodeled" else unmodeled or round_limit


def escalate(session: Session, task: Task, teaching_date: date) -> None:
    """Only an actual persisted escalation decision can enter this state."""
    if task.type != TaskType.ESCALATION or task.student_id is None:
        raise ValueError("An escalation task for a student is required")
    if not escalation_is_eligible(session, task):
        raise ValueError("Escalation needs the goal's round limit or undecidable evidence")
    _set_state(
        session,
        task.student_id,
        task.knowledge_point_id,
        MasteryState.ESCALATED,
        teaching_date,
        task.evidence_ids,
    )


def append_evidence(session: Session, evidence: Evidence) -> Evidence:
    """Append and derive mastery; never commit, never trust a supplied mastery label."""
    if evidence.id is not None:
        raise ValueError("Only new evidence may be appended")
    session.add(evidence)
    session.flush()
    if evidence.kind == EvidenceKind.TEACHER:
        return evidence
    if has_open_escalation(session, evidence.student_id, evidence.knowledge_point_id):
        return evidence
    closed_at = last_escalation_closure(session, evidence.student_id, evidence.knowledge_point_id)
    if closed_at and (evidence.teaching_date, evidence.id) <= closed_at:
        return evidence
    mastery = session.get(Mastery, (evidence.student_id, evidence.knowledge_point_id))
    if mastery is not None and evidence.teaching_date < mastery.updated_on:
        return evidence
    state = None
    basis = [evidence.id]
    confirmed = (
        session.exec(
            select(Hypothesis).where(
                Hypothesis.student_id == evidence.student_id,
                Hypothesis.misconception_id == evidence.matched_misconception_id,
                Hypothesis.status == HypothesisStatus.SUPPORTED,
            )
        ).first()
        if evidence.matched_misconception_id not in (None, SLIP)
        else None
    )
    task = session.get(Task, evidence.task_id) if evidence.task_id is not None else None
    recurring = confirmed is not None or (
        task is not None
        and evidence.matched_misconception_id not in (None, SLIP)
        and evidence.matched_misconception_id in task.misconception_ids
    )
    if recurring and evidence.is_correct is False and mastery is not None:
        state = MasteryState.RELAPSED
    elif evidence.is_correct is False and (
        evidence.kind == EvidenceKind.HOMEWORK
        or evidence.dimension == Dimension.CROSS_CHAPTER
        or (task is not None and task.type == TaskType.PRECHECK)
    ):
        state = MasteryState.NEEDS_INTERVENTION
    elif evidence.kind == EvidenceKind.VERIFICATION and evidence.is_correct is True:
        if (
            evidence.dimension == Dimension.RETENTION
            and mastery is not None
            and mastery.state
            in (MasteryState.INITIAL, MasteryState.CONSOLIDATED, MasteryState.INTEGRATED)
        ):
            state = (
                MasteryState.INTEGRATED
                if mastery.state == MasteryState.INTEGRATED
                else MasteryState.CONSOLIDATED
            )
        elif (
            evidence.dimension == Dimension.CROSS_CHAPTER
            and mastery is not None
            and mastery.state
            in (MasteryState.INITIAL, MasteryState.CONSOLIDATED, MasteryState.INTEGRATED)
        ):
            state = MasteryState.INTEGRATED
        elif (
            (mastery is None or mastery.state != MasteryState.RELAPSED)
            and evidence.task_id is not None
            and evidence.dimension
            in (
                Dimension.CORRECTNESS,
                Dimension.DISTRACTOR,
                Dimension.NEAR_TRANSFER,
                Dimension.EXPLANATION,
            )
        ):
            rows = session.exec(
                select(Evidence)
                .where(
                    Evidence.task_id == evidence.task_id,
                    Evidence.student_id == evidence.student_id,
                    Evidence.knowledge_point_id == evidence.knowledge_point_id,
                    Evidence.kind == EvidenceKind.VERIFICATION,
                )
                .order_by(Evidence.id)
            ).all()
            failures = session.exec(
                select(Evidence).where(
                    Evidence.student_id == evidence.student_id,
                    Evidence.knowledge_point_id == evidence.knowledge_point_id,
                    Evidence.is_correct == False,  # noqa: E712
                    Evidence.kind != EvidenceKind.TEACHER,
                    Evidence.id < evidence.id,
                )
            ).all()
            cutoff = max(
                (
                    (row.teaching_date, row.id)
                    for row in failures
                    if (
                        row.matched_misconception_id not in (None, SLIP)
                        or row.kind == EvidenceKind.HOMEWORK
                        or row.dimension == Dimension.CROSS_CHAPTER
                        or (
                            row.task_id is not None
                            and session.get(Task, row.task_id).type == TaskType.PRECHECK
                        )
                    )
                ),
                default=(date.min, 0),
            )
            if closed_at:
                cutoff = max(cutoff, closed_at)
            rows = sorted(
                (
                    row
                    for row in rows
                    if cutoff < (row.teaching_date, row.id)
                    and row.teaching_date <= evidence.teaching_date
                ),
                key=lambda row: (row.teaching_date, row.id),
            )
            latest = {row.dimension: row for row in rows}
            passed = {dim for dim, row in latest.items() if row.is_correct is True}
            if {Dimension.CORRECTNESS, Dimension.DISTRACTOR} <= passed and passed & {
                Dimension.NEAR_TRANSFER,
                Dimension.EXPLANATION,
            }:
                state = MasteryState.INITIAL
                if mastery is not None and mastery.state in (
                    MasteryState.CONSOLIDATED,
                    MasteryState.INTEGRATED,
                ):
                    state = mastery.state
                basis = [row.id for row in latest.values()]
    if state is not None:
        _set_state(
            session,
            evidence.student_id,
            evidence.knowledge_point_id,
            state,
            evidence.teaching_date,
            basis,
        )
    return evidence
