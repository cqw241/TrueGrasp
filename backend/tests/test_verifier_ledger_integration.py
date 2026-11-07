"""T18/T19 acceptance: real verifier results certify A and B through the ledger."""

from datetime import date
from pathlib import Path

import pytest
from sqlmodel import Session

from app.agents.verifier import create_verifier
from app.agents.verifier.agent import ItemDraft, ItemReview
from app.contracts.agents import ConfirmedMisconception, PlannerOutput, VerifierInput
from app.contracts.entities import Evidence, Item, Mastery, Student, Task
from app.contracts.enums import (
    Dimension,
    EvidenceKind,
    ItemPurpose,
    ItemSource,
    JudgedBy,
    MasteryState,
    TaskType,
)
from app.contracts.values import ItemSpec, Prediction
from app.db import init_db, make_engine
from app.knowledge.content import load
from app.knowledge.graph import NxKnowledgeGraph
from app.knowledge.ingest import ingest
from app.ledger.service import append_evidence
from app.orchestration import EventBus, Orchestrator
from app.verify.tools import SympyMathTools


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_ordinary_task_can_select_authored_series_explanation(tmp_path):
    lectures = Path(__file__).parents[2] / "content/lectures"
    content = load([lectures / "12.yaml"])
    engine = make_engine(f"sqlite:///{tmp_path / 'series.db'}")
    init_db(engine)

    class Planner:
        async def plan(self, inp):
            return PlannerOutput()

    class Model:
        async def generate(self, messages, output, tools=None):
            assert output is ItemReview
            return ItemReview(valid=True, reason="反例与原评分要点一致")

    try:
        with Session(engine) as session:
            ingest(session, content)
            session.add(Student(id="series", name="级数学生"))
            session.add(
                Task(
                    id=1,
                    student_id="series",
                    type=TaskType.VERIFICATION,
                    knowledge_point_id="calc.ch12.s1",
                    created_on=date(2026, 11, 7),
                )
            )
            session.commit()
        verifier = create_verifier(llm=Model(), knowledge=NxKnowledgeGraph.from_engine(engine))
        async with Orchestrator.open(
            engine, ":memory:", EventBus(), planner=Planner(), verifier=verifier
        ) as runtime:
            await runtime.start_task(1)
            [message] = await runtime.get_messages(1)
            seed = content.items["calc.ch12.s1.seed_term_zero_counterexample"]
            assert message.item.dimension == Dimension.EXPLANATION
            assert message.item.rubric == seed.rubric
    finally:
        engine.dispose()


@pytest.mark.anyio
async def test_staged_legacy_duplicate_is_replaced_before_it_is_shown(tmp_path):
    content = load(sorted((Path(__file__).parents[2] / "content/lectures").glob("*.yaml")))
    engine = make_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    init_db(engine)
    point = "calc.ch1.function"
    today = date(2026, 11, 7)
    tools = SympyMathTools()

    def item(id, expr, variable, fingerprint):
        return Item(
            id=id,
            knowledge_point_id=point,
            purpose=ItemPurpose.VERIFICATION,
            dimension=Dimension.CORRECTNESS,
            stem=f"求 {expr} 的导数",
            spec=ItemSpec(operation="derivative", expr=expr, variables=[variable]),
            answer="test",
            fingerprint=fingerprint,
            source=ItemSource.GENERATED,
        )

    seen = item("seen", "x**2", "x", "legacy-seen-fingerprint")
    normalized_seen = tools.fingerprint(seen.spec)
    staged = item("staged", "t**2", "t", "legacy-staged-fingerprint")
    fresh = item("fresh", "x**3", "x", "fresh-fingerprint")

    class Planner:
        async def plan(self, inp):
            return PlannerOutput()

    class Verifier:
        calls = 0

        async def compose(self, inp):
            self.calls += 1
            assert normalized_seen in inp.used_fingerprints
            return [staged] if self.calls == 1 else [fresh]

    try:
        with Session(engine) as session:
            ingest(session, content)
            session.add(Student(id="legacy", name="学生"))
            session.add(seen)
            session.flush()
            session.add(
                Evidence(
                    student_id="legacy",
                    knowledge_point_id=point,
                    item_id=seen.id,
                    kind=EvidenceKind.HOMEWORK,
                    judged_by=JudgedBy.TOOL,
                    teaching_date=today,
                )
            )
            session.add(
                Task(
                    id=1,
                    student_id="legacy",
                    type=TaskType.VERIFICATION,
                    knowledge_point_id=point,
                    misconception_ids=["slip"],
                    created_on=today,
                )
            )
            session.commit()
        verifier = Verifier()
        async with Orchestrator.open(
            engine, ":memory:", EventBus(), planner=Planner(), verifier=verifier
        ) as runtime:
            await runtime.start_task(1)
            [message] = await runtime.get_messages(1)
            assert message.item.id == fresh.id
            assert verifier.calls == 2
    finally:
        engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "student,cause",
    [
        ("A", "calc.ch2.chain.miss_inner"),
        ("B", "calc.ch2.chain.formula_sub"),
    ],
)
async def test_independent_passes_produce_initial_mastery(tmp_path, student, cause):
    content = load(sorted((Path(__file__).parents[2] / "content/lectures").glob("*.yaml")))
    graph = NxKnowledgeGraph(
        list(content.points.values()),
        list(content.misconceptions.values()),
        list(content.strategies.values()),
        list(content.items.values()),
    )
    point = "calc.ch2.chain_rule_multi"
    today = date(2026, 9, 8)
    drafts = [
        ItemDraft(
            stem=r"求 $\sin^3(2x)$ 的导数",
            spec=ItemSpec(operation="derivative", expr="sin(2*x)**3", variables=["x"]),
            answer=r"6\sin^2(2x)\cos(2x)",
        ),
        ItemDraft(
            stem=r"求 $\cos^2(x^2)$ 的导数",
            spec=ItemSpec(operation="derivative", expr="cos(x**2)**2", variables=["x"]),
            answer=r"-4x\cos(x^2)\sin(x^2)",
            predictions=[Prediction(misconception_id=cause, answer=r"-2\cos(x^2)\sin(x^2)")],
        ),
        ItemDraft(
            stem=r"求 $\sin(e^x)$ 的导数，并识别复合层次",
            spec=ItemSpec(operation="derivative", expr="sin(exp(x))", variables=["x"]),
            answer=r"e^x\cos(e^x)",
        ),
    ]

    class Model:
        async def generate(self, messages, output, tools=None):
            if output is ItemReview:
                return ItemReview(valid=True, reason="测试审题替身：题干、结构与维度符合")
            assert output is ItemDraft
            return drafts.pop(0)

    verifier = create_verifier(llm=Model(), knowledge=graph)
    items = await verifier.compose(
        VerifierInput(
            knowledge_point_id=point,
            dimensions=[Dimension.CORRECTNESS, Dimension.DISTRACTOR, Dimension.NEAR_TRANSFER],
            confirmed_misconceptions=[
                ConfirmedMisconception(id=cause, description=graph.misconception(cause).description)
            ],
            used_fingerprints=[],
        )
    )
    engine = make_engine(f"sqlite:///{tmp_path / 'acceptance.db'}")
    init_db(engine)
    try:
        with Session(engine) as session:
            ingest(session, content)
            session.add(Student(id=student, name=student))
            session.flush()
            task = Task(
                type=TaskType.VERIFICATION,
                student_id=student,
                knowledge_point_id=point,
                misconception_ids=[cause],
                created_on=today,
            )
            session.add(task)
            session.flush()
            for item in items:
                session.add(item)
                session.flush()
                judgement = await verifier.judge(item, item.answer)
                append_evidence(
                    session,
                    Evidence(
                        student_id=student,
                        knowledge_point_id=point,
                        kind=EvidenceKind.VERIFICATION,
                        dimension=item.dimension,
                        task_id=task.id,
                        item_id=item.id,
                        student_answer=item.answer,
                        correct_answer=item.answer,
                        predictions=item.predictions,
                        teaching_date=today,
                        **judgement.model_dump(),
                    ),
                )
            session.commit()
            mastery = session.get(Mastery, (student, point))
            assert mastery.state == MasteryState.INITIAL
            assert len(mastery.evidence_ids) == 3
    finally:
        engine.dispose()
