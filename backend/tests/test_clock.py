"""T07 验收：教学日期、到期恢复、三条预检规则与独立验证上下文。"""

import asyncio
from contextlib import suppress
from datetime import date, timedelta
from pathlib import Path

import pytest
from httpx2 import ASGITransport, AsyncClient
from sqlmodel import Session, select

from app.clock.service import ClockError, TeachingClock
from app.clock.storage import DueNotification, LectureEntry, PrecheckContext, TeachingClockState
from app.config import get_settings
from app.contracts.agents import PlannerOutput
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
)
from app.contracts.enums import (
    Dimension,
    EventType,
    EvidenceKind,
    HypothesisStatus,
    ItemPurpose,
    ItemSource,
    JudgedBy,
    MasteryState,
    TaskStatus,
    TaskType,
)
from app.contracts.events import read_payload
from app.db import init_db, make_engine
from app.knowledge.content import load
from app.knowledge.graph import NxKnowledgeGraph
from app.knowledge.ingest import ingest
from app.main import _follow_real_date, create_app
from app.orchestration import EventBus, Orchestrator
from app.orchestration.storage import PendingWork

TODAY = date(2026, 9, 7)
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def business(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'business.db'}")
    init_db(engine)
    with Session(engine) as session:
        session.add_all(
            [
                Student(id="A", name="学生 A"),
                KnowledgePoint(id="old", name="链式法则", lecture=2, section=2),
                KnowledgePoint(id="indirect", name="函数复合", lecture=1, section=1),
                KnowledgePoint(
                    id="new", name="多元复合求导", lecture=9, section=4, prerequisite_ids=["old"]
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                Misconception(id="m", knowledge_point_id="old", family="chain", description="漏层"),
                Misconception(
                    id="recur",
                    knowledge_point_id="indirect",
                    family="composition",
                    description="误读复合",
                    recurs_in=["new"],
                ),
            ]
        )
        session.commit()
    yield engine
    engine.dispose()


class Recorder:
    def __init__(self):
        self.events = []

    async def publish(self, event):
        self.events.append(event)


def clock_for(engine, **kwargs):
    return TeachingClock(engine, NxKnowledgeGraph.from_engine(engine), Recorder(), **kwargs)


def add_history(session, student, kp, mis, status=HypothesisStatus.SUPPORTED):
    task = Task(
        type=TaskType.TUTORING,
        student_id=student,
        knowledge_point_id=kp,
        created_on=TODAY,
        status=TaskStatus.DONE,
    )
    session.add(task)
    session.flush()
    evidence = Evidence(
        student_id=student,
        knowledge_point_id=kp,
        task_id=task.id,
        kind=EvidenceKind.DIAGNOSIS,
        judged_by=JudgedBy.TOOL,
        teaching_date=TODAY,
        is_correct=False,
    )
    session.add(evidence)
    session.flush()
    session.add(
        Hypothesis(
            student_id=student,
            task_id=task.id,
            error_evidence_id=evidence.id,
            misconception_id=mis,
            status=status,
            evidence_ids=[evidence.id],
        )
    )
    return evidence.id


async def test_advance_and_restart_scan_only_due_pending_tasks(business):
    with Session(business) as session:
        for offset, status in [
            (4, TaskStatus.PENDING),
            (5, TaskStatus.PENDING),
            (-1, TaskStatus.ACTIVE),
            (-1, TaskStatus.DONE),
            (-1, TaskStatus.CANCELLED),
            (None, TaskStatus.PENDING),
        ]:
            session.add(
                Task(
                    type=TaskType.RETEST,
                    student_id="A",
                    knowledge_point_id="old",
                    created_on=TODAY,
                    due_date=TODAY + timedelta(days=offset) if offset is not None else None,
                    status=status,
                )
            )
        session.commit()
    clock = clock_for(business)
    await clock.scan()
    with Session(business) as session:
        assert session.exec(select(Event)).all() == []
    assert (await clock.advance(4)).teaching_date == TODAY + timedelta(days=4)
    # 重复扫描与同一进程的并发调用都不重复生成到期事件。
    await asyncio.gather(clock.scan(), clock.scan())
    clock = clock_for(business, initial_date=date(2000, 1, 1))
    assert clock.current_date() == TODAY + timedelta(days=4)
    await clock.scan()
    with Session(business) as session:
        events = session.exec(select(Event)).all()
        assert [(e.type, read_payload(e).task_id, e.student_id) for e in events] == [
            (EventType.TASK_DUE, 1, "A")
        ]
        assert session.get(PendingWork, f"event:{events[0].id}").pending
        assert session.get(DueNotification, 1).event_id == events[0].id
        # 模拟关机期间已经进入下一教学日，启动扫描会补上遗漏的任务。
        row = session.get(TeachingClockState, 1)
        row.teaching_date += timedelta(days=1)
        session.commit()
    await clock_for(business).scan()
    with Session(business) as session:
        assert {read_payload(e).task_id for e in session.exec(select(Event))} == {1, 2}


async def test_three_precheck_rules_and_precise_roster(business):
    cases = {
        "A": (MasteryState.CONSOLIDATED, "m", HypothesisStatus.SUPPORTED),
        "low": (MasteryState.INITIAL, None, None),
        "missing": (None, None, None),
        "recur": (MasteryState.INTEGRATED, "recur", HypothesisStatus.SUPPORTED),
        "safe": (MasteryState.CONSOLIDATED, None, None),
        "integrated": (MasteryState.INTEGRATED, None, None),
        "pending": (MasteryState.CONSOLIDATED, "m", HypothesisStatus.PENDING),
        "refuted": (MasteryState.CONSOLIDATED, "m", HypothesisStatus.REFUTED),
        "slip": (MasteryState.CONSOLIDATED, "slip", HypothesisStatus.SUPPORTED),
    }
    evidence = {}
    with Session(business) as session:
        session.add_all([Student(id=s, name=s) for s in cases if s != "A"])
        session.flush()
        for student, (state, mis, status) in cases.items():
            if state is not None:
                session.add(
                    Mastery(
                        student_id=student, knowledge_point_id="old", state=state, updated_on=TODAY
                    )
                )
            if mis:
                evidence[student] = add_history(
                    session, student, "indirect" if mis == "recur" else "old", mis, status
                )
        session.commit()
    clock = clock_for(business)
    await clock.enter_lecture(9)
    with Session(business) as session:
        tasks = session.exec(select(Task).where(Task.type == TaskType.PRECHECK)).all()
        assert {(t.student_id, t.knowledge_point_id) for t in tasks} == {
            ("A", "old"),
            ("low", "old"),
            ("missing", "old"),
            ("recur", "indirect"),
        }
        assert all(t.due_date == TODAY and t.created_on == TODAY for t in tasks)
        a = next(t for t in tasks if t.student_id == "A")
        assert a.misconception_ids == ["m"] and a.evidence_ids == [evidence["A"]]
        recur = next(t for t in tasks if t.student_id == "recur")
        assert recur.misconception_ids == ["recur"]
        assert recur.evidence_ids == [evidence["recur"]]
        assert "可能在新知识点复发" in recur.reason
        assert {p.context_knowledge_point_id for p in session.exec(select(PrecheckContext))} == {
            "new"
        }
        assert len(session.exec(select(Event).where(Event.type == EventType.TASK_DUE)).all()) == 4
    # 重复进入只补新学生；历史任务即使结束也不重新创建。
    await clock.enter_lecture(9)
    with Session(business) as session:
        session.add(Student(id="late", name="新学生"))
        session.commit()
    await clock.enter_lecture(9)
    with Session(business) as session:
        assert len(session.exec(select(PrecheckContext)).all()) == 5
        assert len(session.exec(select(LectureEntry)).all()) == 1
        assert len(session.exec(select(Event)).all()) == 6
    assert {e.type for e in clock.publisher.events} == {"clock_advanced", "task_changed"}
    assert len([e for e in clock.publisher.events if e.type == "task_changed"]) == 5


async def test_transaction_rollback_keeps_clock_tasks_and_events_together(business, monkeypatch):
    clock = clock_for(business)

    def fail_scan(*args):
        raise RuntimeError("中断事务")

    with monkeypatch.context() as patch:
        patch.setattr(clock, "_scan_due", fail_scan)
        with pytest.raises(RuntimeError, match="中断事务"):
            await clock.enter_lecture(9)
        with pytest.raises(RuntimeError, match="中断事务"):
            await clock.advance(4)
    assert clock.state().lecture == 1
    assert clock.current_date() == TODAY
    with Session(business) as session:
        for table in (Task, Event, PendingWork, PrecheckContext, DueNotification, LectureEntry):
            assert session.exec(select(table)).all() == []
    assert clock.publisher.events == []
    await clock.enter_lecture(9)
    with Session(business) as session:
        assert len(session.exec(select(PrecheckContext)).all()) == 1


async def test_real_mode_follows_date_and_runs_daily_scan(business):
    today = [TODAY]
    clock = clock_for(business, mode="real", today=lambda: today[0])
    with Session(business) as session:
        session.add(
            Task(
                type=TaskType.RETEST,
                student_id="A",
                knowledge_point_id="old",
                created_on=TODAY,
                due_date=TODAY + timedelta(days=1),
            )
        )
        session.commit()
    await clock.scan()
    with pytest.raises(ClockError, match="真实日期模式"):
        await clock.advance()
    job = asyncio.create_task(_follow_real_date(clock, interval=0.01))
    try:
        today[0] += timedelta(days=1)
        assert clock.current_date() == today[0]
        async with asyncio.timeout(2):
            while clock.needs_date_sync():
                await asyncio.sleep(0.01)
        with Session(business) as session:
            assert len(session.exec(select(DueNotification)).all()) == 1
    finally:
        job.cancel()
        with suppress(asyncio.CancelledError):
            await job


class NoopPlanner:
    async def plan(self, inp):
        return PlannerOutput()


class RecordingVerifier:
    def __init__(self):
        self.inputs = []

    async def compose(self, inp):
        self.inputs.append(inp)
        return [
            Item(
                id=f"generated-{len(self.inputs)}",
                knowledge_point_id=inp.knowledge_point_id,
                purpose=ItemPurpose.VERIFICATION,
                dimension=inp.dimensions[0],
                stem="新题",
                answer="1",
                fingerprint=f"fingerprint-{len(self.inputs)}",
                source=ItemSource.GENERATED,
            )
        ]


@pytest.mark.parametrize("automatic", [False, True])
async def test_precheck_context_survives_restart_and_all_start_paths(business, tmp_path, automatic):
    with Session(business) as session:
        add_history(session, "A", "old", "m")
        session.commit()
    await clock_for(business).enter_lecture(9)
    with Session(business) as session:
        task_id = session.exec(select(PrecheckContext)).one().task_id
    verifier = RecordingVerifier()
    path = str(tmp_path / "checkpoint.db")
    async with Orchestrator.open(
        business,
        path,
        EventBus(),
        verifier=verifier,
        planner=NoopPlanner() if automatic else None,
        pending_poll_interval=None,
    ) as runtime:
        if not automatic:
            await runtime.enqueue_start(task_id)
        await runtime.wait_idle()
        assert len(verifier.inputs) == 1
        inp = verifier.inputs[0]
        assert inp.knowledge_point_id == "old"
        assert inp.context_knowledge_point_id == "new"
        assert inp.dimensions == [Dimension.CROSS_CHAPTER]
        assert [m.id for m in inp.confirmed_misconceptions] == ["m"]
    # 恢复已经等待作答的任务不重新出题。
    async with Orchestrator.open(
        business, path, EventBus(), verifier=verifier, pending_poll_interval=None
    ) as runtime:
        await runtime.start_task(task_id)
        await runtime.wait_idle()
        assert len(verifier.inputs) == 1


async def test_advance_reaches_retention_verifier_and_preserves_pending_without_planner(
    business, tmp_path
):
    with Session(business) as session:
        session.add(
            Task(
                type=TaskType.RETEST,
                student_id="A",
                knowledge_point_id="old",
                created_on=TODAY,
                due_date=TODAY + timedelta(days=4),
            )
        )
        session.commit()
    verifier = RecordingVerifier()
    async with Orchestrator.open(
        business,
        str(tmp_path / "cp.db"),
        EventBus(),
        verifier=verifier,
        pending_poll_interval=None,
    ) as runtime:
        clock = clock_for(business, wake=runtime.recover_pending)
        await clock.advance(4)
        await runtime.wait_idle()
        assert verifier.inputs == []
        runtime.planner = NoopPlanner()
        runtime.recover_pending()
        await runtime.wait_idle()
        assert verifier.inputs[0].dimensions == [Dimension.RETENTION]


async def test_clock_http_startup_scan_validation_and_persistence(business, tmp_path, monkeypatch):
    monkeypatch.setenv("TEACHING_CLOCK_MODE", "demo")
    monkeypatch.setenv("TEACHING_CLOCK_INITIAL_DATE", TODAY.isoformat())
    get_settings.cache_clear()
    try:
        app = create_app(
            engine=business,
            checkpoint_path=str(tmp_path / "http.db"),
            orchestration_dependencies={"verifier": None},
        )
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c,
        ):
            assert (await c.get("/api/clock")).json() == {
                "teaching_date": "2026-09-07",
                "lecture": 1,
            }
            assert (await c.post("/api/clock/advance", json={"days": 4})).json() == {
                "teaching_date": "2026-09-11",
                "lecture": 1,
            }
            assert (await c.post("/api/clock/lecture", json={"lecture": 9})).json() == {
                "teaching_date": "2026-09-11",
                "lecture": 9,
            }
            for path, body in [("advance", {"days": 0}), ("lecture", {"lecture": 13})]:
                assert (await c.post(f"/api/clock/{path}", json=body)).status_code == 422
            # 任意大正整数应返回可理解的边界错误，不应导致 500。
            response = await c.post("/api/clock/advance", json={"days": 10**20})
            assert response.status_code == 409
            assert "超出支持范围" in response.json()["detail"]
            assert app.state.orchestrator.teaching_date() == date(2026, 9, 11)
        with Session(business) as session:
            overdue = Task(
                type=TaskType.RETEST,
                student_id="A",
                knowledge_point_id="old",
                created_on=TODAY,
                due_date=TODAY,
            )
            session.add(overdue)
            session.commit()
            task_id = overdue.id
        async with app.router.lifespan_context(app):
            assert app.state.clock.state().lecture == 9
            assert app.state.clock.current_date() == date(2026, 9, 11)
            with Session(business) as session:
                assert session.get(DueNotification, task_id) is not None
        monkeypatch.setenv("TEACHING_CLOCK_MODE", "real")
        get_settings.cache_clear()
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c,
        ):
            assert (await c.post("/api/clock/advance", json={})).status_code == 409
    finally:
        get_settings.cache_clear()


async def test_real_chapter_1_2_9_content_roster(business):
    root = Path(__file__).resolve().parents[2]
    content = load(sorted((root / "content/lectures").glob("*.yaml")))
    with Session(business) as session:
        ingest(session, content)
        session.add_all([Student(id=s, name=s) for s in ("B", "safe", "missing")])
        session.flush()
        knowledge = NxKnowledgeGraph(
            list(content.points.values()), list(content.misconceptions.values()), [], []
        )
        assert [
            p.lecture for p in knowledge.path("calc.ch1.composition", "calc.ch9.multivar_chain")
        ] == [1, 2, 9]
        # 其他先修都有巩固证据，隔离链式法则这一条路径的名单。
        prerequisite_ids = {
            p.id
            for target in knowledge.lecture_points(9)
            for p in knowledge.prerequisites(target.id)
        }
        for student in ("A", "B", "safe", "missing"):
            for kp in prerequisite_ids:
                if student == "missing" and kp == "calc.ch2.chain_rule":
                    continue
                session.add(
                    Mastery(
                        student_id=student,
                        knowledge_point_id=kp,
                        state=MasteryState.CONSOLIDATED,
                        updated_on=TODAY,
                    )
                )
        add_history(session, "A", "calc.ch2.chain_rule_multi", "calc.ch2.chain.miss_inner")
        add_history(session, "B", "calc.ch2.chain_rule_multi", "calc.ch2.chain.formula_sub")
        session.commit()
    clock = TeachingClock(business, knowledge, Recorder())
    await clock.enter_lecture(9)
    with Session(business) as session:
        contexts = session.exec(select(PrecheckContext)).all()
        assert {
            (p.student_id, p.knowledge_point_id, p.context_knowledge_point_id) for p in contexts
        } == {(s, "calc.ch2.chain_rule_multi", "calc.ch9.multivar_chain") for s in ("A", "B")} | {
            ("missing", "calc.ch2.chain_rule", "calc.ch9.multivar_chain")
        }
        assert session.get(Task, contexts[0].task_id).misconception_ids
