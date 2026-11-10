"""Real tutor, real verifier and ledger across the public orchestration seam."""

import json
from datetime import timedelta

import pytest
from httpx2 import ASGITransport, AsyncClient
from sqlmodel import Session, select

from app.agents.planner import create_planner
from app.agents.tutor import create_tutor
from app.agents.tutor.models import CandidateSelection
from app.agents.tutor.queries import diagnosis
from app.agents.tutor.student_question import (
    AttemptReview,
    Guidance,
    QuestionDraft,
    StudentQuestions,
)
from app.agents.verifier import create_verifier
from app.agents.verifier.agent import ItemDraft, ItemReview
from app.config import Settings
from app.contracts.agents import PlannerOutput
from app.contracts.api import MessageCreate
from app.contracts.entities import Event, Evidence, Mastery, Misconception, Task
from app.contracts.enums import ContentSource, EvidenceKind, MasteryState, TaskType
from app.contracts.events import HomeworkImported, make_event
from app.db import make_engine
from app.main import create_app
from app.orchestration import EventBus, Orchestrator
from tests.test_planner import PlanningModel
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


@pytest.mark.parametrize("initial", ["", "cos(x)"])
async def test_student_question_diagnosis_hints_and_independent_followup(setup, tmp_path, initial):
    engine, knowledge, _ = setup
    guidance = []

    class ModelForQuestion(CombinedModel):
        async def generate(self, messages, output, tools=None):
            payload = json.loads(messages[-1].content)
            if output is QuestionDraft:
                return QuestionDraft(
                    knowledge_point_id=POINT,
                    spec={"operation": "derivative", "expr": "sin(8*x)", "variables": ["x"]},
                    answer="8*cos(8*x)" if payload["problem"] != "bad" else "7",
                )
            if output is AttemptReview:
                return AttemptReview(
                    final_answer=payload["attempt"] if payload["attempt"] == "cos(x)" else "",
                    observation="需要检查内层导数",
                )
            if output is Guidance:
                guidance.append(payload)
                return Guidance(text="请检查内层函数和自变量的关系。")
            return await super().generate(messages, output, tools)

    model, planning = ModelForQuestion(), PlanningModel()
    app = create_app(
        engine=engine,
        checkpoint_path=str(tmp_path / "question.db"),
        orchestration_dependencies={
            "tutor": create_tutor(llm=model, knowledge=knowledge, engine=engine),
            "verifier": create_verifier(llm=model, knowledge=knowledge),
            "planner": create_planner(engine=engine, llm=planning, knowledge=knowledge),
            "questions": StudentQuestions(model, knowledge),
        },
    )
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        runtime = app.state.orchestrator
        url = "/api/students/A/questions"
        assert (await client.post(url, json={"content": "bad"})).status_code == 422
        response = await client.post(url, json={"content": "求 sin(8x) 的导数", "attempt": initial})
        assert response.status_code == 202
        question = response.json()
        task_id = question["id"]
        assert question["type"] == "question"
        await runtime.wait_idle()

        async def reply(text):
            target = await runtime.get_answer_target(task_id)
            assert target.phase == "waiting"
            result = await client.post(
                f"/api/tasks/{task_id}/messages",
                params={"expected_turn": target.turn},
                json={"content": text, "answering_item_id": target.item_id},
            )
            assert result.status_code == 202
            await runtime.wait_idle()

        if initial:
            probe = (await runtime.get_messages(task_id))[-1]
            assert probe.item and not probe.item.answer and not probe.item.predictions
            await reply("cos(3*x)")
            assert guidance[-1]["strategy_cards"][0]["id"] == "numeric_slope_conflict"
        else:
            assert "尝试" in (await runtime.get_messages(task_id))[-1].content
            await reply("不知道如何处理内层")
            assert (await runtime.get_messages(task_id))[-1].hint_level is None
            await reply("还是不会")
        for level in (1, 2, 3):
            last = (await runtime.get_messages(task_id))[-1]
            assert last.hint_level == level
            assert not last.solution
            assert "8*cos(8*x)" not in last.content
            await reply("还是不会")
        last = (await runtime.get_messages(task_id))[-1]
        assert last.solution and "8*cos(8*x)" in last.content
        assert (await runtime.get_answer_target(task_id)).phase == "closed"
        with Session(engine) as session:
            evidence = session.get(Evidence, question["evidence_ids"][0])
            assert evidence.student_answer == initial
            assert evidence.is_correct is None
            verify = session.exec(select(Task).where(Task.type == TaskType.VERIFICATION)).one()
            assert evidence.id in verify.evidence_ids
            assert verify.knowledge_point_id == POINT
            task = session.get(Task, task_id)
            assert task.status == "done"
            followup_id = verify.id
        followup = (await runtime.get_messages(followup_id))[-1]
        assert followup.item.dimension == "correctness"
        assert followup.item.stem != "求 sin(8x) 的导数"


async def test_teacher_takeover_survives_restart_and_handback_keeps_same_probe(setup, tmp_path):
    engine, knowledge, task = setup
    model = CombinedModel()
    with Session(engine) as session:
        verify = Task(
            type=TaskType.VERIFICATION, student_id="A", knowledge_point_id=POINT, created_on=TODAY
        )
        session.add(verify)
        session.commit()
        verify_url = f"/api/tasks/{verify.id}"
    dependencies = {
        "tutor": create_tutor(llm=model, knowledge=knowledge, engine=engine),
        "verifier": create_verifier(llm=model, knowledge=knowledge),
        "planner": Planner(),
        "teaching_date": lambda: TODAY,
    }
    task_url = f"/api/tasks/{task.task_id}"
    for restart in (False, True):
        app = create_app(
            engine=engine,
            checkpoint_path=str(tmp_path / "pause.db"),
            orchestration_dependencies=dependencies,
        )
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            runtime = app.state.orchestrator
            if not restart:
                assert (await client.post(task_url + "/start")).status_code == 202
                await runtime.wait_idle()
                probe = (await client.get(task_url + "/answer-target")).json()
                assert probe["phase"] == "waiting"
                assert (await client.post(task_url + "/takeover", json={})).json()["taken_over"]
                call_count = len(model.calls)
            paused = (await client.get(task_url + "/answer-target")).json()
            assert paused["phase"] == "idle" and "教师接管" in paused["prompt"]
            assert (await client.get("/api/tasks/controls")).json()[0]["taken_over"]
            assert (await client.post(task_url + "/start")).status_code == 409
            assert (await client.post(verify_url + "/start")).status_code == 409
            reply = {"content": "cos(3*x)", "answering_item_id": probe["item_id"]}
            assert (await client.post(task_url + "/messages", json=reply)).status_code == 409
            assert len(model.calls) == call_count
            if restart:
                assert not (await client.post(task_url + "/handback")).json()["taken_over"]
                await runtime.wait_idle()
                assert (await client.get(task_url + "/answer-target")).json() == probe
                assert not (await client.get(verify_url + "/answer-target")).json()["prompt"]
                assert (await client.post(task_url + "/messages", json=reply)).status_code == 202
                await runtime.wait_idle()
                view = (await client.get(task_url + "/diagnosis")).json()
                assert view["probes"][0]["student_answer"] == "cos(3*x)"
    with Session(engine) as session:
        reasons = list(
            session.exec(
                select(Evidence.reason)
                .where(Evidence.kind == EvidenceKind.TEACHER)
                .order_by(Evidence.id)
            )
        )
        assert reasons == ["教师接管辅导", "教师交还 Agent"]


async def test_teacher_adoption_reaches_diagnosis_and_override_blocks_replan(setup, tmp_path):
    engine, _, task = setup
    with Session(engine) as session:
        session.add(
            Misconception(
                id="unmodeled.test",
                knowledge_point_id=POINT,
                family="chain",
                description="待审核描述",
                source=ContentSource.UNMODELED,
            )
        )
        session.flush()
        session.add(
            Evidence(
                student_id="A",
                task_id=task.task_id,
                knowledge_point_id=POINT,
                kind=EvidenceKind.DIAGNOSIS,
                matched_misconception_id="unmodeled.test",
                judged_by="model",
                teaching_date=TODAY,
            )
        )
        session.commit()
    app = create_app(
        engine=engine,
        checkpoint_path=str(tmp_path / "teacher.db"),
        orchestration_dependencies={"tutor": None, "planner": None, "verifier": None},
    )
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        runtime = app.state.orchestrator
        graph = app.state.knowledge
        runtime.planner = create_planner(engine=engine, knowledge=graph, llm=PlanningModel())
        pending = (await client.get("/api/misconceptions/pending")).json()
        assert [row["id"] for row in pending] == ["unmodeled.test"]
        adopted = await client.post(
            "/api/misconceptions/unmodeled.test/adopt", json={"description": "内外层次序颠倒"}
        )
        assert adopted.status_code == 200 and adopted.json()["source"] == "teacher"
        assert (await client.get("/api/misconceptions/pending")).json() == []
        assert graph.misconception("unmodeled.test").description == "内外层次序颠倒"
        model = Model()
        tutor = create_tutor(llm=model, knowledge=graph, engine=engine)
        await tutor.turn(input_for(task), Publisher())
        pool = next(
            payload["pool"] for output, payload in model.calls if output is CandidateSelection
        )
        assert any(row["id"] == "unmodeled.test" for row in pool)

        changed = await client.patch(
            f"/api/tasks/{task.task_id}", json={"cancel": True, "note": "改为课堂讲解"}
        )
        assert changed.json()["status"] == "cancelled"
        assert (await client.get("/api/tasks/controls")).json()[0]["overridden"]
        with Session(engine) as session:
            event = make_event(
                HomeworkImported(
                    knowledge_point_ids=[POINT], evidence_ids=[task.error_evidence_id]
                ),
                TODAY,
            )
            session.add(event)
            session.commit()
            event_id = event.id
        await runtime.handle_event(event_id, dispatch=False)
        with Session(engine) as session:
            assert session.get(Event, event_id).processed
            assert [
                t.id for t in session.exec(select(Task).where(Task.type == TaskType.TUTORING))
            ] == [task.task_id]
            records = session.exec(
                select(Evidence).where(Evidence.kind == EvidenceKind.TEACHER)
            ).all()
            assert len(records) == 2 and all(row.is_correct is None for row in records)


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
