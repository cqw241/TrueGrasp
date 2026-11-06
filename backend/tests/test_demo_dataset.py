"""T20 acceptance: fixture math, story counts, and T14 replay boundaries."""

import importlib
import json
import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
dataset = importlib.import_module("demo.dataset")
observed = importlib.import_module("demo.observed")


@pytest.fixture(scope="module")
def plan():
    return dataset.load_dataset()


def test_mathematical_plan_and_story_counts(plan):
    report = dataset.validate_dataset(plan)
    assert report == {
        "status": "input_plan_valid_not_prerun",
        "students": 40,
        "homework_errors": 12,
        "same_cause": 8,
        "individual_followup": 4,
        "ch9_relapses_expected": 2,
        "goal_met_expected": 37,
        "escalated_expected": 3,
        "answer_inputs": 270,
        "math_items_checked": 15,
        "goal_scope": dataset.CHAIN,
        "observed_results": None,
    }
    for stage in ("verification", "retest_friday", "precheck_ch9"):
        assert {a.student_id for a in plan.answers if a.stage == stage} == {
            s.id for s in plan.scenario.students
        }
    assert len(plan.batch("verification")) == 37 * 3
    assert len(plan.batch("retest_friday")) == 37
    assert len(plan.batch("precheck_ch9")) == 37


def test_abc_probes_and_d_live(plan):
    probes = {
        (a.student_id, a.item_id): a.student_answer
        for a in plan.answers
        if a.stage == "diagnosis_ch2"
    }
    assert probes["B", dataset.PROBE1] == r"\cos(3x)"
    assert ("B", dataset.PROBE2) not in probes
    assert probes["A", dataset.PROBE2] == r"-2\cos(5x)\sin(5x)"
    assert probes["C", dataset.PROBE2] == r"-10\cos(5x)\sin(5x)"
    assert plan.batch("homework_ch10_live", include_human=True) == []
    assert plan.batch("diagnosis_ch10_live", include_human=True) == []
    assert not {"A", "B", "C"} & {a.student_id for a in plan.batch("diagnosis_ch2")}
    d = [a for a in plan.answers if a.stage == "homework_ch10_live"]
    assert len(d) == 1 and d[0].student_id == "D"
    assert d[0].student_answer == r"\frac{2\pi}{3}"


def test_t14_uses_actual_waiting_id_and_rejects_other_question(plan):
    a = plan.batch("verification")[0]
    item = plan.items[a.item_id].model_copy(deep=True)
    item.id = "real.generated.item.123"
    assert plan.answer_for(a.key, item).model_dump() == {
        "content": a.student_answer,
        "answering_item_id": "real.generated.item.123",
    }
    item.spec.expr = "sin(31*x)**3"
    with pytest.raises(ValueError, match="operator input required"):
        plan.answer_for(a.key, item)


def test_export_is_inputs_without_ledger_records(plan):
    result = plan.export()
    assert result["status"] == "input_plan_not_prerun"
    assert not {"evidence", "mastery", "tasks", "observed_results"} & result.keys()
    assert result["expected_only"]["goal_met"] == 37
    assert result["goal"]["knowledge_point_ids"] == [dataset.CHAIN]
    assert result["goal"]["max_rounds"] == 2
    for stage in ("b_followup", "ch9_followup"):
        failures = [a for a in plan.answers if a.stage == stage]
        assert all(n == 2 for n in Counter(a.student_id for a in failures).values())
    assert all(anchor in plan.content.items for anchor in plan.anchors.values())
    assert all(i.startswith("demo.t20.") for i in plan.anchors)


@pytest.mark.parametrize(
    "mutation,match",
    [
        ("wrong_math", "invalid math"),
        ("missing_student", "40 unique"),
        ("duplicate_question", "reused student fingerprint"),
        ("only_one_round", "two rounds required"),
        ("third_relapse", "wrong ch9 relapse count"),
    ],
)
def test_acceptance_rejects_broken_plans(mutation, match):
    data = dataset.load_dataset()
    if mutation == "wrong_math":
        data.items[dataset.POLAR].answer = "0"
    elif mutation == "missing_student":
        data.scenario.students.pop()
    elif mutation == "duplicate_question":
        a = data.batch("verification")[0]
        data.answers.append(a.model_copy(update={"key": "different-key"}))
    elif mutation == "only_one_round":
        data.answers = [
            a
            for a in data.answers
            if not (a.stage == "ch9_followup" and a.student_id == "S05" and a.round == 2)
        ]
    else:
        a = next(a for a in data.answers if a.stage == "precheck_ch9" and a.student_id == "S07")
        a.student_answer = dataset._prediction(data.items[a.item_id], dataset.MISS)
        a.intended_cause = dataset.MISS
    with pytest.raises(ValueError, match=match):
        dataset.validate_dataset(data)


def test_cli_batches_and_csv_raw_inputs(plan, capsys, monkeypatch):
    cli = importlib.import_module("demo.__main__")
    monkeypatch.setattr(cli, "load_dataset", lambda *args: plan)
    monkeypatch.setattr(cli, "validate_dataset", lambda *args: {})
    assert cli.main(["batch", "precheck_ch9"]) == 0
    batch = json.loads(capsys.readouterr().out)
    assert len(batch) == 37
    assert set(batch[0]["message"]) == {"content", "answering_item_id"}
    assert cli.main(["homework-csv"]) == 0
    import csv
    import io

    rows = list(csv.DictReader(io.StringIO(capsys.readouterr().out)))
    assert len(rows) == 40
    # T17 HomeworkRow/parse_csv: required and optional columns are a closed set.
    assert set(rows[0]) == {
        "student_id",
        "item_id",
        "student_answer",
        "student_name",
        "stem",
        "correct_answer",
        "knowledge_point_id",
    }
    try:
        from app.ingest.service import parse_csv
    except ImportError:
        parse_csv = None  # T17 not yet merged into this isolated checkout
    if parse_csv is not None:
        encoded = io.StringIO()
        writer = csv.DictWriter(encoded, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
        assert len(parse_csv(encoded.getvalue().encode("utf-8"))) == 40
    assert not {"is_correct", "matched_misconception_id", "state"} & rows[0].keys()
    assert cli.main(["batch", "typo"]) == 1
    assert "unknown stage" in capsys.readouterr().err


def test_run_checker_rejects_plan_or_empty_records(plan):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        observed.RunExport.model_validate(plan.export())
    empty = observed.RunExport(
        run_id="test-only",
        checkpoint="after_ch9_followup_before_ch10_live",
        students=[],
        evidence=[],
        mastery=[],
        tasks=[],
        items=[],
        precheck_task_ids=[],
    )
    with pytest.raises(ValueError, match="observed roster differs"):
        observed.validate_run(plan, empty)


def test_missing_c2_reference_is_actionable(tmp_path):
    with pytest.raises(ValueError, match="内容目录"):
        dataset.load_dataset(content_dir=tmp_path)


def test_observed_export_consistency_and_forged_verdict_rejection(plan):
    """In-memory unit fixture only, deliberately never exported as a real run."""
    from app.contracts.entities import Evidence, Mastery, Student, Task
    from app.contracts.enums import EvidenceKind, TaskType

    evidence, tasks, precheck_ids = [], [], []
    for a in plan.answers:
        if a.operator == "live":
            continue
        item = plan.items[a.item_id]
        result = dataset._equivalent(
            a.student_answer, item.answer, item.spec.answer_mode, tuple(item.spec.variables)
        )
        kind = (
            EvidenceKind.HOMEWORK
            if a.stage == "homework_ch2"
            else EvidenceKind.DIAGNOSIS
            if a.stage in ("diagnosis_ch2", "backtrack_ch1")
            else EvidenceKind.VERIFICATION
        )
        task_id = len(tasks) + 1
        task_type = TaskType.PRECHECK if a.stage == "precheck_ch9" else TaskType.VERIFICATION
        tasks.append(
            Task(
                id=task_id,
                type=task_type,
                student_id=a.student_id,
                knowledge_point_id=item.knowledge_point_id,
                round=a.round,
                created_on=a.teaching_date,
            )
        )
        if task_type == TaskType.PRECHECK:
            precheck_ids.append(task_id)
        matched = a.intended_cause if result is False and a.intended_cause != "slip" else None
        evidence.append(
            Evidence(
                id=len(evidence) + 1,
                student_id=a.student_id,
                knowledge_point_id=item.knowledge_point_id,
                kind=kind,
                dimension=item.dimension,
                task_id=task_id,
                item_id=item.id,
                student_answer=a.student_answer,
                correct_answer=item.answer,
                predictions=item.predictions,
                is_correct=result,
                matched_misconception_id=matched,
                judged_by="tool",
                teaching_date=a.teaching_date,
            )
        )
    mastery = []
    for s in plan.scenario.students:
        ids = [
            e.id for e in evidence if e.student_id == s.id and e.knowledge_point_id == dataset.CHAIN
        ]
        escalated = s.id in plan.scenario.escalation_students
        mastery.append(
            Mastery(
                student_id=s.id,
                knowledge_point_id=dataset.CHAIN,
                state="escalated" if escalated else "initial",
                evidence_ids=ids,
                updated_on="2026-10-28",
            )
        )
        if escalated:
            tasks.append(
                Task(
                    id=len(tasks) + 1,
                    type="escalation",
                    student_id=s.id,
                    knowledge_point_id=dataset.CHAIN,
                    evidence_ids=ids,
                    reason="Unit-test fixture: two failed followups",
                    created_on="2026-10-28",
                )
            )
    actual = observed.RunExport(
        run_id="unit-test-only-not-prerun",
        checkpoint="after_ch9_followup_before_ch10_live",
        students=[Student(id=s.id, name=s.name) for s in plan.scenario.students],
        evidence=evidence,
        mastery=mastery,
        tasks=tasks,
        items=list(plan.items.values()),
        precheck_task_ids=precheck_ids,
    )
    assert observed.validate_run(plan, actual) == {
        "status": "supplied_run_consistent",
        "run_id": "unit-test-only-not-prerun",
        "goal_met": 37,
        "escalated": 3,
        "ch9_relapses": 2,
    }
    evidence[0].is_correct = not evidence[0].is_correct
    with pytest.raises(ValueError, match="tool verdict differs"):
        observed.validate_run(plan, actual)
