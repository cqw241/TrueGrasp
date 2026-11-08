"""T08 acceptance: real discovery, bounded model decisions and durable task execution."""

import asyncio
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.agents.planner import InvalidPlanError, PlanningPolicy, create_planner
from app.agents.planner.models import Decision, PlanningDecisions
from app.agents.planner.queries import common_issues
from app.config import Settings
from app.contracts.agents import Judgement, PlannerInput, TutorTurn
from app.contracts.api import MessageCreate
from app.contracts.entities import (
    Event,
    Evidence,
    Hypothesis,
    Item,
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
    HypothesisStatus,
    ItemPurpose,
    ItemSource,
    JudgedBy,
    MasteryState,
    TaskStatus,
    TaskType,
    TutorOutcome,
)
from app.contracts.events import (
    AnswerSubmitted,
    HomeworkImported,
    LectureEntered,
    TaskDue,
    make_event,
)
from app.contracts.values import ItemSpec
from app.db import init_db, make_engine
from app.ingest import HomeworkAnalysis, HomeworkImporter
from app.knowledge.graph import NxKnowledgeGraph
from app.knowledge.ingest import ingest
from app.ledger.service import append_evidence
from app.main import create_app
from app.orchestration import EventBus, Orchestrator
from app.orchestration.storage import PlanningTaskContext
from app.verify.tools import SympyMathTools

TODAY = date(2026, 9, 8)


@pytest.fixture
def anyio_backend():
    return "asyncio"


class PlanningModel:
    def __init__(self, mutate=None):
        self.contexts = []
        self.mutate = mutate

    async def generate(self, messages, output, tools=None):
        assert output is PlanningDecisions
        context = json.loads(messages[-1].content)
        self.contexts.append(context)
        decisions = [
            Decision(
                candidate_id=c["id"],
                include=True,
                reason="按证据分层拆解复合函数，课上核对最内层变化率，再用新题验证",
                knowledge_point_id=c["task"]["knowledge_point_id"],
                due_date=c["task"]["due_date"],
            )
            for c in context["candidates"]
        ]
        if self.mutate:
            self.mutate(decisions, context)
        return PlanningDecisions(decisions=decisions, summary="根据实际信号安排教学")


@pytest.fixture
def classroom(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'class.db'}")
    init_db(engine)
    with Session(engine) as session:
        session.add_all([Student(id=s, name=s) for s in "ABC"])
        session.add_all(
            [
                KnowledgePoint(id="pre", name="函数复合", lecture=1, section=1),
                KnowledgePoint(
                    id="kp", name="链式法则", lecture=2, section=1, prerequisite_ids=["pre"]
                ),
                KnowledgePoint(
                    id="future", name="多元复合", lecture=9, section=1, prerequisite_ids=["kp"]
                ),
                KnowledgePoint(id="other", name="其他知识点", lecture=3, section=1),
            ]
        )
        session.flush()
        session.add_all(
            [
                Misconception(
                    id="miss",
                    knowledge_point_id="kp",
                    family="chain",
                    description="漏乘内层",
                    recurs_in=["future"],
                ),
                Misconception(
                    id="formula", knowledge_point_id="kp", family="chain", description="公式代换"
                ),
                Misconception(
                    id="unknown",
                    knowledge_point_id="kp",
                    family="unknown",
                    description="待审核",
                    source=ContentSource.UNMODELED,
                ),
            ]
        )
        session.add(
            TeachingGoal(
                knowledge_point_ids=["kp"],
                deadline=TODAY + timedelta(days=3),
                created_on=TODAY,
                max_rounds=2,
            )
        )
        session.commit()
    yield engine
    engine.dispose()


def event_id(engine, payload, when=TODAY):
    with Session(engine) as session:
        event = make_event(payload, when)
        session.add(event)
        session.commit()
        return event.id


def evidence(
    engine,
    *,
    student="A",
    kp="kp",
    task=None,
    correct=False,
    cause=None,
    kind=EvidenceKind.HOMEWORK,
    dimension=None,
    when=TODAY,
):
    with Session(engine) as session:
        row = append_evidence(
            session,
            Evidence(
                student_id=student,
                knowledge_point_id=kp,
                task_id=task,
                kind=kind,
                dimension=dimension,
                is_correct=correct,
                matched_misconception_id=cause,
                judged_by=JudgedBy.TOOL,
                teaching_date=when,
            ),
        )
        session.commit()
        return row.id


def task(
    engine,
    *,
    student="A",
    kp="kp",
    type=TaskType.TUTORING,
    round=1,
    status=TaskStatus.DONE,
    basis=(),
    causes=(),
):
    with Session(engine) as session:
        row = Task(
            student_id=student,
            knowledge_point_id=kp,
            type=type,
            round=round,
            status=status,
            evidence_ids=list(basis),
            misconception_ids=list(causes),
            created_on=TODAY,
        )
        session.add(row)
        session.commit()
        return row.id


def planner(engine, model=None, policy=None):
    return create_planner(
        engine=engine,
        llm=model or PlanningModel(),
        knowledge=NxKnowledgeGraph.from_engine(engine),
        policy=policy,
    )


async def plan(engine, event, model=None):
    return await planner(engine, model).plan(PlannerInput(event_id=event, teaching_date=TODAY))


@pytest.mark.anyio
async def test_real_demo_import_plans_twelve_individuals_and_eight_person_issue(tmp_path):
    sys.path.insert(0, str(Path(__file__).parents[2]))
    from demo.dataset import CHAIN, load_dataset

    dataset = load_dataset()
    roster = {s.id: s for s in dataset.scenario.students}

    class Analysis:
        async def generate(self, messages, output, tools=None):
            row = json.loads(messages[-1].content)["homework"]
            return HomeworkAnalysis(
                knowledge_point_id=CHAIN,
                is_correct=False,
                misconception_id=roster[row["student_id"]].homework_cause,
                reason="受控初步线索，不是确诊",
            )

    engine = make_engine(f"sqlite:///{tmp_path / 'demo.db'}")
    init_db(engine)
    try:
        with Session(engine) as session:
            ingest(session, dataset.content)
            session.add(
                TeachingGoal(
                    knowledge_point_ids=[CHAIN],
                    deadline=TODAY + timedelta(days=3),
                    created_on=TODAY,
                )
            )
            session.commit()
        imported = await HomeworkImporter(engine, Analysis()).import_csv(
            (Path(__file__).parents[2] / "demo/data/homework_ch2.csv").read_bytes(), TODAY
        )
        model = PlanningModel()
        agent = planner(engine, model)
        async with Orchestrator.open(
            engine,
            str(tmp_path / "checkpoints.db"),
            EventBus(),
            planner=agent,
            pending_poll_interval=None,
        ) as runtime:
            await runtime.handle_event(imported.event_id, dispatch=False)
            await runtime.wait_idle()
            await runtime.handle_event(imported.event_id, dispatch=False)
        facts = model.contexts[0]["facts"]
        assert facts["homework_students"] == 40 and facts["incorrect_students"] == 12
        assert sorted(g["students"] for g in facts["misconception_groups"]) == [3, 8]
        with Session(engine) as session:
            rows = session.exec(select(Task)).all()
            assert sum(t.type == TaskType.TUTORING for t in rows) == 12
            assert sum(t.type == TaskType.VERIFICATION for t in rows) == 28
            issues = common_issues(session)
            eight = next(i for i in issues if len(i.student_ids) == 8)
            assert "初步线索" in eight.suggestion
            assert len(issues) == 2
            assert all(t.misconception_ids == [] for t in rows)
            assert session.exec(select(Hypothesis)).all() == []
            assert session.get(Event, imported.event_id).processed
        app = create_app(
            engine=engine,
            checkpoint_path=str(tmp_path / "api-checkpoint.db"),
            orchestration_dependencies={"planner": agent},
        )
        with TestClient(app) as client:
            response = client.get("/api/class/issues")
            assert response.status_code == 200
            assert sorted(len(i["student_ids"]) for i in response.json()) == [3, 8]
    finally:
        engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("violation", ["missing", "duplicate", "unknown_point", "early", "skip"])
async def test_invalid_model_plan_leaves_event_and_tasks_untouched(classroom, tmp_path, violation):
    eid = evidence(classroom)
    event = event_id(classroom, HomeworkImported(knowledge_point_ids=["kp"], evidence_ids=[eid]))

    def corrupt(decisions, _):
        if violation == "missing":
            decisions.clear()
        elif violation == "duplicate":
            decisions.append(decisions[0])
        elif violation == "unknown_point":
            decisions[0].knowledge_point_id = "other"
        elif violation == "early":
            decisions[0].due_date = TODAY - timedelta(days=1)
        else:
            decisions[0].include = False

    async with Orchestrator.open(
        classroom,
        str(tmp_path / "invalid.db"),
        EventBus(),
        planner=planner(classroom, PlanningModel(corrupt)),
        pending_poll_interval=None,
    ) as runtime:
        with pytest.raises(InvalidPlanError):
            await runtime.handle_event(event, dispatch=False)
    with Session(classroom) as session:
        assert session.exec(select(Task)).all() == []
        assert not session.get(Event, event).processed


@pytest.mark.anyio
async def test_concurrent_import_events_create_only_one_individual_task(classroom, tmp_path):
    eid = evidence(classroom)
    payload = HomeworkImported(knowledge_point_ids=["kp"], evidence_ids=[eid])
    events = [event_id(classroom, payload) for _ in range(2)]
    async with Orchestrator.open(
        classroom,
        str(tmp_path / "concurrent.db"),
        EventBus(),
        planner=planner(classroom),
        pending_poll_interval=None,
    ) as runtime:
        await asyncio.gather(*(runtime.handle_event(e, dispatch=False) for e in events))
    with Session(classroom) as session:
        assert len(session.exec(select(Task)).all()) == 1
        assert all(session.get(Event, e).processed for e in events)


@pytest.mark.anyio
async def test_completed_round_schedules_retest_and_failure_limit_escalates(classroom, tmp_path):
    first = task(classroom)
    ids = [
        evidence(classroom, task=first, kind=EvidenceKind.VERIFICATION, dimension=dim, correct=True)
        for dim in (Dimension.CORRECTNESS, Dimension.DISTRACTOR, Dimension.NEAR_TRANSFER)
    ]
    event = event_id(classroom, AnswerSubmitted(task_id=first, evidence_id=ids[-1]))
    result = await plan(classroom, event)
    assert [t.type for t in result.tasks] == [TaskType.RETEST]
    assert result.tasks[0].due_date == TODAY + timedelta(days=3)

    # The second completed intervention fails even when its final answer is correct.
    second = task(classroom, student="B", round=2)
    evidence(
        classroom,
        student="B",
        task=second,
        kind=EvidenceKind.VERIFICATION,
        dimension=Dimension.DISTRACTOR,
        correct=False,
    )
    last = evidence(
        classroom,
        student="B",
        task=second,
        kind=EvidenceKind.VERIFICATION,
        dimension=Dimension.NEAR_TRANSFER,
        correct=True,
    )
    event = event_id(classroom, AnswerSubmitted(task_id=second, evidence_id=last))
    async with Orchestrator.open(
        classroom,
        str(tmp_path / "escalate.db"),
        EventBus(),
        planner=planner(classroom),
        pending_poll_interval=None,
    ) as runtime:
        await runtime.handle_event(event, dispatch=False)
    with Session(classroom) as session:
        escalated = session.exec(select(Task).where(Task.type == TaskType.ESCALATION)).one()
        state = session.get(Mastery, ("B", "kp"))
        assert escalated.round == 2 and last in escalated.evidence_ids
        assert state.state == MasteryState.ESCALATED
        assert set(escalated.evidence_ids) <= set(state.evidence_ids)


@pytest.mark.anyio
async def test_mid_round_does_not_replan_and_missing_goal_does_not_invent_limit(classroom):
    active = task(classroom, status=TaskStatus.ACTIVE, round=2)
    eid = evidence(
        classroom, task=active, kind=EvidenceKind.VERIFICATION, dimension=Dimension.DISTRACTOR
    )
    event = event_id(classroom, AnswerSubmitted(task_id=active, evidence_id=eid))
    assert not (await plan(classroom, event)).tasks
    with Session(classroom) as session:
        session.get(Task, active).status = TaskStatus.DONE
        session.delete(session.exec(select(TeachingGoal)).one())
        session.commit()
    assert not (await plan(classroom, event)).tasks


@pytest.mark.anyio
async def test_backtrack_keeps_original_goal_and_round(classroom, tmp_path):
    previous = task(classroom, round=1)
    eid = evidence(
        classroom, task=previous, kind=EvidenceKind.VERIFICATION, dimension=Dimension.DISTRACTOR
    )
    event = event_id(classroom, AnswerSubmitted(task_id=previous, evidence_id=eid))

    def backtrack(decisions, context):
        assert "pre" in context["candidates"][0]["allowed_backtrack_ids"]
        decisions[0].knowledge_point_id = "pre"

    async with Orchestrator.open(
        classroom,
        str(tmp_path / "backtrack.db"),
        EventBus(),
        planner=planner(classroom, PlanningModel(backtrack)),
        pending_poll_interval=None,
    ) as runtime:
        await runtime.handle_event(event, dispatch=False)
    with Session(classroom) as session:
        back = session.exec(select(Task).where(Task.knowledge_point_id == "pre")).one()
        assert back.type == TaskType.VERIFICATION
        assert back.round == 2
        assert session.get(PlanningTaskContext, back.id).root_knowledge_point_id == "kp"
        back.status = TaskStatus.DONE
        session.commit()
        back_id = back.id
    failed = evidence(
        classroom,
        kp="pre",
        task=back_id,
        kind=EvidenceKind.VERIFICATION,
        dimension=Dimension.CORRECTNESS,
    )
    event = event_id(classroom, AnswerSubmitted(task_id=back_id, evidence_id=failed))
    result = await plan(classroom, event)
    assert result.tasks[0].type == TaskType.TUTORING
    assert result.tasks[0].knowledge_point_id == "pre" and result.tasks[0].round == 2
    assert failed in result.tasks[0].evidence_ids
    assert result.contexts[0].root_knowledge_point_id == "kp"
    # Only the actual prerequisite intervention consumes the round; the entry check does not.
    intervention = task(classroom, kp="pre", round=2, basis=[eid, failed])
    with Session(classroom) as session:
        session.add(PlanningTaskContext(task_id=intervention, root_knowledge_point_id="kp"))
        session.commit()
    failed_again = evidence(
        classroom,
        kp="pre",
        task=intervention,
        kind=EvidenceKind.VERIFICATION,
        dimension=Dimension.DISTRACTOR,
    )
    event = event_id(classroom, AnswerSubmitted(task_id=intervention, evidence_id=failed_again))
    result = await plan(classroom, event)
    assert result.tasks[0].type == TaskType.ESCALATION
    assert result.tasks[0].knowledge_point_id == "kp" and result.tasks[0].round == 2


@pytest.mark.anyio
async def test_other_point_active_task_does_not_drop_new_homework(classroom, tmp_path):
    active = task(classroom, kp="other", status=TaskStatus.ACTIVE)
    eid = evidence(classroom)
    imported = event_id(classroom, HomeworkImported(knowledge_point_ids=["kp"], evidence_ids=[eid]))
    async with Orchestrator.open(
        classroom,
        str(tmp_path / "other-active.db"),
        EventBus(),
        planner=planner(classroom),
        pending_poll_interval=None,
    ) as runtime:
        await runtime.handle_event(imported, dispatch=False)
    with Session(classroom) as session:
        new = session.exec(select(Task).where(Task.knowledge_point_id == "kp")).all()
        assert len(new) == 1 and new[0].type == TaskType.TUTORING
        assert eid in new[0].evidence_ids
        assert session.get(Task, active).status == TaskStatus.ACTIVE
        assert session.get(Event, imported).processed


@pytest.mark.anyio
@pytest.mark.parametrize("correct", [False, True])
async def test_backtrack_checks_before_tutoring_and_supplies_matching_error(
    classroom, tmp_path, correct
):
    original = task(classroom)
    error_id = evidence(
        classroom, task=original, kind=EvidenceKind.VERIFICATION, dimension=Dimension.DISTRACTOR
    )
    event = event_id(classroom, AnswerSubmitted(task_id=original, evidence_id=error_id))

    def choose_backtrack(decisions, context):
        for decision, candidate in zip(decisions, context["candidates"], strict=True):
            if candidate["allowed_backtrack_ids"]:
                decision.knowledge_point_id = "pre"

    class EntryVerifier:
        async def compose(self, inp):
            assert inp.knowledge_point_id == "pre"
            assert inp.dimensions == [Dimension.CORRECTNESS]
            assert inp.confirmed_misconceptions == []
            spec = ItemSpec(operation="expression", expr="10")
            return [
                Item(
                    id="entry-check",
                    knowledge_point_id="pre",
                    purpose=ItemPurpose.VERIFICATION,
                    dimension=Dimension.CORRECTNESS,
                    stem="计算 10",
                    answer="10",
                    spec=spec,
                    fingerprint=SympyMathTools().fingerprint(spec),
                    source=ItemSource.GENERATED,
                )
            ]

        async def judge(self, item, answer):
            return Judgement(is_correct=correct, judged_by=JudgedBy.TOOL, reason="先修检查")

    class TutorEntry:
        def __init__(self):
            self.called = False

        async def turn(self, inp, publish):
            # T09 requires a wrong answer belonging to this student and target point.
            with Session(classroom) as session:
                error = session.get(Evidence, inp.task.error_evidence_id)
                assert error is not None and error.is_correct is False
                assert error.student_id == inp.task.student_id
                assert error.knowledge_point_id == inp.task.knowledge_point_id
                assert error.knowledge_point_id == ("kp" if correct else "pre")
            assert inp.task.round == 2
            self.called = True
            return TutorTurn(outcome=TutorOutcome.AWAIT_STUDENT, message="进入对应知识点诊断")

    tutor = TutorEntry()
    checkpoint = str(tmp_path / "backtrack-chain.db")
    async with Orchestrator.open(
        classroom,
        checkpoint,
        EventBus(),
        planner=planner(classroom, PlanningModel(choose_backtrack)),
        pending_poll_interval=None,
    ) as runtime:
        await runtime.handle_event(event, dispatch=False)
    with Session(classroom) as session:
        entry = session.exec(select(Task).where(Task.knowledge_point_id == "pre")).one()
        entry_id = entry.id
    # Reopen to prove the entry-check behavior is durable rather than an in-memory override.
    async with Orchestrator.open(
        classroom,
        checkpoint,
        EventBus(),
        planner=planner(classroom),
        tutor=tutor,
        verifier=EntryVerifier(),
        knowledge=NxKnowledgeGraph.from_engine(classroom),
        teaching_date=lambda: TODAY,
        pending_poll_interval=None,
    ) as runtime:
        await runtime.start_task(entry_id)
        assert not tutor.called
        await runtime.submit_message(
            entry_id,
            MessageCreate(content="10" if correct else "9", answering_item_id="entry-check"),
        )
        await runtime.wait_idle()
        assert tutor.called
        with Session(classroom) as session:
            assert session.exec(select(Task).where(Task.type == TaskType.ESCALATION)).all() == []


@pytest.mark.anyio
async def test_same_original_goal_still_blocks_conflicting_work(classroom):
    active = task(classroom, kp="pre", status=TaskStatus.ACTIVE)
    with Session(classroom) as session:
        session.add(PlanningTaskContext(task_id=active, root_knowledge_point_id="kp"))
        session.commit()
    eid = evidence(classroom)
    event = event_id(classroom, HomeworkImported(knowledge_point_ids=["kp"], evidence_ids=[eid]))
    assert not (await plan(classroom, event)).tasks


@pytest.mark.anyio
async def test_precheck_rules_and_durable_cross_chapter_context(classroom, tmp_path):
    # A: consolidated but confirmed cause; B: missing mastery; C: consolidated without cause.
    basis = evidence(classroom, cause="miss")
    diagnostic = task(classroom, basis=[basis])
    with Session(classroom) as session:
        session.add(
            Hypothesis(
                student_id="A",
                task_id=diagnostic,
                error_evidence_id=basis,
                misconception_id="miss",
                status=HypothesisStatus.SUPPORTED,
                evidence_ids=[basis],
            )
        )
        for student in "AC":
            row = session.get(Mastery, (student, "kp"))
            if row:
                row.state = MasteryState.CONSOLIDATED
            else:
                session.add(
                    Mastery(
                        student_id=student,
                        knowledge_point_id="kp",
                        state=MasteryState.CONSOLIDATED,
                        updated_on=TODAY,
                    )
                )
        session.commit()
    event = event_id(classroom, LectureEntered(lecture=9))
    async with Orchestrator.open(
        classroom,
        str(tmp_path / "precheck.db"),
        EventBus(),
        planner=planner(classroom),
        pending_poll_interval=None,
    ) as runtime:
        await runtime.handle_event(event, dispatch=False)
    with Session(classroom) as session:
        prechecks = session.exec(select(Task).where(Task.type == TaskType.PRECHECK)).all()
        assert {t.student_id for t in prechecks} == {"A", "B"}
        assert all(
            session.get(PlanningTaskContext, t.id).context_knowledge_point_id == "future"
            for t in prechecks
        )

    class CaptureVerifier:
        async def compose(self, inp):
            assert inp.dimensions == [Dimension.CROSS_CHAPTER]
            assert inp.context_knowledge_point_id == "future"
            raise RuntimeError("context captured")

    # Reopening the runtime proves context is in business storage, not planner memory.
    async with Orchestrator.open(
        classroom,
        str(tmp_path / "context.db"),
        EventBus(),
        verifier=CaptureVerifier(),
        pending_poll_interval=None,
    ) as runtime:
        with pytest.raises(RuntimeError, match="context captured"):
            await runtime.start_task(prechecks[0].id)


@pytest.mark.anyio
async def test_due_task_is_reused_and_unmodeled_evidence_can_escalate(classroom):
    pending = task(classroom, type=TaskType.RETEST, status=TaskStatus.PENDING)
    due = event_id(classroom, TaskDue(task_id=pending))
    assert not (await plan(classroom, due)).tasks
    previous = task(classroom, student="B")
    unknown = evidence(
        classroom,
        student="B",
        task=previous,
        correct=None,
        cause="unknown",
        kind=EvidenceKind.VERIFICATION,
        dimension=Dimension.EXPLANATION,
    )
    event = event_id(classroom, AnswerSubmitted(task_id=previous, evidence_id=unknown))
    result = await plan(classroom, event)
    assert result.tasks[0].type == TaskType.ESCALATION
    assert result.tasks[0].misconception_ids == ["unknown"]


@pytest.mark.anyio
async def test_diagnosis_revises_initial_cluster_and_cancels_obsolete_suggestion(
    classroom, tmp_path
):
    ids = [evidence(classroom, student=s, cause="miss") for s in "ABC"]
    imported = event_id(classroom, HomeworkImported(knowledge_point_ids=["kp"], evidence_ids=ids))
    async with Orchestrator.open(
        classroom,
        str(tmp_path / "revision.db"),
        EventBus(),
        planner=planner(classroom),
        pending_poll_interval=None,
    ) as runtime:
        await runtime.handle_event(imported, dispatch=False)
        with Session(classroom) as session:
            review = session.exec(select(Task).where(Task.type == TaskType.CLASS_REVIEW)).one()
            review_id = review.id
            individual = session.exec(
                select(Task).where(Task.student_id == "A", Task.type == TaskType.TUTORING)
            ).one()
            individual.status = TaskStatus.ACTIVE
            individual_id = individual.id
            session.commit()
        probe = evidence(
            classroom, task=individual_id, kind=EvidenceKind.DIAGNOSIS, cause="formula"
        )
        with Session(classroom) as session:
            session.add(
                Hypothesis(
                    student_id="A",
                    task_id=individual_id,
                    error_evidence_id=ids[0],
                    misconception_id="formula",
                    status=HypothesisStatus.SUPPORTED,
                    evidence_ids=[probe],
                )
            )
            session.commit()
        answered = event_id(classroom, AnswerSubmitted(task_id=individual_id, evidence_id=probe))
        await runtime.handle_event(answered, dispatch=False)
    with Session(classroom) as session:
        assert session.get(Task, review_id).status == TaskStatus.CANCELLED
        assert common_issues(session) == []
        assert session.get(Task, individual_id).status == TaskStatus.ACTIVE


@pytest.mark.anyio
@pytest.mark.parametrize("factor", ["goal_gap", "prerequisites", "deadline", "relapse"])
async def test_four_priority_factors_come_from_real_goals_graph_and_ledger(classroom, factor):
    with Session(classroom) as session:
        goal = session.exec(select(TeachingGoal)).one()
        goal.target_ratio = 0.9 if factor == "goal_gap" else 0.5
        if factor != "prerequisites":
            session.get(KnowledgePoint, "kp").prerequisite_ids = []
        session.add(
            TeachingGoal(
                knowledge_point_ids=["other"],
                target_ratio=0.5,
                deadline=TODAY + timedelta(days=6 if factor == "deadline" else 3),
                created_on=TODAY,
            )
        )
        session.commit()
    first = evidence(classroom, student="A")
    second = evidence(classroom, student="B", kp="other")
    if factor == "relapse":
        with Session(classroom) as session:
            session.get(Mastery, ("B", "other")).state = MasteryState.RELAPSED
            session.commit()
    imported = event_id(
        classroom,
        HomeworkImported(knowledge_point_ids=["kp", "other"], evidence_ids=[first, second]),
    )
    model = PlanningModel()
    result = await plan(classroom, imported, model)
    assert result.tasks[0].student_id == ("B" if factor == "relapse" else "A")
    candidates = model.contexts[0]["candidates"]
    if factor == "goal_gap":
        assert candidates[0]["goal_gap"] > candidates[1]["goal_gap"]
    elif factor == "prerequisites":
        assert candidates[0]["prerequisite_blockers"] == 1
    elif factor == "deadline":
        assert candidates[0]["days_to_deadline"] == 3
    else:
        assert candidates[0]["relapse_risk"] is True


@pytest.mark.anyio
async def test_real_round_finishes_before_retest_is_planned(classroom, tmp_path):
    class Tutor:
        async def turn(self, inp, publish):
            return TutorTurn(
                outcome=TutorOutcome.REQUEST_VERIFICATION,
                message="开始独立验证",
                confirmed_misconception_ids=["miss"],
            )

    class Verifier:
        async def compose(self, inp):
            return [
                Item(
                    id=f"generated-{index}",
                    knowledge_point_id=inp.knowledge_point_id,
                    purpose=ItemPurpose.VERIFICATION,
                    dimension=dimension,
                    stem=f"计算 {index}",
                    answer=str(index),
                    source=ItemSource.GENERATED,
                    spec=(spec := ItemSpec(operation="expression", expr=str(index))),
                    fingerprint=SympyMathTools().fingerprint(spec),
                )
                for index, dimension in enumerate(inp.dimensions, start=1)
            ]

        async def judge(self, item, answer):
            return Judgement(is_correct=True, judged_by=JudgedBy.TOOL, reason="正确")

    basis = evidence(classroom)
    tid = task(classroom, status=TaskStatus.PENDING, basis=[basis])
    async with Orchestrator.open(
        classroom,
        str(tmp_path / "round.db"),
        EventBus(),
        planner=planner(classroom),
        tutor=Tutor(),
        verifier=Verifier(),
        knowledge=NxKnowledgeGraph.from_engine(classroom),
        teaching_date=lambda: TODAY,
        pending_poll_interval=0.01,
    ) as runtime:
        await runtime.start_task(tid)
        for index in range(1, 4):
            await runtime.submit_message(
                tid, MessageCreate(content=str(index), answering_item_id=f"generated-{index}")
            )
            await runtime.wait_idle()
            with Session(classroom) as session:
                followups = session.exec(select(Task).where(Task.type == TaskType.RETEST)).all()
                assert len(followups) == (1 if index == 3 else 0)
        with Session(classroom) as session:
            assert session.get(Task, tid).status == TaskStatus.DONE
            assert session.get(Mastery, ("A", "kp")).state == MasteryState.INITIAL


@pytest.mark.anyio
async def test_non_direct_recurrence_rule_and_configurable_threshold(classroom):
    basis = evidence(classroom, cause="miss")
    diagnostic = task(classroom, basis=[basis])
    with Session(classroom) as session:
        session.get(KnowledgePoint, "future").prerequisite_ids = []
        session.add(
            Hypothesis(
                student_id="A",
                task_id=diagnostic,
                error_evidence_id=basis,
                misconception_id="miss",
                status=HypothesisStatus.SUPPORTED,
                evidence_ids=[basis],
            )
        )
        session.commit()
    entered = event_id(classroom, LectureEntered(lecture=9))
    result = await plan(classroom, entered)
    assert len(result.tasks) == 1 and result.tasks[0].student_id == "A"
    assert result.contexts[0].context_knowledge_point_id == "future"
    ids = [basis, evidence(classroom, student="B", cause="miss")]
    imported = event_id(classroom, HomeworkImported(knowledge_point_ids=["kp"], evidence_ids=ids))
    result = await planner(classroom, policy=PlanningPolicy(class_review_min_students=2)).plan(
        PlannerInput(event_id=imported, teaching_date=TODAY)
    )
    assert sum(t.type == TaskType.CLASS_REVIEW for t in result.tasks) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("disabled", [{"planner": None}, {"planner": None, "verifier": None}])
async def test_application_respects_explicit_disable_and_closes_shared_client(
    classroom, tmp_path, monkeypatch, disabled
):
    clients = []

    class Client:
        def __init__(self, settings):
            self.closed = False
            clients.append(self)

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr("app.main.get_settings", lambda: Settings(llm_api_key="test-only"))
    monkeypatch.setattr("app.main.OpenAICompatClient", Client)
    app = create_app(
        engine=classroom,
        checkpoint_path=str(tmp_path / "lifespan.db"),
        orchestration_dependencies=disabled,
    )
    async with app.router.lifespan_context(app):
        assert app.state.orchestrator.planner is None
        assert len(clients) == (0 if "verifier" in disabled else 1)
    assert all(c.closed for c in clients)


@pytest.mark.anyio
async def test_new_error_cancels_pending_verification_and_retest(classroom, tmp_path):
    obsolete = [
        task(classroom, type=kind, status=TaskStatus.PENDING)
        for kind in (TaskType.VERIFICATION, TaskType.RETEST)
    ]
    eid = evidence(classroom)
    imported = event_id(classroom, HomeworkImported(knowledge_point_ids=["kp"], evidence_ids=[eid]))
    async with Orchestrator.open(
        classroom,
        str(tmp_path / "obsolete.db"),
        EventBus(),
        planner=planner(classroom),
        pending_poll_interval=None,
    ) as runtime:
        await runtime.handle_event(imported, dispatch=False)
    with Session(classroom) as session:
        assert all(session.get(Task, tid).status == TaskStatus.CANCELLED for tid in obsolete)
        assert len(session.exec(select(Task).where(Task.type == TaskType.TUTORING)).all()) == 1


@pytest.mark.anyio
async def test_student_starts_task_while_model_is_planning(classroom, tmp_path):
    pending = task(classroom, type=TaskType.VERIFICATION, status=TaskStatus.PENDING)
    eid = evidence(classroom)
    imported = event_id(classroom, HomeworkImported(knowledge_point_ids=["kp"], evidence_ids=[eid]))

    def start_during_model_call(decisions, context):
        with Session(classroom) as session:
            session.get(Task, pending).status = TaskStatus.ACTIVE
            session.commit()

    async with Orchestrator.open(
        classroom,
        str(tmp_path / "start-race.db"),
        EventBus(),
        planner=planner(classroom, PlanningModel(start_during_model_call)),
        pending_poll_interval=None,
    ) as runtime:
        await runtime.handle_event(imported, dispatch=False)
    with Session(classroom) as session:
        assert session.get(Task, pending).status == TaskStatus.ACTIVE
        assert len(session.exec(select(Task)).all()) == 1
