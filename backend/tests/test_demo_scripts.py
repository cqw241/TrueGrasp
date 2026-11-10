"""T14 exercises the real student API and restores a persisted LangGraph wait."""

import importlib
import json
import sys
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.agents.tutor import create_tutor
from app.agents.tutor.models import CandidateSelection, QuestionDraft
from app.agents.verifier import create_verifier
from app.config import Settings
from app.contracts.entities import Evidence, Item, Student, Task
from app.db import make_engine
from app.knowledge.graph import NxKnowledgeGraph
from app.main import create_app
from tests.test_tutor import Model
from tests.test_tutor import setup as setup
from tests.test_tutor_integration import CombinedModel, Planner

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
dataset = importlib.import_module("demo.dataset")
online = importlib.import_module("demo.scripts.online")
snapshot = importlib.import_module("demo.scripts.snapshot")


def test_batch_submits_current_probe_and_stops_at_a_different_item(setup, tmp_path):
    engine, graph, source = setup
    with Session(engine) as session:
        session.add(Student(id="S05", name="脚本学生"))
        session.flush()
        error = session.get(Evidence, source.error_evidence_id)
        clone = Evidence.model_validate({**error.model_dump(exclude={"id"}), "student_id": "S05"})
        session.add(clone)
        session.flush()
        task = Task(
            type="tutoring",
            student_id="S05",
            knowledge_point_id=source.knowledge_point_id,
            evidence_ids=[clone.id],
            created_on=date(2026, 9, 7),
        )
        session.add(task)
        session.commit()
        task_id = task.id
    model = CombinedModel()
    tutor = create_tutor(llm=model, knowledge=graph, engine=engine)
    app = create_app(
        engine=engine,
        checkpoint_path=str(tmp_path / "checkpoint.db"),
        orchestration_dependencies={
            "planner": Planner(),
            "tutor": tutor,
            "verifier": create_verifier(llm=model, knowledge=graph),
        },
    )
    plan = dataset.load_dataset()
    report = []
    with TestClient(app) as client:
        api = online.DemoAPI(client, 20)
        with pytest.raises(online.DemoError, match="人工操作"):
            api.batch(plan, "diagnosis_ch2", ["A"], report.append)
        assert api.batch(plan, "diagnosis_ch2", ["S05"], report.append) == 0
        probes = client.get(f"/api/tasks/{task_id}/diagnosis").json()["probes"]
        assert [row["item_id"] for row in report] == [probe["item_id"] for probe in probes]
        assert [probe["student_answer"] for probe in probes] == [
            row.student_answer for row in plan.batch("diagnosis_ch2") if row.student_id == "S05"
        ]
        assert {row["task_id"] for row in report} == {task_id}
        assert api.batch(plan, "diagnosis_ch2", ["S05"], report.append) == 0
        assert [row["status"] for row in report[-2:]] == ["already_answered"] * 2
        api.advance(date(2026, 9, 8))
        assert api.batch(plan, "verification", ["S05"], report.append) == 1
        assert "实际题" in report[-1]["detail"]
        history = client.get(f"/api/tasks/{task_id}/messages").json()
        assert sum(row["role"] == "user" for row in history) == 2


def test_restore_same_waiting_diagnosis_twice(setup, tmp_path, monkeypatch):
    engine, _, _ = setup
    day = date(2026, 11, 2)
    monkeypatch.setattr("app.main.get_settings", lambda: Settings(teaching_clock_initial_date=day))
    point = "calc.ch10.polar"
    miss_r = f"{point}.miss_r"
    with Session(engine) as session:
        session.add(Student(id="D", name="现场学生"))
        session.flush()
        error = Evidence(
            student_id="D",
            knowledge_point_id=point,
            item_id=dataset.POLAR,
            kind="homework",
            student_answer=r"\frac{2\pi}{3}",
            is_correct=False,
            judged_by="tool",
            teaching_date=day,
        )
        session.add(error)
        session.flush()
        task = Task(
            type="tutoring",
            student_id="D",
            knowledge_point_id=point,
            evidence_ids=[error.id],
            created_on=day,
        )
        session.add(task)
        practice = session.get(Item, f"{point}.seed_quarter_disk")
        practice_draft = QuestionDraft(
            stem=practice.stem, spec=practice.spec, answer=practice.answer
        )
        session.commit()
        task_id = task.id

    class PolarModel(Model):
        async def generate(self, messages, output, tools=None):
            if output is CandidateSelection:
                return CandidateSelection(misconception_ids=[miss_r], reason="漏乘雅可比因子")
            if output is QuestionDraft:
                assert not json.loads(messages[-1].content)["active_ids"]
                return practice_draft
            return await super().generate(messages, output, tools)

    def app_for(db, checkpoints):
        model = PolarModel()
        graph = NxKnowledgeGraph.from_engine(db)
        return create_app(
            engine=db,
            checkpoint_path=str(checkpoints),
            orchestration_dependencies={
                "planner": Planner(),
                "tutor": create_tutor(llm=model, knowledge=graph, engine=db),
                "verifier": create_verifier(llm=model, knowledge=graph),
            },
        )

    route = f"/api/tasks/{task_id}"
    checkpoint_path = tmp_path / "checkpoint.db"
    with TestClient(app_for(engine, checkpoint_path)) as client:
        assert client.post(f"{route}/start").status_code == 202
        first_target = online.DemoAPI(client, 20).wait_target(task_id, turn=0)
        probe = client.get(f"{route}/diagnosis").json()["probes"][0]
        assert probe["item_id"] == first_target.item_id
        assert probe["student_answer"] is None
        assert {"misconception_id": miss_r, "answer": r"2\pi"} in probe["predictions"]

    saved = tmp_path / "saved"
    snapshot.save(tmp_path / "tutor.db", checkpoint_path, saved, revision="test")
    for take in (1, 2):
        destination = tmp_path / f"take-{take}"
        snapshot.restore(saved, destination)
        restored = make_engine(f"sqlite:///{destination / snapshot.BUSINESS}")
        try:
            with TestClient(app_for(restored, destination / snapshot.CHECKPOINTS)) as client:
                api = online.DemoAPI(client, 20)
                assert api.wait_target(task_id) == first_target
                assert (
                    client.get(f"{route}/diagnosis").json()["probes"][0]["student_answer"] is None
                )
                api.request(
                    "POST",
                    f"/tasks/{task_id}/messages",
                    params={"expected_turn": first_target.turn},
                    json={"content": r"2\pi", "answering_item_id": first_target.item_id},
                )
                api.wait_target(task_id, turn=first_target.turn)
                view = client.get(f"{route}/diagnosis").json()
                assert view["probes"][0]["student_answer"] == r"2\pi"
                assert [
                    h["misconception_id"] for h in view["hypotheses"] if h["status"] == "supported"
                ] == [miss_r]
        finally:
            restored.dispose()
