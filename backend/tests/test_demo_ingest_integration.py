"""T20 inputs pass through the real T17 importer and T19 evidence ledger."""

import sys
from datetime import date
from pathlib import Path

import pytest
from sqlmodel import Session, select

from app.contracts.entities import Event, Evidence, Mastery, Student
from app.contracts.enums import EventType, JudgedBy, MasteryState
from app.db import init_db, make_engine
from app.ingest import HomeworkAnalysis, HomeworkImporter
from app.knowledge.ingest import ingest


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_forty_demo_homeworks_produce_real_import_signals(tmp_path):
    sys.path.insert(0, str(Path(__file__).parents[2]))
    from demo.dataset import CHAIN, load_dataset

    plan = load_dataset()

    class AnalysisModel:
        async def generate(self, messages, output, tools=None):
            assert output is HomeworkAnalysis
            # Deliberately claims every answer is wrong: real math must override.
            return HomeworkAnalysis(
                knowledge_point_id=CHAIN,
                is_correct=False,
                misconception_id=None,
                reason="确定性测试分析替身",
            )

    engine = make_engine(f"sqlite:///{tmp_path / 'classroom.db'}")
    init_db(engine)
    try:
        with Session(engine) as session:
            ingest(session, plan.content)
            session.commit()
        result = await HomeworkImporter(engine, AnalysisModel()).import_csv(
            (Path(__file__).parents[2] / "demo/data/homework_ch2.csv").read_bytes(),
            date(2026, 9, 7),
        )
        assert result.students == result.evidence == 40 and result.items == 1
        with Session(engine) as session:
            rows = session.exec(select(Evidence)).all()
            assert len(session.exec(select(Student)).all()) == 40
            assert sum(row.is_correct is False for row in rows) == 12
            assert sum(row.is_correct is True for row in rows) == 28
            assert all(row.judged_by == JudgedBy.TOOL for row in rows)
            states = session.exec(select(Mastery)).all()
            assert len(states) == 12
            assert all(state.state == MasteryState.NEEDS_INTERVENTION for state in states)
            assert session.get(Event, result.event_id).type == EventType.HOMEWORK_IMPORTED
    finally:
        engine.dispose()
