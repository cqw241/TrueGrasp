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
    from app.ingest.service import parse_csv

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


@pytest.fixture
def actual_run(plan):
    """In-memory unit fixture only, deliberately never exported as a real run."""
    from app.contracts.entities import Evidence, Mastery, Student, Task
    from app.contracts.enums import EvidenceKind, TaskType

    evidence, tasks, precheck_ids = [], [], []
    task_groups = {}
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
        group = (a.stage, a.student_id, a.round)
        if group not in task_groups:
            task_id = len(tasks) + 1
            task_groups[group] = task_id
            task_type = (
                TaskType.PRECHECK
                if a.stage == "precheck_ch9"
                else TaskType.RETEST
                if a.stage == "retest_friday"
                else TaskType.TUTORING
                if kind == EvidenceKind.DIAGNOSIS
                else TaskType.VERIFICATION
            )
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
        task_id = task_groups[group]
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
                    round=2,
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
    return actual


def test_observed_export_consistency_and_forged_verdict_rejection(plan, actual_run):
    assert observed.validate_run(plan, actual_run) == {
        "status": "supplied_run_consistent",
        "run_id": "unit-test-only-not-prerun",
        "goal_met": 37,
        "escalated": 3,
        "ch9_relapses": 2,
    }
    actual_run.evidence[0].is_correct = not actual_run.evidence[0].is_correct
    with pytest.raises(ValueError, match="tool verdict differs"):
        observed.validate_run(plan, actual_run)


@pytest.mark.parametrize(
    "mutation,match",
    [
        ("remove_failures", "two-round failures"),
        ("handoff_old_homework", "two-round failures"),
        ("handoff_round_one", "two-round failures"),
        ("failure_same_round", "two-round failures"),
        ("handoff_before_failure", "two-round failures"),
        ("failure_before_recurrence", "two-round failures"),
        ("repeated_correctness", "dimension mismatch"),
        ("alias_fingerprint", "verification fingerprint"),
        ("practice_as_verification", "wrong purpose"),
        ("no_item", "missing item reference"),
        ("missing_retention", "Friday retention"),
        ("missing_precheck_answers", "no actual answer"),
        ("omit_precheck_ids", "task list incomplete"),
        ("missing_b_diagnosis", "diagnostic evidence"),
        ("missing_c_diagnosis", "diagnostic evidence"),
        ("ambiguous_a_diagnosis", "diagnostic evidence"),
        ("mastery_homework_only", "linked same-task current dimensions"),
        ("split_assessment_tasks", "linked same-task current dimensions"),
        ("reuse_after_recurrence", "linked same-task current dimensions"),
    ],
)
def test_observed_rejects_review_regressions(plan, actual_run, mutation, match):
    from app.contracts.enums import Dimension, EvidenceKind

    actual = actual_run
    target = next(m for m in actual.mastery if m.student_id == "D")
    checks = [
        e
        for e in actual.evidence
        if e.student_id == "D"
        and e.kind == EvidenceKind.VERIFICATION
        and e.dimension in (Dimension.CORRECTNESS, Dimension.DISTRACTOR, Dimension.NEAR_TRANSFER)
    ]
    if mutation == "remove_failures":
        removed = {
            e.id
            for e in actual.evidence
            if e.is_correct is False and e.dimension == Dimension.DISTRACTOR
        }
        actual.evidence = [e for e in actual.evidence if e.id not in removed]
        # Keep references structurally valid; old homework still cannot support handoff.
        for t in actual.tasks:
            t.evidence_ids = [i for i in t.evidence_ids if i not in removed]
        for m in actual.mastery:
            m.evidence_ids = [i for i in m.evidence_ids if i not in removed]
    elif mutation == "handoff_old_homework":
        for t in actual.tasks:
            if t.type == "escalation":
                t.evidence_ids = [
                    e.id
                    for e in actual.evidence
                    if e.student_id == t.student_id and e.kind == EvidenceKind.HOMEWORK
                ]
    elif mutation == "handoff_round_one":
        for t in actual.tasks:
            if t.type == "escalation":
                t.round = 1
    elif mutation == "failure_same_round":
        for t in actual.tasks:
            if t.student_id == "B" and t.round == 2 and t.type == "verification":
                t.round = 1
    elif mutation == "handoff_before_failure":
        for t in actual.tasks:
            if t.type == "escalation":
                t.created_on = dataset.date(2026, 9, 10)
    elif mutation == "failure_before_recurrence":
        for e in actual.evidence:
            if (
                e.student_id == "S05"
                and e.is_correct is False
                and e.dimension == Dimension.DISTRACTOR
            ):
                e.teaching_date = dataset.date(2026, 10, 25)
                next(t for t in actual.tasks if t.id == e.task_id).created_on = e.teaching_date
    elif mutation in ("repeated_correctness", "alias_fingerprint"):
        base = next(e for e in checks if e.dimension == Dimension.CORRECTNESS)
        for e in checks:
            if e is base:
                continue
            e.student_answer = base.student_answer
            if mutation == "repeated_correctness":
                e.item_id = base.item_id
            else:
                item = next(i for i in actual.items if i.id == base.item_id).model_copy(deep=True)
                item.id = f"alias.{e.id}"
                item.dimension = e.dimension
                actual.items.append(item)
                e.item_id = item.id
    elif mutation == "practice_as_verification":
        next(i for i in actual.items if i.id == checks[0].item_id).purpose = "practice"
    elif mutation == "no_item":
        for e in actual.evidence:
            if e.kind == EvidenceKind.VERIFICATION:
                e.item_id, e.student_answer = None, "0"
    elif mutation == "missing_retention":
        actual.evidence = [
            e for e in actual.evidence if e.dimension != Dimension.RETENTION or e.student_id == "B"
        ]
        ids = {e.id for e in actual.evidence}
        for m in actual.mastery:
            m.evidence_ids = [i for i in m.evidence_ids if i in ids]
        for t in actual.tasks:
            t.evidence_ids = [i for i in t.evidence_ids if i in ids]
    elif mutation == "missing_precheck_answers":
        actual.evidence = [
            e
            for e in actual.evidence
            if e.dimension != Dimension.CROSS_CHAPTER or e.student_id in ("B", "S05", "S06")
        ]
    elif mutation == "omit_precheck_ids":
        keep = {t.id for t in actual.tasks if t.student_id in ("B", "S05", "S06")}
        actual.precheck_task_ids = [i for i in actual.precheck_task_ids if i in keep]
    elif mutation in ("missing_b_diagnosis", "missing_c_diagnosis", "ambiguous_a_diagnosis"):
        sid = {
            "missing_b_diagnosis": "B",
            "missing_c_diagnosis": "C",
            "ambiguous_a_diagnosis": "A",
        }[mutation]
        actual.evidence = [
            e
            for e in actual.evidence
            if not (
                e.student_id == sid
                and e.kind == EvidenceKind.DIAGNOSIS
                and (sid != "A" or e.item_id == dataset.PROBE1)
            )
        ]
    elif mutation == "mastery_homework_only":
        for m in actual.mastery:
            m.evidence_ids = [
                e.id
                for e in actual.evidence
                if e.student_id == m.student_id and e.kind == EvidenceKind.HOMEWORK
            ]
    elif mutation == "split_assessment_tasks":
        for e in checks[1:]:
            task = next(t for t in actual.tasks if t.id == e.task_id).model_copy(deep=True)
            task.id = max(t.id for t in actual.tasks) + 1
            actual.tasks.append(task)
            e.task_id = task.id
    elif mutation == "reuse_after_recurrence":
        from app.contracts.entities import Evidence

        original = next(
            e for e in actual.evidence if e.student_id == "D" and e.kind == EvidenceKind.HOMEWORK
        )
        # A later homework failure invalidates old dimensions without reusing a
        # verification fingerprint or corrupting original homework counts.
        actual.evidence.append(
            Evidence(
                id=max(e.id for e in actual.evidence) + 1,
                student_id="D",
                knowledge_point_id=dataset.CHAIN,
                kind="homework",
                item_id=dataset.PROBE2,
                student_answer=dataset._prediction(plan.items[dataset.PROBE2], dataset.MISS),
                correct_answer=plan.items[dataset.PROBE2].answer,
                is_correct=False,
                judged_by="tool",
                matched_misconception_id=dataset.MISS,
                predictions=plan.items[dataset.PROBE2].predictions,
                teaching_date=dataset.date(2026, 10, 28),
            )
        )
        assert original.id in target.evidence_ids
    with pytest.raises(ValueError, match=match):
        observed.validate_run(plan, actual)


def test_actual_json_roundtrip_and_cli_file(plan, actual_run, tmp_path, capsys, monkeypatch):
    from datetime import date

    cli = importlib.import_module("demo.__main__")
    path = tmp_path / "unit-test-export-not-prerun.json"
    path.write_text(actual_run.model_dump_json(warnings=False), encoding="utf-8")
    loaded = observed.RunExport.model_validate_json(path.read_text(encoding="utf-8"))
    assert isinstance(loaded.evidence[0].teaching_date, date)
    assert isinstance(loaded.mastery[0].updated_on, date)
    assert isinstance(loaded.tasks[0].created_on, date)
    assert loaded.items[0].spec is None or isinstance(loaded.items[0].spec, dataset.ItemSpec)
    assert observed.validate_run(plan, loaded)["goal_met"] == 37
    monkeypatch.setattr(cli, "load_dataset", lambda *args: plan)
    # Exercise real check-run file parsing and validation, not a mocked verdict.
    assert cli.main(["check-run", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "supplied_run_consistent"
