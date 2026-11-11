"""T19 acceptance: every transition, independent evidence and immutable ledger."""

from datetime import date, timedelta

import pytest
from httpx2 import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.contracts.entities import (
    Evidence,
    KnowledgePoint,
    Mastery,
    Misconception,
    Student,
    Task,
    TeachingGoal,
)
from app.contracts.enums import (
    ContentSource,
    Dimension,
    EvidenceKind,
    JudgedBy,
    MasteryState,
    TaskStatus,
    TaskType,
    TimelineKind,
)
from app.db import init_db, make_engine
from app.ledger import queries
from app.ledger.service import append_evidence, escalate, start_tutoring
from app.main import create_app
from app.orchestration.storage import PlanningTaskContext

TODAY = date(2026, 9, 7)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def session(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'ledger.db'}")
    init_db(engine)
    with Session(engine) as session:
        session.add_all([Student(id="A", name="A"), Student(id="B", name="B")])
        session.add(KnowledgePoint(id="kp", name="chain", lecture=2, section=2))
        session.flush()
        session.add(
            Misconception(
                id="miss", knowledge_point_id="kp", family="chain", description="missing rate"
            )
        )
        session.add(
            TeachingGoal(knowledge_point_ids=["kp"], created_on=TODAY, deadline=TODAY, max_rounds=2)
        )
        session.add(
            Task(
                id=1,
                type=TaskType.TUTORING,
                student_id="A",
                knowledge_point_id="kp",
                created_on=TODAY,
                misconception_ids=["miss"],
            )
        )
        session.commit()
        yield session
    engine.dispose()


def evidence(
    session,
    dimension=None,
    *,
    correct=True,
    kind=EvidenceKind.VERIFICATION,
    day=0,
    student="A",
    matched=None,
    task_id=1,
):
    return append_evidence(
        session,
        Evidence(
            student_id=student,
            knowledge_point_id="kp",
            kind=kind,
            dimension=dimension,
            task_id=task_id,
            is_correct=correct,
            matched_misconception_id=matched,
            judged_by=JudgedBy.TOOL,
            teaching_date=TODAY + timedelta(days=day),
        ),
    )


def state(session):
    row = session.get(Mastery, ("A", "kp"))
    return row.state if row else None


def initial(session):
    for dimension in (Dimension.CORRECTNESS, Dimension.DISTRACTOR, Dimension.NEAR_TRANSFER):
        evidence(session, dimension)


def test_all_mastery_transitions(session):
    evidence(session, correct=False, kind=EvidenceKind.HOMEWORK, task_id=None)
    assert state(session) == MasteryState.NEEDS_INTERVENTION
    start_tutoring(session, session.get(Task, 1), TODAY)
    assert state(session) == MasteryState.TUTORING
    evidence(session, Dimension.CORRECTNESS)
    evidence(session, Dimension.DISTRACTOR)
    assert state(session) == MasteryState.TUTORING
    evidence(session, Dimension.EXPLANATION)
    assert state(session) == MasteryState.INITIAL
    evidence(session, Dimension.RETENTION, day=4)
    assert state(session) == MasteryState.CONSOLIDATED
    evidence(session, Dimension.CROSS_CHAPTER, day=20)
    assert state(session) == MasteryState.INTEGRATED
    evidence(session, Dimension.CROSS_CHAPTER, correct=False, matched="miss", day=21)
    assert state(session) == MasteryState.RELAPSED
    start_tutoring(session, session.get(Task, 1), TODAY + timedelta(days=21))
    assert state(session) == MasteryState.TUTORING
    task = Task(
        type=TaskType.ESCALATION, student_id="A", knowledge_point_id="kp", created_on=TODAY, round=2
    )
    session.add(task)
    session.flush()
    escalate(session, task, TODAY + timedelta(days=22))
    assert state(session) == MasteryState.ESCALATED


def test_only_independent_evidence_can_certify(session):
    for kind in (EvidenceKind.DIAGNOSIS, EvidenceKind.PRACTICE, EvidenceKind.TEACHER):
        for dimension in (Dimension.CORRECTNESS, Dimension.DISTRACTOR, Dimension.EXPLANATION):
            evidence(session, dimension, kind=kind)
    assert state(session) is None
    evidence(session, Dimension.RETENTION)
    evidence(session, Dimension.CROSS_CHAPTER)
    assert state(session) is None


def test_open_and_resolved_escalation_do_not_pool_old_passes(session):
    transfer = Task(
        type=TaskType.ESCALATION, student_id="A", knowledge_point_id="kp", created_on=TODAY, round=2
    )
    session.add(transfer)
    session.flush()
    escalate(session, transfer, TODAY)
    initial(session)
    start_tutoring(session, session.get(Task, 1), TODAY)
    evidence(session, correct=False, kind=EvidenceKind.HOMEWORK)
    assert state(session) == MasteryState.ESCALATED

    evidence(session, kind=EvidenceKind.TEACHER, correct=None, task_id=transfer.id)
    transfer.status = "done"
    evidence(session, Dimension.NEAR_TRANSFER)
    evidence(session, Dimension.CORRECTNESS)
    assert state(session) == MasteryState.ESCALATED
    evidence(session, Dimension.DISTRACTOR)
    assert state(session) == MasteryState.INITIAL


def test_students_and_verification_sessions_cannot_pool_passes(session):
    evidence(session, Dimension.CORRECTNESS)
    evidence(session, Dimension.DISTRACTOR, student="B")
    session.add(
        Task(
            id=2,
            type=TaskType.VERIFICATION,
            student_id="A",
            knowledge_point_id="kp",
            created_on=TODAY,
        )
    )
    session.flush()
    evidence(session, Dimension.NEAR_TRANSFER, task_id=2)
    assert state(session) is None


def test_failed_precheck_and_old_evidence(session):
    initial(session)
    evidence(session, Dimension.CROSS_CHAPTER, correct=False, day=20)
    assert state(session) == MasteryState.NEEDS_INTERVENTION
    evidence(session, Dimension.RETENTION, day=4)
    assert state(session) == MasteryState.NEEDS_INTERVENTION


def test_precheck_correctness_failure_requires_intervention(session):
    initial(session)
    session.add(
        Task(
            id=2, type=TaskType.PRECHECK, student_id="A", knowledge_point_id="kp", created_on=TODAY
        )
    )
    session.flush()
    evidence(session, Dimension.CORRECTNESS, correct=False, task_id=2)
    assert state(session) == MasteryState.NEEDS_INTERVENTION


def test_relapse_invalidates_prior_passes(session):
    initial(session)
    evidence(session, Dimension.NEAR_TRANSFER, correct=False, matched="miss")
    assert state(session) == MasteryState.RELAPSED
    evidence(session, Dimension.EXPLANATION)
    assert state(session) == MasteryState.RELAPSED
    start_tutoring(session, session.get(Task, 1), TODAY)
    evidence(session, Dimension.EXPLANATION)
    assert state(session) == MasteryState.TUTORING
    evidence(session, Dimension.CORRECTNESS)
    evidence(session, Dimension.DISTRACTOR)
    assert state(session) == MasteryState.INITIAL


def test_backfilled_error_does_not_invalidate_newer_verification(session):
    start_tutoring(session, session.get(Task, 1), TODAY + timedelta(days=10))
    evidence(session, Dimension.CORRECTNESS, day=10)
    evidence(session, Dimension.DISTRACTOR, day=10)
    evidence(session, correct=False, kind=EvidenceKind.HOMEWORK, day=1)
    evidence(session, Dimension.NEAR_TRANSFER, day=10)
    assert state(session) == MasteryState.INITIAL


def test_cross_chapter_failure_invalidates_earlier_passes(session):
    initial(session)
    evidence(session, Dimension.CROSS_CHAPTER, correct=False)
    evidence(session, Dimension.EXPLANATION)
    assert state(session) == MasteryState.NEEDS_INTERVENTION


def test_teacher_note_is_not_undecidable_answer(session):
    unknown = evidence(session, correct=None, kind=EvidenceKind.TEACHER)
    session.add(
        Misconception(
            id="unknown",
            knowledge_point_id="kp",
            family="unmodeled",
            description="unresolved",
            source=ContentSource.UNMODELED,
        )
    )
    session.flush()
    task = Task(
        type=TaskType.ESCALATION,
        student_id="A",
        knowledge_point_id="kp",
        round=1,
        misconception_ids=["unknown"],
        evidence_ids=[unknown.id],
        created_on=TODAY,
    )
    session.add(task)
    session.flush()
    with pytest.raises(ValueError, match="round limit"):
        escalate(session, task, TODAY)


def test_teacher_note_does_not_break_verification_accumulation(session):
    start_tutoring(session, session.get(Task, 1), TODAY)
    evidence(session, Dimension.CORRECTNESS)
    evidence(session, Dimension.DISTRACTOR)
    evidence(session, correct=False, kind=EvidenceKind.TEACHER, matched="miss")
    evidence(session, Dimension.NEAR_TRANSFER)
    assert state(session) == MasteryState.INITIAL


def test_slip_is_not_a_recurring_misconception(session):
    initial(session)
    session.get(Task, 1).misconception_ids = ["slip"]
    session.flush()
    evidence(session, Dimension.CORRECTNESS, correct=False, matched="slip", day=1)
    assert state(session) != MasteryState.RELAPSED


def test_escalation_uses_latest_applicable_goal(session):
    session.add(
        TeachingGoal(knowledge_point_ids=["kp"], max_rounds=5, created_on=TODAY, deadline=TODAY)
    )
    session.flush()
    task = Task(
        type=TaskType.ESCALATION, student_id="A", knowledge_point_id="kp", round=2, created_on=TODAY
    )
    session.add(task)
    session.flush()
    with pytest.raises(ValueError, match="round limit"):
        escalate(session, task, TODAY)


def test_replace_cannot_overwrite_evidence(session):
    row = evidence(session, Dimension.CORRECTNESS)
    session.commit()
    with pytest.raises(IntegrityError, match="append-only"):
        session.execute(
            text(
                "INSERT OR REPLACE INTO evidence SELECT id, student_id, knowledge_point_id, kind, "
                "dimension, task_id, item_id, student_answer, correct_answer, predictions, "
                "is_correct, matched_misconception_id, judged_by, 'replaced', teaching_date "
                "FROM evidence"
            )
        )
        session.commit()
    session.rollback()
    assert session.get(Evidence, row.id).reason == ""


def test_unknown_results_and_wrong_predictions_do_not_certify(session):
    evidence(session, Dimension.CORRECTNESS)
    evidence(session, Dimension.DISTRACTOR, correct=None)
    evidence(session, Dimension.NEAR_TRANSFER)
    assert state(session) is None


def test_repeat_assessment_does_not_demote_integrated(session):
    initial(session)
    evidence(session, Dimension.CROSS_CHAPTER, day=20)
    evidence(session, Dimension.CORRECTNESS, day=21)
    evidence(session, Dimension.RETENTION, day=22)
    assert state(session) == MasteryState.INTEGRATED


def test_escalation_gates_and_unmodeled_error(session):
    task = Task(
        type=TaskType.ESCALATION, student_id="A", knowledge_point_id="kp", created_on=TODAY, round=1
    )
    session.add(task)
    session.flush()
    with pytest.raises(ValueError, match="round limit"):
        escalate(session, task, TODAY)
    unknown = evidence(session, correct=None, kind=EvidenceKind.DIAGNOSIS)
    session.add(
        Misconception(
            id="unknown",
            knowledge_point_id="kp",
            family="unmodeled",
            description="unresolved",
            source=ContentSource.UNMODELED,
        )
    )
    session.flush()
    task.misconception_ids = ["unknown"]
    task.evidence_ids = [unknown.id]
    escalate(session, task, TODAY)
    assert state(session) == MasteryState.ESCALATED


@pytest.mark.parametrize(
    "operation", ["UPDATE evidence SET reason='changed'", "DELETE FROM evidence"]
)
def test_ledger_immutable_even_through_sql(session, operation):
    row = evidence(session, Dimension.CORRECTNESS)
    session.commit()
    with pytest.raises(IntegrityError, match="append-only"):
        session.execute(text(operation))
        session.commit()
    session.rollback()
    assert session.get(Evidence, row.id).reason == ""


def test_append_does_not_commit_callers_transaction(session):
    evidence(session, correct=False, kind=EvidenceKind.HOMEWORK)
    assert state(session) == MasteryState.NEEDS_INTERVENTION
    session.rollback()
    assert session.exec(select(Evidence)).all() == []
    assert state(session) is None


def test_query_models_and_latest_results(session):
    initial(session)
    evidence(session, Dimension.CORRECTNESS, correct=False, day=1)
    result = queries.verification(session, "A", "kp")
    assert len(result) == 6
    assert next(row for row in result if row.dimension == Dimension.CORRECTNESS).passed is False
    assert queries.class_overview(session).goal_met == 1
    assert queries.heatmap(session)[0].mastered_ratio == 1
    assert len(queries.timeline(session, "A")) == 4


def test_timeline_reports_only_started_real_backtrack(session):
    session.add(KnowledgePoint(id="pre", name="composition", lecture=1, section=1))
    session.flush()
    for task_id, student, status, reason in (
        (2, "A", TaskStatus.ACTIVE, "先修回溯：先独立检查"),
        (3, "A", TaskStatus.PENDING, "先修回溯：先独立检查"),
        (4, "A", TaskStatus.DONE, "普通独立验证"),
        (5, "B", TaskStatus.DONE, "先修回溯：先独立检查"),
    ):
        session.add(
            Task(
                id=task_id,
                type=TaskType.VERIFICATION,
                student_id=student,
                knowledge_point_id="pre",
                created_on=TODAY,
                status=status,
                reason=reason,
            )
        )
        session.add(PlanningTaskContext(task_id=task_id, root_knowledge_point_id="kp"))
    session.add(
        Task(
            id=6,
            type=TaskType.PRECHECK,
            student_id="A",
            knowledge_point_id="pre",
            created_on=TODAY,
            status=TaskStatus.DONE,
            reason="先修回溯：先独立检查",
        )
    )
    session.add(PlanningTaskContext(task_id=6, root_knowledge_point_id="kp"))
    session.add(
        Task(
            id=7,
            type=TaskType.VERIFICATION,
            student_id="A",
            knowledge_point_id="pre",
            created_on=TODAY,
            status=TaskStatus.DONE,
            reason="先修回溯：先独立检查",
        )
    )
    session.add(PlanningTaskContext(task_id=7, root_knowledge_point_id="pre"))
    session.flush()

    entries = queries.timeline(session, "A")
    backtracks = [entry for entry in entries if entry.kind == TimelineKind.BACKTRACK]
    assert [(entry.task_id, entry.knowledge_point_id) for entry in backtracks] == [(2, "pre")]
    assert "先修回溯" in backtracks[0].detail
    assert "任务创建日期" in backtracks[0].detail

    answer_date = TODAY + timedelta(days=3)
    session.add(
        Evidence(
            student_id="A",
            knowledge_point_id="pre",
            task_id=2,
            kind=EvidenceKind.VERIFICATION,
            dimension=Dimension.CORRECTNESS,
            is_correct=True,
            judged_by=JudgedBy.TOOL,
            teaching_date=answer_date,
        )
    )
    session.flush()
    related = [entry for entry in queries.timeline(session, "A") if entry.task_id == 2]
    assert [entry.kind for entry in related] == [TimelineKind.BACKTRACK, TimelineKind.VERIFICATION]
    assert related[0].teaching_date == answer_date
    assert "首条作答日期" in related[0].detail


@pytest.mark.anyio
async def test_read_api_uses_application_engine(session, tmp_path):
    initial(session)
    session.commit()
    app = create_app(engine=session.bind, checkpoint_path=str(tmp_path / "checkpoint.db"))
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        for path in (
            "/students",
            "/class/overview",
            "/class/heatmap",
            "/students/A/mastery",
            "/students/A/timeline",
            "/students/A/verification?knowledge_point_id=kp",
            "/evidence/1",
        ):
            response = await client.get("/api" + path)
            assert response.status_code == 200, response.text
        assert (await client.get("/api/evidence/9999")).status_code == 404
