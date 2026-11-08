"""Real tutor, real verifier and ledger across the public orchestration seam."""

import json
from datetime import timedelta

import pytest
from httpx2 import ASGITransport, AsyncClient
from sqlmodel import Session, select

from app.agents.tutor import create_tutor
from app.agents.tutor.models import CandidateSelection
from app.agents.tutor.queries import diagnosis
from app.agents.verifier import create_verifier
from app.agents.verifier.agent import ItemDraft, ItemReview
from app.config import Settings
from app.contracts.agents import PlannerOutput
from app.contracts.api import MessageCreate
from app.contracts.entities import Evidence, Mastery
from app.contracts.enums import EvidenceKind, MasteryState
from app.db import make_engine
from app.main import create_app
from app.orchestration import EventBus, Orchestrator
from tests.test_tutor import FORMULA, MISS, POINT, TODAY, Model, Publisher, input_for
from tests.test_tutor import setup as setup

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Planner:
    async def plan(self, inp):
        return PlannerOutput()


class CombinedModel(Model):
    async def generate(self, messages, output, tools=None):
        if output is ItemReview:
            return ItemReview(valid=True, reason="符合目标维度及结构")
        if output is ItemDraft:
            payload = json.loads(messages[-1].content)
            dimension = payload["dimension"]
            seeds = {
                "correctness": ("sin(2*x)**3", r"6\sin^2(2x)\cos(2x)", []),
                "distractor": (
                    "cos(x**2)**2",
                    r"-4x\cos(x^2)\sin(x^2)",
                    [
                        {"misconception_id": m["id"], "answer": r"-2\cos(x^2)\sin(x^2)"}
                        for m in payload["input"]["confirmed_misconceptions"]
                    ],
                ),
                "near_transfer": (
                    "exp(sqrt(1+t**2))",
                    r"\frac{te^{\sqrt{1+t^2}}}{\sqrt{1+t^2}}",
                    [],
                ),
            }
            expr, answer, predictions = seeds[dimension]
            return ItemDraft(
                stem=f"求导：{expr}",
                spec={
                    "operation": "derivative",
                    "expr": expr,
                    "variables": ["t"] if dimension == "near_transfer" else ["x"],
                },
                answer=answer,
                predictions=predictions,
            )
        return await super().generate(messages, output, tools)


@pytest.mark.parametrize(
    ("answers", "expected"),
    [
        (["3*cos(3*x)", "-2*cos(5*x)*sin(5*x)"], MISS),
        (["cos(3*x)"], FORMULA),
        (["3*cos(3*x)", "-10*cos(5*x)*sin(5*x)"], "slip"),
    ],
)
async def test_complete_tutor_verifier_path(setup, tmp_path, answers, expected):
    engine, knowledge, task = setup
    model = CombinedModel()
    bus = EventBus()
    tutor = create_tutor(llm=model, knowledge=knowledge, engine=engine)
    verifier = create_verifier(llm=model, knowledge=knowledge)
    checkpoint = str(tmp_path / "checkpoints.db")
    dependencies = dict(
        planner=Planner(),
        tutor=tutor,
        verifier=verifier,
        knowledge=knowledge,
        pending_poll_interval=None,
    )
    async with Orchestrator.open(engine, checkpoint, bus, **dependencies) as runtime:
        await runtime.start_task(task.task_id, teaching_date=TODAY + timedelta(days=1))
        for answer in answers:
            target = await runtime.get_answer_target(task.task_id)
            await runtime.submit_message(
                task.task_id, MessageCreate(content=answer, answering_item_id=target.item_id)
            )
        if expected != "slip":
            message = (await runtime.get_messages(task.task_id))[-1]
            await runtime.submit_message(
                task.task_id,
                MessageCreate(content=message.item.answer, answering_item_id=message.item.id),
            )
        # Stop at a real verification wait and recover it from disk.
    async with Orchestrator.open(engine, checkpoint, bus, **dependencies) as runtime:
        dimensions = 1 if expected == "slip" else 3
        for _ in range(dimensions):
            message = (await runtime.get_messages(task.task_id))[-1]
            assert message.item.purpose == "verification"
            await runtime.submit_message(
                task.task_id,
                MessageCreate(content=message.item.answer, answering_item_id=message.item.id),
            )
        await runtime.wait_idle()
        with Session(engine) as session:
            rows = session.exec(
                select(Evidence).where(
                    Evidence.task_id == task.task_id, Evidence.kind == EvidenceKind.VERIFICATION
                )
            ).all()
            assert len(rows) == dimensions
            diagnostic = session.exec(
                select(Evidence).where(
                    Evidence.task_id == task.task_id, Evidence.kind == EvidenceKind.DIAGNOSIS
                )
            ).all()
            assert all(e.teaching_date == TODAY + timedelta(days=1) for e in diagnostic)
            assert all(row.is_correct for row in rows)
            mastery = session.get(Mastery, ("A", POINT))
            assert mastery.state == (
                MasteryState.TUTORING if expected == "slip" else MasteryState.INITIAL
            )
            # The existing ledger does not claim multi-dimension mastery for a slip.
            assert [
                h.misconception_id
                for h in diagnosis(session, task.task_id).hypotheses
                if h.status == "supported"
            ] == [expected]
    await bus.close()


async def test_diagnosis_http_shows_unanswered_predictions(setup, tmp_path):
    engine, knowledge, task = setup
    tutor = create_tutor(llm=Model(), knowledge=knowledge, engine=engine)
    await tutor.turn(input_for(task), Publisher())
    app = create_app(
        engine=engine,
        checkpoint_path=str(tmp_path / "http.db"),
        orchestration_dependencies={"tutor": tutor, "verifier": None},
    )
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.get(f"/api/tasks/{task.task_id}/diagnosis")
        assert response.status_code == 200
        assert response.json()["probes"][0]["student_answer"] is None
        assert len(response.json()["probes"][0]["predictions"]) == 3
        assert (await client.get("/api/tasks/999/diagnosis")).status_code == 404


async def test_shared_initial_error_cannot_be_pruned_by_model(setup):
    engine, knowledge, task = setup

    class PrematureModel(Model):
        async def generate(self, messages, output, tools=None):
            if output is CandidateSelection:
                return CandidateSelection(misconception_ids=[MISS], reason="过早选择单一错因")
            return await super().generate(messages, output, tools)

    tutor = create_tutor(llm=PrematureModel(), knowledge=knowledge, engine=engine)
    await tutor.turn(input_for(task), Publisher())
    with Session(engine) as session:
        assert {h.misconception_id for h in diagnosis(session, task.task_id).hypotheses} == {
            MISS,
            FORMULA,
            "slip",
        }


@pytest.mark.parametrize(
    "disabled", [{}, {"tutor": None}, {"verifier": None}, {"tutor": None, "verifier": None}]
)
async def test_auto_tutor_shares_clock_date_and_model_client(tmp_path, monkeypatch, disabled):
    clients = []

    class Client:
        def __init__(self, settings):
            self.closed = False
            clients.append(self)

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(
        "app.main.get_settings",
        lambda: Settings(llm_api_key="test-only", teaching_clock_initial_date=TODAY),
    )
    monkeypatch.setattr("app.main.OpenAICompatClient", Client)
    engine = make_engine(f"sqlite:///{tmp_path / 'assembly.db'}")
    try:
        app = create_app(
            engine=engine,
            checkpoint_path=str(tmp_path / "assembly-checkpoints.db"),
            orchestration_dependencies={"planner": None, **disabled},
        )
        async with app.router.lifespan_context(app):
            runtime = app.state.orchestrator
            assert len(clients) == (0 if len(disabled) == 2 else 1)
            for name in ("tutor", "verifier"):
                agent = getattr(runtime, name)
                if name in disabled:
                    assert agent is None
                else:
                    assert agent.llm is clients[0]
            await app.state.clock.advance(4)
            assert runtime.teaching_date() == TODAY + timedelta(days=4)
            if runtime.tutor is not None:
                assert runtime.tutor.teaching_date() == TODAY + timedelta(days=4)
        assert all(c.closed for c in clients)
    finally:
        engine.dispose()
