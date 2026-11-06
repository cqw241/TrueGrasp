"""Consistency checks for a T14 export of real system records, never a seed writer."""

from collections import Counter
from datetime import date

from pydantic import Field

from app.contracts.entities import Evidence, Item, Mastery, Student, Task
from app.contracts.enums import Dimension, EvidenceKind, MasteryState, TaskType

from .dataset import CHAIN, FORMULA, HOMEWORK, MISS, Dataset, Strict, _equivalent, _verify_item


class RunExport(Strict):
    run_id: str = Field(min_length=1)
    checkpoint: str
    students: list[Student]
    evidence: list[Evidence]
    mastery: list[Mastery]
    tasks: list[Task]
    items: list[Item]  # includes real generated questions, not just C2 seed items
    precheck_task_ids: list[int]


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
    for e in evidence.values():
        require(e.student_id in roster, "unknown evidence student")
        if e.item_id is None:
            continue
        require(e.item_id in items, "observed item missing")
        item = items[e.item_id]
        require(e.knowledge_point_id == item.knowledge_point_id, "evidence/item point mismatch")
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
    homework = [
        e for e in evidence.values() if e.item_id == HOMEWORK and e.kind == EvidenceKind.HOMEWORK
    ]
    require(
        Counter(e.student_id for e in homework) == Counter(roster),
        "observed homework must cover 40 exactly once",
    )
    require(sum(e.is_correct is False for e in homework) == 12, "observed homework errors != 12")
    # Same initial answer cannot distinguish A from B; count the eight confirmed
    # diagnoses using each student's later supporting diagnosis evidence.
    diagnosed_miss = {
        e.student_id
        for e in evidence.values()
        if e.kind == EvidenceKind.DIAGNOSIS
        and e.matched_misconception_id == MISS
        and e.is_correct is False
    }
    require(len(diagnosed_miss) == 8, "observed confirmed common cause != 8")
    tasks = {t.id: t for t in actual.tasks}
    require(None not in tasks and len(tasks) == len(actual.tasks), "invalid observed task IDs")
    for e in evidence.values():
        if e.task_id is not None:
            require(
                e.task_id in tasks and tasks[e.task_id].student_id == e.student_id,
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
    states = [m for m in actual.mastery if m.knowledge_point_id == CHAIN]
    require(Counter(m.student_id for m in states) == Counter(roster), "missing goal mastery rows")
    mastered = {MasteryState.INITIAL, MasteryState.CONSOLIDATED, MasteryState.INTEGRATED}
    reached, escalated = set(), set()
    for m in states:
        require(bool(m.evidence_ids), "mastery without evidence")
        require(
            all(i in evidence and evidence[i].student_id == m.student_id for i in m.evidence_ids),
            "mastery evidence reference differs",
        )
        support = [
            e
            for e in evidence.values()
            if e.student_id == m.student_id
            and e.knowledge_point_id == CHAIN
            and e.kind == EvidenceKind.VERIFICATION
        ]
        if m.state in mastered:
            dims = {e.dimension for e in support if e.is_correct is True}
            require(
                {Dimension.CORRECTNESS, Dimension.DISTRACTOR} <= dims
                and bool({Dimension.NEAR_TRANSFER, Dimension.EXPLANATION} & dims),
                "goal reached without required dimensions",
            )
            reached.add(m.student_id)
        elif m.state == MasteryState.ESCALATED:
            handoffs = [
                t
                for t in actual.tasks
                if t.student_id == m.student_id
                and t.knowledge_point_id == CHAIN
                and t.type == TaskType.ESCALATION
            ]
            require(
                any(
                    t.evidence_ids
                    and t.reason
                    and all(
                        i in evidence and evidence[i].student_id == m.student_id
                        for i in t.evidence_ids
                    )
                    for t in handoffs
                ),
                "escalation lacks task/evidence/reason",
            )
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
