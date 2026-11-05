from datetime import date

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.contracts.agents import VerifierInput
from app.contracts.entities import Evidence, Item, KnowledgePoint, Student
from app.contracts.enums import (
    EquivalenceMode,
    EvidenceKind,
    ExamTag,
    ItemPurpose,
    ItemSource,
    JudgedBy,
    Operation,
)
from app.contracts.events import AnswerSubmitted, make_event, read_payload
from app.contracts.values import Bound, ItemSpec, Prediction
from app.db import init_db, make_engine

TODAY = date(2026, 9, 7)


@pytest.fixture
def session():
    engine = make_engine("sqlite://")
    init_db(engine)
    with Session(engine) as s:
        yield s


def test_json_columns_restore_types(session: Session) -> None:
    spec = ItemSpec(operation=Operation.DERIVATIVE, expr="sin(3*x)**2", variables=["x"])
    session.add(Student(id="A", name="学生 A"))
    session.add(KnowledgePoint(id="kp", lecture=2, section=2, name="链式法则", exam_tags=["math1"]))
    session.flush()
    session.add(
        Item(
            id="i1",
            knowledge_point_id="kp",
            purpose=ItemPurpose.HOMEWORK,
            stem="求 $\\sin^2(3x)$ 的导数",
            spec=spec,
            answer="6\\sin(3x)\\cos(3x)",
            predictions=[{"misconception_id": "m", "answer": "2\\sin(3x)\\cos(3x)"}],
            source=ItemSource.HOMEWORK,
        )
    )
    session.commit()
    session.expire_all()

    item = session.get(Item, "i1")
    assert item.spec == spec
    assert item.predictions == [Prediction(misconception_id="m", answer="2\\sin(3x)\\cos(3x)")]
    assert session.get(KnowledgePoint, "kp").exam_tags == [ExamTag.MATH1]


def test_foreign_keys_enforced(session: Session) -> None:
    session.add(
        Evidence(
            student_id="nobody",
            knowledge_point_id="kp",
            kind=EvidenceKind.HOMEWORK,
            judged_by=JudgedBy.TOOL,
            teaching_date=TODAY,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_item_spec_shape() -> None:
    disk = ItemSpec(
        operation=Operation.DOUBLE_INTEGRAL,
        expr="x**2 + y**2",
        bounds=[
            Bound(var="y", lower="-sqrt(1 - x**2)", upper="sqrt(1 - x**2)"),
            Bound(var="x", lower="-1", upper="1"),
        ],
    )
    assert disk.answer_mode == EquivalenceMode.VALUE
    with pytest.raises(ValidationError):
        ItemSpec(operation=Operation.DOUBLE_INTEGRAL, expr="x", bounds=disk.bounds[:1])
    with pytest.raises(ValidationError):
        ItemSpec(operation=Operation.DERIVATIVE, expr="x**2")


def test_verifier_input_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        VerifierInput(
            knowledge_point_id="kp",
            dimensions=[],
            confirmed_misconceptions=[],
            used_fingerprints=[],
            dialogue=["辅导对话"],
        )


def test_event_payload_roundtrip(session: Session) -> None:
    event = make_event(AnswerSubmitted(task_id=1, evidence_id=2), TODAY)
    session.add(event)
    session.commit()
    stored = session.exec(select(type(event))).one()
    assert read_payload(stored) == AnswerSubmitted(task_id=1, evidence_id=2)
