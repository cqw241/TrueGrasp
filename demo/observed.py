"""Consistency checks for a T14 export of real system records, never a seed writer."""

from collections import Counter, defaultdict
from datetime import date

from pydantic import Field, ValidationInfo, field_validator

from app.contracts.entities import Evidence, Item, Mastery, Student, Task
from app.contracts.enums import Dimension, EvidenceKind, ItemPurpose, MasteryState, TaskType
from app.verify.tools import SympyMathTools

from .dataset import (
    CHAIN,
    FORMULA,
    HOMEWORK,
    MISS,
    PROBE1,
    PROBE2,
    Dataset,
    Strict,
    _equivalent,
    _verify_item,
)


class RunExport(Strict):
    run_id: str = Field(min_length=1)
    checkpoint: str
    students: list[Student]
    evidence: list[Evidence]
    mastery: list[Mastery]
    tasks: list[Task]
    items: list[Item]  # includes real generated questions, not just C2 seed items
    precheck_task_ids: list[int]

    @field_validator("students", "evidence", "mastery", "tasks", "items", mode="before")
    @classmethod
    def validate_entity_rows(cls, value, info: ValidationInfo):
        # SQLModel table entities nested in BaseModel otherwise bypass their own
        # validation (e.g. JSON dates remain str). Validate each at this boundary.
        entities = {
            "students": Student,
            "evidence": Evidence,
            "mastery": Mastery,
            "tasks": Task,
            "items": Item,
        }
        entity = entities[info.field_name]
        if not isinstance(value, list):
            raise ValueError("entity rows must be an array")
        return [
            entity.model_validate(row.model_dump() if isinstance(row, entity) else row)
            for row in value
        ]


def validate_run(data: Dataset, actual: RunExport) -> dict:
    """Validate supplied records. This checks consistency, not export authenticity."""

    def require(ok, message):
        if not ok:
            raise ValueError(message)

    require(actual.checkpoint == "after_ch9_followup_before_ch10_live", "wrong checkpoint")
    roster = {s.id for s in data.scenario.students}
    require(
        len(actual.students) == len(roster) and {s.id for s in actual.students} == roster,
        "observed roster differs",
    )
    evidence = {e.id: e for e in actual.evidence}
    require(
        None not in evidence and len(evidence) == len(actual.evidence),
        "observed evidence needs unique persisted IDs",
    )
    require(
        all(e.teaching_date < date(2026, 11, 2) for e in evidence.values()),
        "checkpoint includes ch10 live or later evidence",
    )
    items = {i.id: i for i in actual.items}
    require(len(items) == len(actual.items), "duplicate observed items")
    seen = defaultdict(set)
    for e in sorted(evidence.values(), key=lambda row: (row.teaching_date, row.id)):
        require(e.student_id in roster, "unknown evidence student")
        require(
            e.item_id is not None
            or (e.kind != EvidenceKind.VERIFICATION and e.judged_by != "tool"),
            "critical evidence missing item reference",
        )
        if e.item_id is None:
            continue
        require(e.item_id in items, "observed item missing")
        item = items[e.item_id]
        require(e.knowledge_point_id == item.knowledge_point_id, "evidence/item point mismatch")
        if e.kind == EvidenceKind.VERIFICATION:
            require(
                item.purpose == ItemPurpose.VERIFICATION and item.dimension is not None,
                "verification item has wrong purpose",
            )
            require(e.dimension == item.dimension, "evidence/item dimension mismatch")
            fingerprint = (
                SympyMathTools().fingerprint(item.spec)
                if item.spec is not None
                else item.fingerprint
            )
            require(
                bool(fingerprint) and fingerprint not in seen[e.student_id],
                "reused or missing student verification fingerprint",
            )
        if item.spec is not None:
            seen[e.student_id].add(SympyMathTools().fingerprint(item.spec))
        elif item.fingerprint:
            seen[e.student_id].add(item.fingerprint)
        if e.judged_by == "tool":
            require(item.spec is not None, "tool evidence without spec")
            require(
                _verify_item(item.spec.model_dump_json(), item.answer).ok is True,
                "observed standard answer fails math check",
            )
            result = _equivalent(
                e.student_answer, item.answer, item.spec.answer_mode, tuple(item.spec.variables)
            )
            require(result is not None and result == e.is_correct, "observed tool verdict differs")
        if e.matched_misconception_id in (MISS, FORMULA):
            prediction = next(
                (p for p in e.predictions if p.misconception_id == e.matched_misconception_id), None
            )
            require(
                prediction is not None and item.spec is not None, "missing old cause prediction"
            )
            require(
                _equivalent(
                    e.student_answer,
                    prediction.answer,
                    item.spec.answer_mode,
                    tuple(item.spec.variables),
                )
                is True,
                "old cause prediction not hit",
            )
    for e in evidence.values():
        if e.kind == EvidenceKind.VERIFICATION:
            require(e.task_id is not None, "verification missing task reference")
    homework = [
        e for e in evidence.values() if e.item_id == HOMEWORK and e.kind == EvidenceKind.HOMEWORK
    ]
    require(
        Counter(e.student_id for e in homework) == Counter(roster),
        "observed homework must cover 40 exactly once",
    )
    require(sum(e.is_correct is False for e in homework) == 12, "observed homework errors != 12")
    # Reconstruct diagnoses from the intersection of actual C2 probe responses.
    # A single shared wrong prediction cannot confirm MISS rather than FORMULA.
    confirmed = {}
    for sid in {e.student_id for e in homework if e.is_correct is False}:
        candidates = {MISS, FORMULA, "slip"}
        probes = [
            e
            for e in evidence.values()
            if e.student_id == sid
            and e.kind == EvidenceKind.DIAGNOSIS
            and e.item_id in (PROBE1, PROBE2)
        ]
        for row in probes:
            probe = data.content.items[row.item_id]
            predictions = {p.misconception_id: p.answer for p in probe.predictions}
            predictions["slip"] = probe.answer
            candidates &= {
                cause
                for cause in candidates
                if _equivalent(
                    row.student_answer,
                    predictions[cause],
                    probe.spec.answer_mode,
                    tuple(probe.spec.variables),
                )
                is True
            }
        require(
            bool(probes) and len(candidates) == 1,
            f"missing or ambiguous diagnostic evidence: {sid}",
        )
        confirmed[sid] = candidates.pop()
    require(
        Counter(confirmed.values()) == Counter({MISS: 8, FORMULA: 3, "slip": 1})
        and all(
            confirmed.get(sid) == cause
            for sid, cause in (("A", MISS), ("B", FORMULA), ("C", "slip"))
        ),
        "observed probe-derived diagnoses differ",
    )
    tasks = {t.id: t for t in actual.tasks}
    require(None not in tasks and len(tasks) == len(actual.tasks), "invalid observed task IDs")
    for e in evidence.values():
        if e.task_id is not None:
            require(
                e.task_id in tasks
                and tasks[e.task_id].student_id == e.student_id
                and tasks[e.task_id].knowledge_point_id == e.knowledge_point_id
                and tasks[e.task_id].created_on <= e.teaching_date,
                "evidence task reference differs",
            )
    require(
        len(set(actual.precheck_task_ids)) == len(actual.precheck_task_ids) > 0,
        "missing/duplicate precheck task IDs",
    )
    for task_id in actual.precheck_task_ids:
        require(
            task_id in tasks and tasks[task_id].type == TaskType.PRECHECK,
            "non-precheck task in precheck IDs",
        )
    precheck = [
        e
        for e in evidence.values()
        if e.task_id in actual.precheck_task_ids
        and e.kind == EvidenceKind.VERIFICATION
        and e.dimension == Dimension.CROSS_CHAPTER
    ]
    relapsed = {
        e.student_id
        for e in precheck
        if e.is_correct is False and e.matched_misconception_id in (MISS, FORMULA)
    }
    require(relapsed == set(data.scenario.relapse_students), "observed ch9 relapses differ")
    require(
        any(e.student_id == "B" and e.is_correct is True for e in precheck),
        "B must pass ch9 precheck",
    )
    require(
        any(
            e.student_id == "B"
            and e.dimension == Dimension.RETENTION
            and e.teaching_date == date(2026, 9, 11)
            and e.is_correct is False
            and e.matched_misconception_id == FORMULA
            for e in evidence.values()
        ),
        "missing B Friday recurrence",
    )
    require(
        any(
            e.student_id == "B"
            and e.knowledge_point_id == "calc.ch1.composition"
            and e.teaching_date > date(2026, 9, 11)
            for e in evidence.values()
        ),
        "missing B first-lecture backtrack",
    )
    friday = [
        e
        for e in evidence.values()
        if e.kind == EvidenceKind.VERIFICATION
        and e.knowledge_point_id == CHAIN
        and e.dimension == Dimension.RETENTION
        and e.teaching_date == date(2026, 9, 11)
    ]
    require(
        Counter(e.student_id for e in friday) == Counter(roster),
        "Friday retention must cover all 40 students",
    )
    followup_tasks = [
        t
        for t in tasks.values()
        if t.knowledge_point_id == CHAIN and t.type in (TaskType.RETEST, TaskType.PRECHECK)
    ]
    require(
        {t.id for t in followup_tasks if t.type == TaskType.PRECHECK}
        == set(actual.precheck_task_ids),
        "precheck task list incomplete",
    )
    for task in followup_tasks:
        require(
            any(
                e.task_id == task.id and e.kind == EvidenceKind.VERIFICATION
                for e in evidence.values()
            ),
            "followup task has no actual answer",
        )
    for sid, probe_ids, cause in (("B", (PROBE1,), FORMULA), ("C", (PROBE1, PROBE2), "slip")):
        for probe_id in probe_ids:
            probe = data.content.items[probe_id]
            target = (
                probe.answer
                if cause == "slip"
                else next(p.answer for p in probe.predictions if p.misconception_id == cause)
            )
            require(
                any(
                    e.student_id == sid
                    and e.item_id == probe_id
                    and e.kind == EvidenceKind.DIAGNOSIS
                    and _equivalent(
                        e.student_answer,
                        target,
                        probe.spec.answer_mode,
                        tuple(probe.spec.variables),
                    )
                    is True
                    for e in evidence.values()
                ),
                f"missing {sid} diagnostic probe evidence",
            )
    states = [m for m in actual.mastery if m.knowledge_point_id == CHAIN]
    require(Counter(m.student_id for m in states) == Counter(roster), "missing goal mastery rows")
    mastered = {MasteryState.INITIAL, MasteryState.CONSOLIDATED, MasteryState.INTEGRATED}
    reached, escalated = set(), set()
    for m in states:
        require(bool(m.evidence_ids), "mastery without evidence")
        require(
            all(
                i in evidence
                and evidence[i].student_id == m.student_id
                and evidence[i].knowledge_point_id == CHAIN
                for i in m.evidence_ids
            ),
            "mastery evidence reference differs",
        )
        history = [
            e
            for e in evidence.values()
            if e.student_id == m.student_id and e.knowledge_point_id == CHAIN
        ]
        linked = [evidence[i] for i in m.evidence_ids]
        if m.state in mastered:
            # Match T19: invalidating errors reset accumulation; dimensions are
            # latest per same student/point/task, never pooled across tasks.
            failures = [
                e
                for e in history
                if e.is_correct is False
                and e.kind != EvidenceKind.TEACHER
                and (
                    e.matched_misconception_id not in (None, "slip")
                    or e.kind == EvidenceKind.HOMEWORK
                    or e.dimension == Dimension.CROSS_CHAPTER
                    or (e.task_id in tasks and tasks[e.task_id].type == TaskType.PRECHECK)
                )
            ]
            cutoff = max(((e.teaching_date, e.id) for e in failures), default=(date.min, 0))
            qualified = False
            for task_id in {e.task_id for e in linked if e.task_id is not None}:
                rows = sorted(
                    (
                        e
                        for e in history
                        if e.task_id == task_id
                        and e.kind == EvidenceKind.VERIFICATION
                        and cutoff < (e.teaching_date, e.id)
                        and e.teaching_date <= m.updated_on
                    ),
                    key=lambda e: (e.teaching_date, e.id),
                )
                latest = {e.dimension: e for e in rows}
                passed = {
                    dim
                    for dim, e in latest.items()
                    if e.is_correct is True and e.id in m.evidence_ids
                }
                if {Dimension.CORRECTNESS, Dimension.DISTRACTOR} <= passed and passed & {
                    Dimension.NEAR_TRANSFER,
                    Dimension.EXPLANATION,
                }:
                    qualified = True
            require(qualified, "goal reached without linked same-task current dimensions")
            reached.add(m.student_id)
        elif m.state == MasteryState.ESCALATED:
            trigger = [
                e
                for e in history
                if e.is_correct is False
                and (
                    (
                        m.student_id == "B"
                        and e.dimension == Dimension.RETENTION
                        and e.teaching_date == date(2026, 9, 11)
                    )
                    or (m.student_id != "B" and e in precheck)
                )
            ]
            require(bool(trigger), "escalation lacks recurrence trigger")
            trigger_order = max((e.teaching_date, e.id) for e in trigger)
            supported = False
            for handoff in actual.tasks:
                if (
                    handoff.student_id != m.student_id
                    or handoff.knowledge_point_id != CHAIN
                    or handoff.type != TaskType.ESCALATION
                    or handoff.round < 2
                    or not handoff.reason
                    or handoff.created_on > m.updated_on
                ):
                    continue
                if not handoff.evidence_ids or not all(
                    i in evidence
                    and evidence[i].student_id == m.student_id
                    and evidence[i].knowledge_point_id == CHAIN
                    for i in handoff.evidence_ids
                ):
                    continue
                failed = sorted(
                    (
                        e
                        for e in linked
                        if e.id in handoff.evidence_ids
                        and e.kind == EvidenceKind.VERIFICATION
                        and e.is_correct is False
                        and e.matched_misconception_id in (MISS, FORMULA)
                        and trigger_order < (e.teaching_date, e.id)
                        and e.teaching_date <= handoff.created_on
                        and e.task_id in tasks
                        and tasks[e.task_id].type == TaskType.VERIFICATION
                        and tasks[e.task_id].knowledge_point_id == CHAIN
                        and tasks[e.task_id].created_on <= e.teaching_date
                    ),
                    key=lambda e: (e.teaching_date, e.id),
                )
                for first in failed:
                    for second in failed:
                        first_task, second_task = tasks[first.task_id], tasks[second.task_id]
                        if (
                            first.task_id != second.task_id
                            and (first.teaching_date, first.id) < (second.teaching_date, second.id)
                            and 1 <= first_task.round < second_task.round <= handoff.round
                            and second_task.round >= 2
                            and first_task.created_on <= second_task.created_on
                        ):
                            supported = True
            require(supported, "escalation lacks linked chronological two-round failures")
            escalated.add(m.student_id)
    require(
        len(reached) == 37 and escalated == set(data.scenario.escalation_students),
        "observed goal outcome must be 37 reached / 3 escalated",
    )
    return {
        "status": "supplied_run_consistent",
        "run_id": actual.run_id,
        "goal_met": len(reached),
        "escalated": len(escalated),
        "ch9_relapses": len(relapsed),
    }
