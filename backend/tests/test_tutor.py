"""T09 acceptance with authored C2 content, real tools and deterministic models."""

import json
from datetime import date
from pathlib import Path

import pytest
from sqlmodel import Session, select

from app.agents.tutor import create_tutor
from app.agents.tutor.models import (
    AnswerReview,
    CandidateSelection,
    QuestionDraft,
    QuestionReview,
    TeachingState,
)
from app.agents.tutor.queries import diagnosis
from app.agents.tutor.storage import TutorSession
from app.contracts.agents import TutorTask, TutorTurnInput
from app.contracts.entities import Evidence, Hypothesis, Item, Mastery, Misconception, Student, Task
from app.contracts.enums import (
    ContentSource,
    EvidenceKind,
    HypothesisStatus,
    ItemPurpose,
    ItemSource,
    JudgedBy,
    MasteryState,
    TutorOutcome,
)
from app.contracts.values import SLIP, ItemSpec, Prediction
from app.db import init_db, make_engine
from app.knowledge.content import load
from app.knowledge.graph import NxKnowledgeGraph
from app.knowledge.ingest import ingest
from app.llm.client import LLMOutputError
from app.orchestration.inputs import build_verifier_input
from app.orchestration.storage import ItemExposure

POINT = "calc.ch2.chain_rule_multi"
MISS = "calc.ch2.chain.miss_inner"
FORMULA = "calc.ch2.chain.formula_sub"
TODAY = date(2026, 11, 8)
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Publisher:
    def __init__(self):
        self.events = []

    async def publish(self, event):
        self.events.append(event)


class Model:
    def __init__(self):
        self.calls = []
        self.practice = 0

    async def generate(self, messages, output, tools=None):
        payload = json.loads(messages[-1].content)
        self.calls.append((output, payload))
        if output is CandidateSelection:
            return CandidateSelection(misconception_ids=[MISS, FORMULA], reason="同一错误两种解释")
        if output is QuestionReview:
            return QuestionReview(valid=True, reason="题干与数学载体一致")
        if output is QuestionDraft:
            if payload["active_ids"]:
                n = 4 + len(payload["used_fingerprints"]) if "used_fingerprints" in payload else 7
                return QuestionDraft(
                    stem=f"求 $sin({n}x)$ 的导数",
                    spec=ItemSpec(operation="derivative", expr=f"sin({n}*x)", variables=["x"]),
                    answer=f"{n}*cos({n}*x)",
                    predictions=[
                        Prediction(misconception_id=MISS, answer=f"{n}*cos({n}*x)"),
                        Prediction(misconception_id=FORMULA, answer=f"cos({n}*x)"),
                        Prediction(misconception_id=SLIP, answer=f"{n}*cos({n}*x)"),
                    ],
                )
            self.practice += 1
            n = self.practice + 2
            return QuestionDraft(
                stem=f"求 $sin^2({n}x)$ 的导数",
                spec=ItemSpec(operation="derivative", expr=f"sin({n}*x)**2", variables=["x"]),
                answer=f"{2 * n}*sin({n}*x)*cos({n}*x)",
            )
        if output is AnswerReview:
            return AnswerReview(is_correct=None, reason="无法判断")
        raise AssertionError(output)

    async def stream(self, messages):
        payload = json.loads(messages[-1].content)
        self.calls.append(("stream", payload))
        yield "请按每层依赖关系"
        yield "说明变化率，再完成练习。"


@pytest.fixture
def setup(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'tutor.db'}")
    init_db(engine)
    content = load(sorted((Path(__file__).parents[2] / "content/lectures").glob("*.yaml")))
    with Session(engine) as session:
        ingest(session, content)
        session.add(Student(id="A", name="测试学生"))
        session.flush()
        error = Evidence(
            student_id="A",
            knowledge_point_id=POINT,
            kind=EvidenceKind.HOMEWORK,
            item_id="calc.ch2.chain_rule_multi.demo_homework",
            student_answer="2*sin(3*x)*cos(3*x)",
            is_correct=False,
            judged_by=JudgedBy.TOOL,
            teaching_date=TODAY,
        )
        session.add(error)
        session.flush()
        task = Task(
            type="tutoring",
            student_id="A",
            knowledge_point_id=POINT,
            evidence_ids=[error.id],
            created_on=TODAY,
        )
        session.add(task)
        session.add(
            Mastery(
                student_id="A",
                knowledge_point_id=POINT,
                state=MasteryState.TUTORING,
                updated_on=TODAY,
            )
        )
        session.commit()
        task_id, error_id = task.id, error.id
    knowledge = NxKnowledgeGraph.from_engine(engine)
    task = TutorTask(
        task_id=task_id,
        student_id="A",
        knowledge_point_id=POINT,
        error_evidence_id=error_id,
        round=1,
        allowed_backtrack_ids=[p.id for p in knowledge.ancestors(POINT)],
    )
    yield engine, knowledge, task
    engine.dispose()


def input_for(task, text=None, item=None, history=None):
    return TutorTurnInput(
        task=task, history=history or [], student_message=text, answering_item_id=item
    )


@pytest.mark.parametrize(
    ("student", "answers", "confirmed", "strategy"),
    [
        ("A", ["3*cos(3*x)", "-2*cos(5*x)*sin(5*x)"], MISS, "layered_decomposition"),
        ("B", ["cos(3*x)"], FORMULA, "numeric_slope_conflict"),
        ("C", ["3*cos(3*x)", "-10*cos(5*x)*sin(5*x)"], SLIP, None),
    ],
)
async def test_abc_diagnosis(setup, student, answers, confirmed, strategy):
    engine, knowledge, task = setup
    model, publisher = Model(), Publisher()
    tutor = create_tutor(llm=model, knowledge=knowledge, engine=engine)
    opening = input_for(task)
    turn = await tutor.turn(opening, publisher)
    assert await tutor.turn(opening, publisher) == turn
    with Session(engine) as session:
        view = diagnosis(session, task.task_id)
        assert len(view.hypotheses) == 3
        assert len(view.probes) == 1
        assert view.probes[0].student_answer is None
        assert "sin" in view.probes[0].stem
        assert len(view.probes[0].predictions) == 3
    for index, answer in enumerate(answers):
        turn = await tutor.turn(input_for(task, answer, turn.awaiting_item_id), publisher)
        if student in ("A", "C") and index == 0:
            with Session(engine) as session:
                states = {
                    h.misconception_id: h.status for h in session.exec(select(Hypothesis)).all()
                }
                assert states == {
                    MISS: HypothesisStatus.SUPPORTED,
                    FORMULA: HypothesisStatus.REFUTED,
                    SLIP: HypothesisStatus.SUPPORTED,
                }
    if confirmed != SLIP:
        assert turn.outcome == TutorOutcome.AWAIT_STUDENT
        assert (
            next(p for kind, p in model.calls if kind == "stream")["strategies"][0]["id"]
            == strategy
        )
        with Session(engine) as session:
            item = session.get(Item, turn.awaiting_item_id)
        turn = await tutor.turn(input_for(task, item.answer, item.id), publisher)
    assert turn.outcome == TutorOutcome.REQUEST_VERIFICATION
    assert turn.confirmed_misconception_ids == [confirmed]
    with Session(engine) as session:
        rows = session.exec(select(Evidence).where(Evidence.kind == EvidenceKind.DIAGNOSIS)).all()
        assert len(rows) == len(answers)
        assert all(row.judged_by == JudgedBy.TOOL for row in rows)
        assert session.get(Mastery, ("A", POINT)).state == MasteryState.TUTORING
        from app.contracts.enums import Dimension

        whitelist = build_verifier_input(
            session,
            student_id="A",
            knowledge_point_id=POINT,
            dimensions=[Dimension.CORRECTNESS],
            confirmed_misconception_ids=[confirmed],
            shown_item_ids=[],
        )
        assert "history" not in whitelist.model_dump()
        assert len(whitelist.used_fingerprints) >= len(answers) + 1
    assert any(e.type == "hypothesis_updated" for e in publisher.events)
    assert any(e.type == "evidence_added" for e in publisher.events)


async def test_no_progress_requests_allowed_backtrack(setup):
    engine, knowledge, task = setup
    model, pub = Model(), Publisher()
    tutor = create_tutor(llm=model, knowledge=knowledge, engine=engine)
    turn = await tutor.turn(input_for(task), pub)
    turn = await tutor.turn(input_for(task, "cos(3*x)", turn.awaiting_item_id), pub)
    for _ in range(2):
        turn = await tutor.turn(input_for(task, "0", turn.awaiting_item_id), pub)
    assert turn.outcome == TutorOutcome.REQUEST_REPLAN
    assert task.allowed_backtrack_ids[0] in turn.replan_reason
    with Session(engine) as session:
        assert session.get(Mastery, ("A", POINT)).state == MasteryState.TUTORING


@pytest.mark.parametrize("fallback", [False, True])
async def test_shared_students_preserve_seed_items(setup, fallback):
    engine, knowledge, first_task = setup
    seeds = {item.id: item.model_dump() for item in knowledge.seed_items(POINT)}
    tasks = [first_task]
    with Session(engine) as session:
        for student_id in ("B", "C"):
            session.add(Student(id=student_id, name=student_id))
            session.flush()
            error = Evidence(
                student_id=student_id,
                knowledge_point_id=POINT,
                kind=EvidenceKind.HOMEWORK,
                item_id="calc.ch2.chain_rule_multi.demo_homework",
                student_answer="2*sin(3*x)*cos(3*x)",
                is_correct=False,
                judged_by=JudgedBy.TOOL,
                teaching_date=TODAY,
            )
            session.add(error)
            session.flush()
            stored = Task(
                type="tutoring",
                student_id=student_id,
                knowledge_point_id=POINT,
                evidence_ids=[error.id],
                created_on=TODAY,
            )
            session.add(stored)
            session.flush()
            tasks.append(
                TutorTask(
                    task_id=stored.id,
                    student_id=student_id,
                    knowledge_point_id=POINT,
                    error_evidence_id=error.id,
                    round=1,
                    allowed_backtrack_ids=first_task.allowed_backtrack_ids,
                )
            )
        session.commit()

    class SharedModel(Model):
        async def generate(self, messages, output, tools=None):
            if fallback and output is QuestionDraft:
                self.practice += 1
                raise LLMOutputError("exercise seed fallback")
            return await super().generate(messages, output, tools)

    model = SharedModel()
    tutor = create_tutor(llm=model, knowledge=knowledge, engine=engine)
    first_probe_ids = set()
    for task, answers, expected in zip(
        tasks,
        (
            ["3*cos(3*x)", "-2*cos(5*x)*sin(5*x)"],
            ["cos(3*x)"],
            ["3*cos(3*x)", "-10*cos(5*x)*sin(5*x)"],
        ),
        (MISS, FORMULA, SLIP),
        strict=True,
    ):
        publisher = Publisher()
        turn = await tutor.turn(input_for(task), publisher)
        first_probe_ids.add(turn.awaiting_item_id)
        for answer in answers:
            turn = await tutor.turn(input_for(task, answer, turn.awaiting_item_id), publisher)
        if expected != SLIP:
            with Session(engine) as session:
                practice = session.get(Item, turn.awaiting_item_id)
                assert practice.purpose == "practice"
                assert practice.dimension is None and not practice.predictions
                if fallback:
                    assert practice.source == "seed"
            turn = await tutor.turn(input_for(task, practice.answer, practice.id), publisher)
        assert turn.outcome == TutorOutcome.REQUEST_VERIFICATION
        assert turn.confirmed_misconception_ids == [expected]
        with Session(engine) as session:
            for seed_id, original in seeds.items():
                persisted = session.get(Item, seed_id)
                assert persisted is not None, f"seed removed: {seed_id}"
                assert persisted.model_dump() == original
        assert {item.id: item.model_dump() for item in knowledge.seed_items(POINT)} == seeds
    assert len(first_probe_ids) == 3
    if fallback:
        assert model.practice == 6


async def test_unmodeled_is_recorded_for_teacher(setup):
    engine, knowledge, task = setup
    with Session(engine) as session:
        original = session.get(Evidence, task.error_evidence_id)
        replacement = Evidence(
            student_id=original.student_id,
            knowledge_point_id=original.knowledge_point_id,
            kind=EvidenceKind.HOMEWORK,
            item_id=original.item_id,
            student_answer="12345",
            is_correct=False,
            judged_by=JudgedBy.TOOL,
            teaching_date=TODAY,
        )
        session.add(replacement)
        session.flush()
        task.error_evidence_id = replacement.id
        session.commit()

    class UnknownModel(Model):
        async def generate(self, messages, output, tools=None):
            if output is CandidateSelection:
                return CandidateSelection(
                    unmodeled_description="学生把指数当乘法",
                    unmodeled_family="未知结构",
                    reason="库内不匹配",
                )
            return await super().generate(messages, output, tools)

    tutor = create_tutor(llm=UnknownModel(), knowledge=knowledge, engine=engine)
    turn = await tutor.turn(input_for(task), Publisher())
    assert turn.outcome == TutorOutcome.AWAIT_STUDENT
    assert turn.awaiting_item_id
    with Session(engine) as session:
        stored = session.get(Task, task.task_id)
        assert stored.status != "cancelled"
    practice = None
    with Session(engine) as session:
        practice = session.get(Item, turn.awaiting_item_id)
    turn = await tutor.turn(input_for(task, practice.answer, practice.id), Publisher())
    assert turn.outcome == TutorOutcome.REQUEST_VERIFICATION
    assert turn.confirmed_misconception_ids == []
    with Session(engine) as session:
        unknown = session.exec(
            select(Misconception).where(Misconception.source == ContentSource.UNMODELED)
        ).one()
        stored = session.get(Task, task.task_id)
        assert unknown.id in stored.misconception_ids
        evidence = session.get(Evidence, stored.evidence_ids[-1])
        assert evidence.is_correct is None
        assert evidence.matched_misconception_id == unknown.id
        hypothesis = session.exec(
            select(Hypothesis).where(
                Hypothesis.task_id == task.task_id,
                Hypothesis.misconception_id == unknown.id,
            )
        ).one()
        assert evidence.id in hypothesis.evidence_ids
        assert hypothesis.status == HypothesisStatus.PENDING
        # No teaching goal is configured: undecidable, explicitly linked unknown
        # evidence must still authorize the downstream teacher escalation.
        from app.ledger.service import escalate

        escalation = Task(
            type="escalation",
            student_id=stored.student_id,
            knowledge_point_id=stored.knowledge_point_id,
            misconception_ids=[unknown.id],
            evidence_ids=[evidence.id],
            created_on=TODAY,
        )
        session.add(escalation)
        session.flush()
        escalate(session, escalation, TODAY)
        assert session.get(Mastery, (stored.student_id, stored.knowledge_point_id)).state == (
            MasteryState.ESCALATED
        )
        assert diagnosis(session, task.task_id).hypotheses[0].unmodeled


async def test_ordinary_message_does_not_consume_probe(setup):
    engine, knowledge, task = setup

    class ChattyModel(Model):
        async def generate(self, messages, output, tools=None):
            if output is AnswerReview:
                return AnswerReview(intent="concept_question", is_correct=None, reason="问概念")
            return await super().generate(messages, output, tools)

    tutor = create_tutor(llm=ChattyModel(), knowledge=knowledge, engine=engine)
    opening = await tutor.turn(input_for(task), Publisher())
    reply = await tutor.turn(
        input_for(task, "为什么这里要乘 3？", opening.awaiting_item_id), Publisher()
    )
    assert reply.outcome == TutorOutcome.AWAIT_STUDENT
    assert reply.awaiting_item_id == opening.awaiting_item_id
    with Session(engine) as session:
        assert not session.exec(
            select(Evidence).where(Evidence.kind == EvidenceKind.DIAGNOSIS)
        ).all()
        state = TeachingState.model_validate(session.get(TutorSession, task.task_id).state)
        assert state.probe_count == 0


async def test_spec_null_practice_is_accepted_and_model_judged(setup):
    engine, knowledge, task = setup

    class TextModel(Model):
        async def generate(self, messages, output, tools=None):
            payload = json.loads(messages[-1].content)
            if output is QuestionDraft and not payload.get("active_ids"):
                return QuestionDraft(
                    stem="请说明下一步的求导依据",
                    spec=None,
                    answer="先对外层求导，再乘内层导数",
                    rubric=["说明外层求导", "乘内层导数"],
                )
            if output is AnswerReview:
                return AnswerReview(intent="answer", is_correct=True, reason="要点齐全")
            return await super().generate(messages, output, tools)

    tutor = create_tutor(llm=TextModel(), knowledge=knowledge, engine=engine)
    opening = await tutor.turn(input_for(task), Publisher())
    turn = await tutor.turn(input_for(task, "cos(3*x)", opening.awaiting_item_id), Publisher())
    assert turn.outcome == TutorOutcome.AWAIT_STUDENT
    with Session(engine) as session:
        item = session.get(Item, turn.awaiting_item_id)
        assert item.purpose == ItemPurpose.PRACTICE
        assert item.spec is None and item.rubric
    reply = await tutor.turn(input_for(task, "先对外层求导，再乘内层导数", item.id), Publisher())
    assert reply.outcome == TutorOutcome.REQUEST_VERIFICATION
    with Session(engine) as session:
        evidence = session.exec(
            select(Evidence).where(Evidence.item_id == item.id).order_by(Evidence.id.desc())
        ).first()
        assert evidence.is_correct is True and evidence.judged_by == JudgedBy.MODEL


async def test_spec_null_probe_is_rejected(setup):
    engine, knowledge, task = setup
    tutor = create_tutor(llm=Model(), knowledge=knowledge, engine=engine)
    item = Item(
        id="tutor.spec.null",
        knowledge_point_id=POINT,
        purpose=ItemPurpose.PROBE,
        stem="请说明证明思路",
        spec=None,
        answer="参考证明",
        rubric=["说明依据"],
        source=ItemSource.GENERATED,
    )
    state = TeachingState(phase="diagnosis", candidate_ids=["a", SLIP])
    failure = await tutor._gate(item, state, set(), review=False)
    assert "spec" in failure


async def test_three_round_budget_survives_reproposal_and_recreation(setup):
    engine, knowledge, task = setup
    model, pub = Model(), Publisher()
    tutor = create_tutor(llm=model, knowledge=knowledge, engine=engine)
    turn = await tutor.turn(input_for(task), pub)
    for index in range(3):
        tutor = create_tutor(llm=model, knowledge=knowledge, engine=engine)
        turn = await tutor.turn(input_for(task, "12345", turn.awaiting_item_id), pub)
        if index < 2:
            assert turn.outcome == TutorOutcome.AWAIT_STUDENT
    assert turn.outcome == TutorOutcome.REQUEST_REPLAN
    assert "三轮" in turn.replan_reason
    with Session(engine) as session:
        state = TeachingState.model_validate(session.get(TutorSession, task.task_id).state)
        assert state.probe_count == 3
        assert len(state.probe_item_ids) == 3
        assert len(session.exec(select(ItemExposure)).all()) == 3


async def test_generation_exhaustion_does_not_leak_bad_question(setup):
    engine, knowledge, task = setup

    class BadModel(Model):
        async def generate(self, messages, output, tools=None):
            if output is QuestionDraft:
                self.practice += 1
                raise LLMOutputError("invalid draft")
            return await super().generate(messages, output, tools)

    # Remove practice/verification fallbacks to exercise actual exhaustion.
    knowledge._items = [i for i in knowledge._items if i.purpose == "probe"]
    model = BadModel()
    tutor = create_tutor(llm=model, knowledge=knowledge, engine=engine)
    pub = Publisher()
    turn = await tutor.turn(input_for(task), pub)
    turn = await tutor.turn(input_for(task, "cos(3*x)", turn.awaiting_item_id), pub)
    assert turn.outcome == TutorOutcome.REQUEST_REPLAN
    assert model.practice == 3


async def test_receipt_survives_publish_failure_without_duplicate_evidence(setup):
    engine, knowledge, task = setup
    model = Model()
    tutor = create_tutor(llm=model, knowledge=knowledge, engine=engine)
    turn = await tutor.turn(input_for(task), Publisher())
    inp = input_for(task, "3*cos(3*x)", turn.awaiting_item_id)

    class BrokenPublisher:
        async def publish(self, event):
            raise RuntimeError("disconnected")

    with pytest.raises(RuntimeError):
        await tutor.turn(inp, BrokenPublisher())
    calls = len(model.calls)
    await create_tutor(llm=model, knowledge=knowledge, engine=engine).turn(inp, Publisher())
    assert len(model.calls) == calls
    with Session(engine) as session:
        assert (
            len(session.exec(select(Evidence).where(Evidence.kind == EvidenceKind.DIAGNOSIS)).all())
            == 1
        )
        assert len(session.exec(select(ItemExposure)).all()) == 2


async def test_practice_falls_back_to_unused_seed_after_three_failures(setup):
    engine, knowledge, task = setup

    class FailedDrafts(Model):
        async def generate(self, messages, output, tools=None):
            if output is QuestionDraft:
                self.practice += 1
                raise LLMOutputError("bad draft")
            return await super().generate(messages, output, tools)

    model, publisher = FailedDrafts(), Publisher()
    tutor = create_tutor(llm=model, knowledge=knowledge, engine=engine)
    turn = await tutor.turn(input_for(task), publisher)
    turn = await tutor.turn(input_for(task, "cos(3*x)", turn.awaiting_item_id), publisher)
    assert turn.outcome == TutorOutcome.AWAIT_STUDENT
    assert model.practice == 3
    assert "中心差商" in turn.message
    with Session(engine) as session:
        item = session.get(Item, turn.awaiting_item_id)
        assert item.purpose == "practice" and item.source == "seed"
        assert item.dimension is None and not item.predictions
        assert item.fingerprint != knowledge.seed_items(POINT)[0].fingerprint


async def test_stream_failure_preserves_judgement_and_staged_question(setup):
    engine, knowledge, task = setup

    class InterruptedModel(Model):
        broken = True

        async def stream(self, messages):
            if self.broken:
                raise LLMOutputError("stream interrupted")
            async for chunk in super().stream(messages):
                yield chunk

    model, publisher = InterruptedModel(), Publisher()
    tutor = create_tutor(llm=model, knowledge=knowledge, engine=engine)
    turn = await tutor.turn(input_for(task), publisher)
    inp = input_for(task, "cos(3*x)", turn.awaiting_item_id)
    with pytest.raises(LLMOutputError):
        await tutor.turn(inp, publisher)
    drafts = model.practice
    model.broken = False
    turn = await create_tutor(llm=model, knowledge=knowledge, engine=engine).turn(inp, publisher)
    assert turn.outcome == TutorOutcome.AWAIT_STUDENT
    assert model.practice == drafts
    with Session(engine) as session:
        assert (
            len(session.exec(select(Evidence).where(Evidence.kind == EvidenceKind.DIAGNOSIS)).all())
            == 1
        )
        assert len(diagnosis(session, task.task_id).probes) == 1


async def test_model_fallback_cannot_override_known_tool_results(setup):
    from app.contracts.verify import Equivalence
    from app.verify.tools import SympyMathTools

    engine, knowledge, task = setup

    class PartialTools(SympyMathTools):
        def check_equivalent(self, a, b, mode, variables):
            if a == "ambiguous":
                if b == r"\cos(3x)":
                    return Equivalence(equivalent=False, method="symbolic")
                return Equivalence(equivalent=None, method="none")
            return super().check_equivalent(a, b, mode, variables)

    class FallbackModel(Model):
        async def generate(self, messages, output, tools=None):
            if output is AnswerReview:
                return AnswerReview(
                    is_correct=True,
                    matches={MISS: True, FORMULA: True, SLIP: True},
                    reason="自然语言数学作答",
                )
            return await super().generate(messages, output, tools)

    pub = Publisher()
    tutor = create_tutor(
        llm=FallbackModel(), knowledge=knowledge, engine=engine, tools=PartialTools()
    )
    turn = await tutor.turn(input_for(task), pub)
    await tutor.turn(input_for(task, "ambiguous", turn.awaiting_item_id), pub)
    with Session(engine) as session:
        evidence = session.exec(
            select(Evidence).where(Evidence.kind == EvidenceKind.DIAGNOSIS)
        ).one()
        assert evidence.judged_by == JudgedBy.MODEL
        states = {h.misconception_id: h.status for h in session.exec(select(Hypothesis))}
        assert states[FORMULA] == HypothesisStatus.REFUTED
        assert states[MISS] == states[SLIP] == HypothesisStatus.SUPPORTED


async def test_tutoring_stream_releases_sqlite_writer_and_emits_incrementally(setup):
    engine, knowledge, task = setup
    publisher = Publisher()

    class ConcurrentModel(Model):
        async def stream(self, messages):
            with Session(engine) as session:
                session.add(Student(id="other", name="另一个学生"))
                session.commit()
            yield "先看每层变化率。"
            assert any(e.type == "token" and e.text == "先看每层变化率。" for e in publisher.events)
            yield "再自己完成计算。"

    tutor = create_tutor(llm=ConcurrentModel(), knowledge=knowledge, engine=engine)
    turn = await tutor.turn(input_for(task), publisher)
    await tutor.turn(input_for(task, "cos(3*x)", turn.awaiting_item_id), publisher)
    with Session(engine) as session:
        assert session.get(Student, "other") is not None


async def test_teacher_round_limit_requests_escalation(setup):
    from app.contracts.entities import TeachingGoal

    engine, knowledge, task = setup
    with Session(engine) as session:
        session.add(
            TeachingGoal(
                knowledge_point_ids=[POINT], deadline=TODAY, created_on=TODAY, max_rounds=1
            )
        )
        session.commit()
    tutor = create_tutor(llm=Model(), knowledge=knowledge, engine=engine)
    pub = Publisher()
    turn = await tutor.turn(input_for(task), pub)
    turn = await tutor.turn(input_for(task, "cos(3*x)", turn.awaiting_item_id), pub)
    for _ in range(2):
        turn = await tutor.turn(input_for(task, "0", turn.awaiting_item_id), pub)
    assert "达到教师轮次上限，请转交教师" in turn.replan_reason
    with Session(engine) as session:
        assert session.get(Mastery, ("A", POINT)).state == MasteryState.TUTORING
