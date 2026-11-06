import asyncio
import csv
import io
import json
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.contracts.entities import (
    Event,
    Evidence,
    Item,
    KnowledgePoint,
    Mastery,
    Misconception,
    Student,
)
from app.contracts.enums import EvidenceKind, ItemPurpose, ItemSource, JudgedBy, MasteryState
from app.contracts.events import HomeworkImported, read_payload
from app.contracts.values import ItemSpec
from app.db import init_db, make_engine
from app.ingest import (
    ConfirmationRequired,
    HomeworkAnalysis,
    HomeworkImporter,
    HomeworkRow,
    ImageHomework,
    IngestError,
    parse_csv,
)
from app.ingest.__main__ import image_batch, main
from app.llm.client import LLMOutputError
from app.llm.vision import Transcription
from app.main import create_app
from app.orchestration.storage import PendingWork

TODAY = date(2026, 9, 7)


class Model:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    async def generate(self, messages, output, tools=None):
        self.calls.append((messages, output))
        if self.responses:
            result = self.responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return output.model_validate(result)
        assert output is HomeworkAnalysis
        return HomeworkAnalysis(
            knowledge_point_id="kp",
            is_correct=False,
            misconception_id="miss",
            reason="可能漏乘内层导数",
        )


@pytest.fixture
def engine(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'business.db'}")
    init_db(engine)
    with Session(engine) as session:
        session.add(KnowledgePoint(id="kp", lecture=2, section=1, name="链式法则"))
        session.add(KnowledgePoint(id="other", lecture=1, section=1, name="函数"))
        session.flush()
        session.add(
            Misconception(
                id="miss", knowledge_point_id="kp", family="chain", description="漏乘内层导数"
            )
        )
        session.commit()
    yield engine
    engine.dispose()


def row(student="S01", **overrides):
    values = dict(
        student_id=student,
        student_name=f"学生{student}",
        item_id="hw1",
        stem="求 sin(3x) 导数",
        correct_answer=r"3\cos(3x)",
        student_answer=r"\cos(3x)",
    )
    values.update(overrides)
    return HomeworkRow(**values)


def csv_bytes(rows):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(HomeworkRow.model_fields))
    writer.writeheader()
    writer.writerows(entry.model_dump() for entry in rows)
    return output.getvalue().encode("utf-8-sig")


def run_import(engine, rows, model=None, **kwargs):
    return asyncio.run(
        HomeworkImporter(engine, model or Model(), **kwargs).import_rows(rows, TODAY)
    )


def assert_empty(engine):
    with Session(engine) as session:
        for entity in (Student, Item, Evidence, Event, Mastery, PendingWork):
            assert not session.exec(select(entity)).all(), entity


def test_csv_forty_students_event_and_ledger(engine):
    model = Model()
    result = asyncio.run(
        HomeworkImporter(engine, model).import_csv(
            csv_bytes([row(f"S{i:02}") for i in range(40)]), TODAY
        )
    )
    assert result.students == 40 and result.items == 1 and result.evidence == 40
    with Session(engine) as session:
        evidence = session.exec(select(Evidence).order_by(Evidence.id)).all()
        assert len(evidence) == 40
        assert all(entry.kind == EvidenceKind.HOMEWORK for entry in evidence)
        assert all(entry.matched_misconception_id == "miss" for entry in evidence)
        assert all(entry.judged_by == JudgedBy.MODEL for entry in evidence)
        states = session.exec(select(Mastery)).all()
        assert len(states) == 40
        assert all(entry.state == MasteryState.NEEDS_INTERVENTION for entry in states)
        event = session.get(Event, result.event_id)
        payload = read_payload(event)
        assert isinstance(payload, HomeworkImported)
        assert payload.knowledge_point_ids == ["kp"]
        assert payload.evidence_ids == [entry.id for entry in evidence]
        assert event.teaching_date == TODAY and not event.processed
        assert session.get(PendingWork, f"event:{event.id}").pending
        item = session.get(Item, "hw1")
        assert item.source == ItemSource.HOMEWORK
        assert item.purpose == ItemPurpose.HOMEWORK


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"student_id,item_id,student_answer\n",
        b"\xff",
        b"student_id,item_id,item_id,student_answer\n",
        b"student_id,item_id,student_answer,extra\ns,i,a,b\n",
        b"student_id,item_id,student_answer\ns,i\n",
        b"student_id,item_id,student_answer\ns,i,a,extra\n",
        b"student_id,item_id,student_answer\ns,i,   \n",
        b'student_id,item_id,student_answer\ns,i,"unterminated',
    ],
)
def test_malformed_csv(data):
    with pytest.raises(IngestError):
        parse_csv(data)


def test_csv_preserves_latex_commas_newlines():
    entry = row(stem="题干, 含逗号\n第二行", student_answer=r"\frac{1}{2}")
    assert parse_csv(csv_bytes([entry])) == [entry]


def test_duplicates_conflicting_items_and_students_are_atomic(engine):
    with pytest.raises(IngestError, match="重复"):
        run_import(engine, [row(), row()])
    with pytest.raises(IngestError, match="不一致"):
        run_import(engine, [row(), row("S02", correct_answer="other")])
    with pytest.raises(IngestError, match="姓名"):
        run_import(engine, [row(), row(student_name="new name", item_id="hw2")])
    assert_empty(engine)


@pytest.mark.parametrize(
    "overrides",
    [
        {"knowledge_point_id": "invented"},
        {"student_name": None},
        {"stem": None},
        {"correct_answer": None},
    ],
)
def test_invalid_references(engine, overrides):
    with pytest.raises(IngestError):
        run_import(engine, [row(**overrides)])
    assert_empty(engine)


@pytest.mark.parametrize(
    "analysis",
    [
        dict(knowledge_point_id="unknown", is_correct=False, misconception_id=None, reason="x"),
        dict(knowledge_point_id="kp", is_correct=False, misconception_id="unknown", reason="x"),
        dict(knowledge_point_id="other", is_correct=False, misconception_id="miss", reason="x"),
    ],
)
def test_model_ids_are_validated(engine, analysis):
    with pytest.raises(IngestError):
        run_import(engine, [row()], Model([analysis]))
    assert_empty(engine)


def test_model_failure_after_first_row_leaves_no_writes(engine):
    analysis = dict(knowledge_point_id="kp", is_correct=False, misconception_id="miss", reason="x")
    with pytest.raises(LLMOutputError):
        run_import(engine, [row(), row("S02")], Model([analysis, LLMOutputError("failed")]))
    assert_empty(engine)


def test_ledger_failure_rolls_back_students_items_mastery_and_event(engine):
    from app.ledger.service import append_evidence

    def failing_writer(session, evidence):
        append_evidence(session, evidence)
        if evidence.student_id == "S02":
            raise RuntimeError("ledger failed")
        return evidence

    with pytest.raises(RuntimeError, match="ledger failed"):
        run_import(engine, [row(), row("S02")], evidence_writer=failing_writer)
    assert_empty(engine)


def test_event_failure_rolls_back_entire_batch(engine, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("event failed")

    monkeypatch.setattr("app.ingest.service.queue_work", fail)
    with pytest.raises(RuntimeError, match="event failed"):
        run_import(engine, [row()])
    assert_empty(engine)


def test_known_item_tool_overrides_model_and_existing_records_preserved(engine):
    with Session(engine) as session:
        session.add(Student(id="S01", name="学生S01"))
        session.add(
            Item(
                id="hw1",
                knowledge_point_id="kp",
                stem="x^2 求导",
                answer="2x",
                spec=ItemSpec(operation="derivative", expr="x**2", variables=["x"]),
                purpose=ItemPurpose.HOMEWORK,
                source=ItemSource.HOMEWORK,
            )
        )
        session.commit()
    entry = HomeworkRow(student_id="S01", item_id="hw1", student_answer="2x")
    run_import(engine, [entry])
    with Session(engine) as session:
        evidence = session.exec(select(Evidence)).one()
        assert evidence.is_correct is True and evidence.judged_by == JudgedBy.TOOL
        assert evidence.matched_misconception_id is None
        assert session.get(Item, "hw1").stem == "x^2 求导"
        assert session.get(Student, "S01").name == "学生S01"
        assert not session.exec(select(Mastery)).all()


@pytest.mark.parametrize("correct", [True, None])
def test_non_errors_do_not_claim_matched_cause(engine, correct):
    model = Model(
        [dict(knowledge_point_id="kp", is_correct=correct, misconception_id="miss", reason="x")]
    )
    run_import(engine, [row()], model)
    with Session(engine) as session:
        assert session.exec(select(Evidence)).one().matched_misconception_id is None
        assert not session.exec(select(Mastery)).all()


def image_entry(**overrides):
    values = row().model_dump(exclude={"student_answer"})
    values.update(images=["https://example.org/answer.png"], **overrides)
    return ImageHomework(**values)


def test_images_reuse_vision_and_produce_same_signals(engine):
    model = Model([dict(latex=r"\cos(3x)", doubts=[])])
    result = asyncio.run(HomeworkImporter(engine, model).import_images([image_entry()], TODAY))
    assert result.evidence == 1
    messages, output = model.calls[0]
    assert output is Transcription
    assert messages[0].content[1].image_url.url == "https://example.org/answer.png"
    with Session(engine) as session:
        assert session.exec(select(Mastery)).one().state == MasteryState.NEEDS_INTERVENTION


@pytest.mark.parametrize("latex,doubts", [("x", ["符号不清"]), (r"\frac{", [])])
def test_images_needing_confirmation_are_not_graded(engine, latex, doubts):
    model = Model([dict(latex=latex, doubts=doubts)])
    with pytest.raises(ConfirmationRequired) as error:
        asyncio.run(HomeworkImporter(engine, model).import_images([image_entry()], TODAY))
    assert error.value.drafts[0].requires_confirmation
    assert error.value.drafts[0].row.student_answer == latex
    assert len(model.calls) == 1
    assert_empty(engine)


def test_confirmed_images_skip_ocr(engine):
    model = Model()
    result = asyncio.run(
        HomeworkImporter(engine, model).import_images(
            [image_entry(confirmed_answer="经学生确认的文字解释")], TODAY
        )
    )
    assert result.evidence == 1
    assert len(model.calls) == 1 and model.calls[0][1] is HomeworkAnalysis


def test_image_manifest_resolves_relative_files(tmp_path):
    (tmp_path / "answer.png").write_bytes(b"test image bytes")
    manifest = tmp_path / "batch.json"
    entry = image_entry().model_dump()
    entry["images"] = ["answer.png"]
    manifest.write_text(json.dumps([entry]), encoding="utf-8")
    assert image_batch(manifest)[0].images[0].startswith("data:image/png;base64,")


def test_http_uses_app_engine_date_and_contract(engine, tmp_path):
    app = create_app(
        engine=engine,
        checkpoint_path=str(tmp_path / "checkpoint.db"),
        orchestration_dependencies={"teaching_date": lambda: TODAY},
    )
    app.state.homework_llm = Model()
    with TestClient(app) as client:
        response = client.post("/api/homework", files={"file": ("hw.csv", csv_bytes([row()]))})
        assert response.status_code == 200, response.text
        assert set(response.json()) == {"event_id", "students", "items", "evidence"}
        invalid = client.post("/api/homework", files={"file": ("bad.csv", b"invalid")})
        assert invalid.status_code == 422
    with Session(engine) as session:
        assert session.exec(select(Evidence)).one().teaching_date == TODAY


def test_http_model_failure_is_atomic(engine, tmp_path):
    app = create_app(engine=engine, checkpoint_path=str(tmp_path / "checkpoint.db"))
    app.state.homework_llm = Model([LLMOutputError("provider failure")])
    with TestClient(app) as client:
        response = client.post("/api/homework", files={"file": ("hw.csv", csv_bytes([row()]))})
        assert response.status_code == 502
    assert_empty(engine)


def test_cli_csv(engine, tmp_path, monkeypatch, capsys):
    class Client(Model):
        def __init__(self, settings):
            super().__init__()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    path = tmp_path / "hw.csv"
    path.write_bytes(csv_bytes([row()]))
    monkeypatch.setattr("app.ingest.__main__.get_engine", lambda: engine)
    monkeypatch.setattr("app.ingest.__main__.OpenAICompatClient", Client)
    assert main(["csv", str(path), "--teaching-date", TODAY.isoformat()]) == 0
    assert json.loads(capsys.readouterr().out)["evidence"] == 1


def test_reserved_slip_candidate_is_allowed(engine):
    model = Model(
        [
            dict(
                knowledge_point_id="kp",
                is_correct=False,
                misconception_id="slip",
                reason="偶发失误",
            )
        ]
    )
    run_import(engine, [row()], model)
    with Session(engine) as session:
        assert session.exec(select(Evidence)).one().matched_misconception_id == "slip"


def test_http_invalid_csv_does_not_create_llm(engine, tmp_path, monkeypatch):
    def unexpected(*args):
        raise AssertionError("invalid input must not construct a provider client")

    monkeypatch.setattr("app.api.teacher.OpenAICompatClient", unexpected)
    app = create_app(engine=engine, checkpoint_path=str(tmp_path / "checkpoint.db"))
    with TestClient(app) as client:
        result = client.post("/api/homework", files={"file": ("bad.csv", b"invalid")})
        assert result.status_code == 422
    assert_empty(engine)


def test_images_mixed_batch_is_atomic_and_returns_all_drafts(engine):
    model = Model(
        [
            dict(latex="x", doubts=[]),
            dict(latex="y", doubts=["不确定"]),
        ]
    )
    second = image_entry().model_copy(update={"student_id": "S02", "student_name": "学生S02"})
    with pytest.raises(ConfirmationRequired) as error:
        asyncio.run(HomeworkImporter(engine, model).import_images([image_entry(), second], TODAY))
    assert len(error.value.drafts) == 2
    assert [draft.requires_confirmation for draft in error.value.drafts] == [False, True]
    assert all(output is Transcription for _, output in model.calls)
    assert_empty(engine)


def test_cli_images_writes_review_without_importing(engine, tmp_path, monkeypatch, capsys):
    class Client(Model):
        def __init__(self, settings):
            super().__init__([dict(latex="x", doubts=["不确定"])])

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    manifest = tmp_path / "images.json"
    manifest.write_text(json.dumps([image_entry().model_dump()]), encoding="utf-8")
    review = tmp_path / "review.json"
    monkeypatch.setattr("app.ingest.__main__.get_engine", lambda: engine)
    monkeypatch.setattr("app.ingest.__main__.OpenAICompatClient", Client)
    assert (
        main(
            [
                "images",
                str(manifest),
                "--teaching-date",
                TODAY.isoformat(),
                "--review-output",
                str(review),
            ]
        )
        == 2
    )
    assert json.loads(review.read_text(encoding="utf-8"))[0]["requires_confirmation"]
    assert "confirmed_answer" in capsys.readouterr().err
    assert_empty(engine)


def analysis_response(**overrides):
    values = dict(
        knowledge_point_id="kp", is_correct=False, misconception_id="miss", reason="模型初评"
    )
    values.update(overrides)
    return values


def test_new_csv_expression_tool_overrides_wrong_model_without_spec(engine):
    model = Model([analysis_response()])
    entry = row(stem="化简 x+x", student_answer="x+x", correct_answer="2x")
    asyncio.run(HomeworkImporter(engine, model).import_csv(csv_bytes([entry]), TODAY))
    with Session(engine) as session:
        evidence = session.exec(select(Evidence)).one()
        assert evidence.is_correct is True and evidence.judged_by == JudgedBy.TOOL
        assert evidence.matched_misconception_id is None
        assert session.get(Item, "hw1").spec is None
        assert not session.exec(select(Mastery)).all()


def test_new_image_item_extracts_and_checks_spec_with_real_tool(engine):
    spec = dict(operation="derivative", expr="x**2", variables=["x"])
    model = Model([dict(latex="x+x", doubts=[]), analysis_response(spec=spec)])
    entry = image_entry(stem="求 x^2 的导数", correct_answer="2x")
    asyncio.run(HomeworkImporter(engine, model).import_images([entry], TODAY))
    with Session(engine) as session:
        evidence = session.exec(select(Evidence)).one()
        assert evidence.is_correct is True and evidence.judged_by == JudgedBy.TOOL
        assert session.get(Item, "hw1").spec == ItemSpec(**spec)


@pytest.mark.parametrize("answer,expected", [("x^2+C", True), ("x^2+7", True), ("x+C", False)])
def test_new_integral_uses_antiderivative_and_real_tool(engine, answer, expected):
    spec = dict(operation="indefinite_integral", expr="2*x", variables=["x"])
    model = Model([analysis_response(spec=spec, is_correct=not expected)])
    entry = row(stem=r"求不定积分 \int 2x dx", correct_answer="x^2", student_answer=answer)
    asyncio.run(HomeworkImporter(engine, model).import_csv(csv_bytes([entry]), TODAY))
    with Session(engine) as session:
        evidence = session.exec(select(Evidence)).one()
        assert evidence.is_correct is expected and evidence.judged_by == JudgedBy.TOOL
        assert session.get(Item, "hw1").spec.answer_mode.value == "antiderivative"


def test_new_integral_mode_only_ignores_constant(engine):
    model = Model([analysis_response(answer_mode="antiderivative", variables=["x"])])
    entry = row(stem="求 2x 的不定积分", correct_answer="x^2", student_answer="x^2+C")
    run_import(engine, [entry], model)
    with Session(engine) as session:
        evidence = session.exec(select(Evidence)).one()
        assert evidence.is_correct is True and evidence.judged_by == JudgedBy.TOOL


def test_unknown_mode_negative_expr_does_not_override_integral_model(engine):
    model = Model([analysis_response(is_correct=True, answer_mode=None)])
    entry = row(stem="求 2x 的不定积分", correct_answer="x^2", student_answer="x^2+C")
    run_import(engine, [entry], model)
    with Session(engine) as session:
        evidence = session.exec(select(Evidence)).one()
        assert evidence.is_correct is True and evidence.judged_by == JudgedBy.MODEL


def test_unverified_extracted_spec_cannot_supply_negative_tool_verdict(engine):
    # 模型错误地把题干抽取成对 x^3 求导，重算答案不匹配，不能保存此 spec。
    model = Model(
        [
            analysis_response(
                is_correct=True, spec=dict(operation="derivative", expr="x**3", variables=["x"])
            )
        ]
    )
    entry = row(stem="求 x^2 导数", correct_answer="2x", student_answer="3x")
    run_import(engine, [entry], model)
    with Session(engine) as session:
        evidence = session.exec(select(Evidence)).one()
        assert evidence.is_correct is True and evidence.judged_by == JudgedBy.MODEL
        assert session.get(Item, "hw1").spec is None


class ImportPlanner:
    def __init__(self):
        self.calls = []
        self.called = asyncio.Event()
        self.fail_event_id = None

    async def plan(self, inp):
        from app.contracts.agents import PlannedTask, PlannerOutput

        self.calls.append(inp.event_id)
        self.called.set()
        if inp.event_id == self.fail_event_id:
            raise RuntimeError("planner failed once")
        return PlannerOutput(
            tasks=[
                PlannedTask(
                    type="class_review",
                    student_id=None,
                    knowledge_point_id="kp",
                    reason="导入后集体讲解",
                )
            ]
        )


def test_idle_runtime_discovers_import_from_another_engine_and_closes_listener(engine, tmp_path):
    from app.contracts.entities import Task
    from app.orchestration import EventBus, Orchestrator

    async def scenario():
        planner = ImportPlanner()
        async with Orchestrator.open(
            engine,
            str(tmp_path / "listener.db"),
            EventBus(),
            planner=planner,
            pending_poll_interval=0.01,
        ) as runtime:
            # 持续监听不在 _jobs 中，空闲等待无需关闭监听。
            await asyncio.wait_for(runtime.wait_idle(), timeout=1)
            listener = runtime._pending_listener
            assert listener is not None and not listener.done()
            other_engine = make_engine(str(engine.url))
            try:
                result = await HomeworkImporter(other_engine, Model()).import_csv(
                    csv_bytes([row()]), TODAY
                )
            finally:
                other_engine.dispose()
            # 不调用 recover_pending / HTTP / wait_idle 来触发发现，只等规划自然被唤醒。
            await asyncio.wait_for(planner.called.wait(), timeout=3)
            await asyncio.wait_for(runtime.wait_idle(), timeout=3)
            assert planner.calls == [result.event_id]
            with Session(engine) as session:
                assert session.get(Event, result.event_id).processed
                assert not session.get(PendingWork, f"event:{result.event_id}").pending
                assert session.exec(select(Task)).one().reason == "导入后集体讲解"
        assert listener.done()

    asyncio.run(scenario())


def test_pending_listener_does_not_retry_failure_after_errors_consumed(engine, tmp_path):
    from app.orchestration import EventBus, Orchestrator

    async def scenario():
        planner = ImportPlanner()
        # 外部写入发生在已经启动且空闲的服务之后。
        async with Orchestrator.open(
            engine,
            str(tmp_path / "fail-listener.db"),
            EventBus(),
            planner=planner,
            pending_poll_interval=0.01,
        ) as runtime:
            first = await HomeworkImporter(engine, Model()).import_rows([row()], TODAY)
            planner.fail_event_id = first.event_id
            await asyncio.wait_for(planner.called.wait(), timeout=3)
            with pytest.raises(ExceptionGroup, match="编排后台执行失败"):
                await runtime.wait_idle()
            planner.called.clear()
            await asyncio.sleep(0.06)  # 经多个监听轮次，失败工作仍不得被重试。
            await asyncio.wait_for(runtime.wait_idle(), timeout=1)
            assert planner.calls == [first.event_id]
            second = await HomeworkImporter(engine, Model()).import_rows([row("S02")], TODAY)
            await asyncio.wait_for(planner.called.wait(), timeout=3)
            await asyncio.wait_for(runtime.wait_idle(), timeout=3)
            assert planner.calls == [first.event_id, second.event_id]
            with Session(engine) as session:
                assert session.get(PendingWork, f"event:{first.event_id}").pending
                assert not session.get(PendingWork, f"event:{second.event_id}").pending
            # 失败仍可由调用方显式重试；不把失败记录误当成已处理。
            planner.fail_event_id = None
            await runtime.handle_event(first.event_id)
            await runtime.wait_idle()
            assert planner.calls == [first.event_id, second.event_id, first.event_id]

    asyncio.run(scenario())


def test_pending_listener_can_be_disabled(engine, tmp_path):
    from app.orchestration import EventBus, Orchestrator

    async def scenario():
        planner = ImportPlanner()
        async with Orchestrator.open(
            engine,
            str(tmp_path / "disabled-listener.db"),
            EventBus(),
            planner=planner,
            pending_poll_interval=None,
        ) as runtime:
            result = await HomeworkImporter(engine, Model()).import_rows([row()], TODAY)
            assert runtime._pending_listener is None
            await asyncio.sleep(0.04)
            await asyncio.wait_for(runtime.wait_idle(), timeout=1)
            assert not planner.calls
            runtime.recover_pending()
            await asyncio.wait_for(runtime.wait_idle(), timeout=3)
            assert planner.calls == [result.event_id]

    asyncio.run(scenario())
