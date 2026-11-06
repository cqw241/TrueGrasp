"""Read-only, deterministic T20 input loader and mathematical acceptance checks.

Callers submit answers through the real ingest/orchestration boundary. This module
never imports a database session, constructs Evidence/Mastery, or calls an Agent.
"""

from collections import Counter, defaultdict
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.api import MessageCreate
from app.contracts.entities import Item
from app.contracts.enums import Dimension, ItemPurpose, ItemSource
from app.contracts.values import SLIP, ItemSpec, Prediction
from app.knowledge.content import Content, load_dir
from app.knowledge.graph import NxKnowledgeGraph
from app.verify.tools import SympyMathTools

ROOT = Path(__file__).resolve().parents[1]
CHAIN = "calc.ch2.chain_rule_multi"
MULTIVAR = "calc.ch9.multivar_chain"
MISS = "calc.ch2.chain.miss_inner"
FORMULA = "calc.ch2.chain.formula_sub"
HOMEWORK = "calc.ch2.chain_rule_multi.demo_homework"
POLAR = "calc.ch10.polar.demo_homework"
PROBE1 = "calc.ch2.chain_multi.probe_sin3x"
PROBE2 = "calc.ch2.chain_multi.probe_cos2_5x"


@lru_cache(maxsize=512)
def _equivalent(a, b, mode, variables):
    return SympyMathTools().check_equivalent(a, b, mode, list(variables)).equivalent


@lru_cache(maxsize=128)
def _verify_item(spec_json, answer):
    return SympyMathTools().verify_item(ItemSpec.model_validate_json(spec_json), answer)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Learner(Strict):
    id: str
    name: str
    homework_cause: str | None
    operator: Literal["human", "script"]


class Scenario(Strict):
    schema_version: Literal[1]
    status: Literal["input_plan_not_prerun"]
    students: list[Learner]
    relapse_students: list[str]
    escalation_students: list[str]


class Answer(Strict):
    key: str
    stage: str
    teaching_date: date
    student_id: str
    item_id: str
    student_answer: str
    operator: Literal["human", "script", "live"]
    round: int = Field(default=1, ge=1, le=2)
    context_knowledge_point_id: str | None = None
    intended_cause: str | None = None  # fixture intent, NEVER an ingest signal

    def message(self) -> MessageCreate:
        """Only student input, with no predicted verdict or mastery state."""
        return MessageCreate(content=self.student_answer, answering_item_id=self.item_id)


class Dataset:
    def __init__(self, scenario: Scenario, content: Content):
        self.scenario = scenario
        self.content = content
        self.items = dict(content.items)
        self.anchors: dict[str, str] = {}
        self.answers: list[Answer] = []

    def batch(self, stage: str, *, include_human: bool = False) -> list[Answer]:
        """Human rehearsal inputs are opt-in; D is always excluded from replay."""
        return [
            a
            for a in self.answers
            if a.stage == stage
            and a.operator != "live"
            and (include_human or a.operator == "script")
        ]

    def answer_for(self, key: str, displayed: Item) -> MessageCreate:
        """Resolve a real waiting item; fail closed if the Agent chose another question.

        T14 must keep the real displayed ID, even when its spec matches a fixture.
        This is not an oracle for arbitrary generated questions.
        """
        answer = next(a for a in self.answers if a.key == key)
        planned = self.items[answer.item_id]
        tools = SympyMathTools()
        if displayed.spec is None or planned.spec is None:
            raise ValueError("T20 requires a computable displayed item")
        if tools.fingerprint(displayed.spec) != tools.fingerprint(planned.spec):
            raise ValueError(f"{key}: displayed item differs; operator input required")
        if displayed.knowledge_point_id != planned.knowledge_point_id:
            raise ValueError(f"{key}: displayed knowledge point differs")
        if displayed.dimension != planned.dimension or displayed.purpose != planned.purpose:
            raise ValueError(f"{key}: displayed assessment purpose/dimension differs")
        return MessageCreate(content=answer.student_answer, answering_item_id=displayed.id)

    def export(self) -> dict:
        """Portable plan for T14, deliberately containing no observed results."""
        return {
            "schema_version": 1,
            "status": "input_plan_not_prerun",
            "students": [s.model_dump(mode="json") for s in self.scenario.students],
            "goal": {
                "knowledge_point_ids": [CHAIN],
                "target_state": "initial",
                "target_ratio": 0.9,
                "deadline": "2026-09-11",
                "max_rounds": 2,
            },
            "items": [
                i.model_dump(mode="json")
                for i in self.items.values()
                if any(a.item_id == i.id for a in self.answers)
            ],
            "demo_item_c2_anchors": self.anchors,
            "answers": [a.model_dump(mode="json") for a in self.answers],
            "expected_only": {
                "goal_knowledge_point_id": CHAIN,
                "checkpoint": "after_ch9_followup_before_ch10_live",
                "goal_met": 37,
                "escalated": self.scenario.escalation_students,
                "ch9_relapse": self.scenario.relapse_students,
            },
        }


def _prediction(item: Item, cause: str) -> str:
    return next(p.answer for p in item.predictions if p.misconception_id == cause)


def _chain_item(data: Dataset, name: str, coefficient: int, dimension: Dimension) -> str:
    """Demo-authored new items, not fictitious C2 IDs or LLM-generated output."""
    cross = dimension == Dimension.CROSS_CHAPTER
    var = "t" if cross else "x"
    k = coefficient
    stem = (
        f"z=sin(u)^2, u={k}t，求 dz/dt，说明沿依赖路径求导。"
        if cross
        else f"求 $\\sin^2({k}x)$ 的导数。"
    )
    item = Item(
        id=f"demo.t20.{name}",
        knowledge_point_id=CHAIN,
        purpose=ItemPurpose.VERIFICATION,
        dimension=dimension,
        source=ItemSource.SEED,
        stem=stem,
        spec=ItemSpec(operation="derivative", expr=f"sin({k}*{var})**2", variables=[var]),
        answer=rf"{2 * k}\sin({k}{var})\cos({k}{var})",
        predictions=[
            Prediction(misconception_id=cause, answer=rf"2\sin({k}{var})\cos({k}{var})")
            for cause in (MISS, FORMULA)
        ],
    )
    if dimension == Dimension.NEAR_TRANSFER:
        # Change the outer structure, rather than just the numerical coefficient.
        item.stem = rf"求 $e^{{\sin({k}x)}}$ 的导数。"
        item.spec = ItemSpec(operation="derivative", expr=f"exp(sin({k}*x))", variables=["x"])
        item.answer = rf"{k}e^{{\sin({k}x)}}\cos({k}x)"
        item.predictions = [
            Prediction(misconception_id=cause, answer=rf"e^{{\sin({k}x)}}\cos({k}x)")
            for cause in (MISS, FORMULA)
        ]
    data.items[item.id] = item
    data.anchors[item.id] = HOMEWORK
    return item.id


def load_dataset(
    scenario_path: Path = ROOT / "demo/data/scenario.json",
    content_dir: Path = ROOT / "content/lectures",
) -> Dataset:
    """Load actual C2 content and expand the versioned input scenario, without I/O writes."""
    scenario = Scenario.model_validate_json(scenario_path.read_text(encoding="utf-8"))
    content = load_dir(content_dir)
    if content.errors:
        raise ValueError("\n".join(map(str, content.errors)))
    data = Dataset(scenario, content)
    required = [
        HOMEWORK,
        POLAR,
        PROBE1,
        PROBE2,
        "calc.ch2.chain_multi.seed1",
        "calc.ch2.chain_rule_multi.seed_square_inner",
        "calc.ch1.composition.probe_order",
        "calc.ch10.polar.probe_disk_area",
    ]
    for item_id in required:
        if item_id not in data.items:
            raise ValueError(f"missing C2 item: {item_id}")

    def add(student, stage, when, item_id, cause=None, *, operator=None, round=1):
        item = data.items[item_id]
        value = _prediction(item, cause) if cause not in (None, SLIP) else item.answer
        if stage == "homework_ch2" and cause == SLIP:
            value = _prediction(item, MISS)  # same initial answer, different probe behavior
        data.answers.append(
            Answer(
                key=f"{stage}:{student.id}:{round}:{item_id}",
                stage=stage,
                teaching_date=date.fromisoformat(when),
                student_id=student.id,
                item_id=item_id,
                student_answer=value,
                operator=operator or student.operator,
                round=round,
                context_knowledge_point_id=MULTIVAR if stage == "precheck_ch9" else None,
                intended_cause=cause,
            )
        )

    near = _chain_item(data, "initial.near", 4, Dimension.NEAR_TRANSFER)
    retention = _chain_item(data, "friday.retention", 7, Dimension.RETENTION)
    precheck = _chain_item(data, "ch9.cross", 11, Dimension.CROSS_CHAPTER)
    b_rounds = [_chain_item(data, f"b.retry{r}", 12 + r, Dimension.DISTRACTOR) for r in (1, 2)]
    relapse_rounds = [
        _chain_item(data, f"ch9.retry{r}", 14 + r, Dimension.DISTRACTOR) for r in (1, 2)
    ]
    # Chronological ordering is finalized below; item choice is stable across loads.
    for student in scenario.students:
        cause = student.homework_cause
        add(student, "homework_ch2", "2026-09-07", HOMEWORK, cause, operator="script")
        if cause:
            add(student, "diagnosis_ch2", "2026-09-07", PROBE1, cause)
            if cause != FORMULA:
                add(student, "diagnosis_ch2", "2026-09-07", PROBE2, cause)
        for item_id in (
            "calc.ch2.chain_multi.seed1",
            "calc.ch2.chain_rule_multi.seed_square_inner",
            near,
        ):
            add(student, "verification", "2026-09-08", item_id)
        add(
            student,
            "retest_friday",
            "2026-09-11",
            retention,
            FORMULA if student.id == "B" else None,
        )
        if student.id == "B":
            add(
                student,
                "backtrack_ch1",
                "2026-09-14",
                "calc.ch1.composition.probe_order",
                "calc.ch1.composition.reverse_order",
            )
            for r, item_id in enumerate(b_rounds, 1):
                add(student, "b_followup", f"2026-09-{14 + r:02d}", item_id, FORMULA, round=r)
        # Conservative roster: check all 40 (unconsolidated prerequisite nodes qualify).
        # B passing here does not resolve the already escalated ch2 goal.
        add(
            student,
            "precheck_ch9",
            "2026-10-26",
            precheck,
            MISS if student.id in scenario.relapse_students else None,
        )
        if student.id in scenario.relapse_students:
            for r, item_id in enumerate(relapse_rounds, 1):
                add(student, "ch9_followup", f"2026-10-{26 + r:02d}", item_id, MISS, round=r)
        if student.id == "D":
            add(
                student,
                "homework_ch10_live",
                "2026-11-02",
                POLAR,
                "calc.ch10.polar.miss_r",
                operator="live",
            )
            add(
                student,
                "diagnosis_ch10_live",
                "2026-11-02",
                "calc.ch10.polar.probe_disk_area",
                "calc.ch10.polar.miss_r",
                operator="live",
            )
    data.answers.sort(key=lambda a: a.teaching_date)  # stable within a day's narrative
    return data


def validate_dataset(data: Dataset) -> dict:
    """Check fixture math/counts and feasibility, never claim an actual prerun passed."""

    def require(ok, message):
        if not ok:
            raise ValueError(message)

    roster = {s.id: s for s in data.scenario.students}
    require(len(roster) == len(data.scenario.students) == 40, "expected 40 unique students")
    require(set("ABCD") <= roster.keys(), "A/B/C/D must exist")
    causes = Counter(s.homework_cause for s in roster.values() if s.homework_cause)
    require(causes == Counter({MISS: 8, FORMULA: 3, SLIP: 1}), "expected 12 errors, 8 same cause")
    require(
        [roster[s].homework_cause for s in "ABC"] == [MISS, FORMULA, SLIP],
        "A/B/C behavioral profiles differ from C2",
    )
    require(data.scenario.relapse_students == ["S05", "S06"], "expected two ch9 relapses")
    require(
        set(data.scenario.escalation_students) == {"B", "S05", "S06"},
        "expected B plus two relapses escalated",
    )
    require(all(roster[s].operator == "human" for s in "ABC"), "A/B/C require human rehearsal")
    graph = NxKnowledgeGraph(
        list(data.content.points.values()),
        list(data.content.misconceptions.values()),
        list(data.content.strategies.values()),
        list(data.content.items.values()),
    )
    require(bool(graph.path("calc.ch1.composition", MULTIVAR)), "C2 1→2→9 path missing")
    require(bool(graph.path("calc.ch4.substitution", "calc.ch10.polar")), "C2 4→5→10 path missing")
    for cause in (MISS, FORMULA):
        require(
            MULTIVAR in data.content.misconceptions[cause].recurs_in,
            f"C2 recurrence link missing: {cause}",
        )

    tools = SympyMathTools()

    used_items = {a.item_id for a in data.answers}
    for item_id in used_items:
        item = data.items[item_id]
        require(item.knowledge_point_id in data.content.points, f"unknown point: {item_id}")
        require(item.spec is not None, f"missing computable spec: {item_id}")
        require(
            _verify_item(item.spec.model_dump_json(), item.answer).ok is True,
            f"invalid math: {item_id}",
        )
        for pred in item.predictions:
            require(
                pred.misconception_id in data.content.misconceptions,
                f"unknown cause: {pred.misconception_id}",
            )
        if item.predictions:
            distinct = tools.check_distinct(item.spec, item.answer, item.predictions)
            require(distinct.ok is True, f"indistinct predictions: {item_id}")
            if item.dimension == Dimension.DISTRACTOR or item_id in data.anchors:
                require(
                    all(distinct.separates(cause) for cause in (MISS, FORMULA)),
                    f"does not expose old causes: {item_id}",
                )
    seen = defaultdict(set)
    counts = Counter()
    groups = defaultdict(list)
    keys = set()
    for answer in data.answers:
        require(answer.key not in keys, f"duplicate answer key: {answer.key}")
        keys.add(answer.key)
        require(answer.student_id in roster, f"unknown student: {answer.student_id}")
        item = data.items[answer.item_id]
        spec = item.spec
        correct = _equivalent(
            answer.student_answer, item.answer, spec.answer_mode, tuple(spec.variables)
        )
        require(correct is not None, f"unparseable answer: {answer.key}")
        if answer.intended_cause not in (None, SLIP):
            require(
                _equivalent(
                    answer.student_answer,
                    _prediction(item, answer.intended_cause),
                    spec.answer_mode,
                    tuple(spec.variables),
                )
                is True,
                f"answer misses intended prediction: {answer.key}",
            )
        else:
            require(
                correct == (answer.intended_cause != SLIP or answer.stage != "homework_ch2"),
                f"unexpected correct/incorrect answer: {answer.key}",
            )
        if correct is False:
            counts[answer.stage] += 1
        groups[answer.stage].append(answer)
        fp = tools.fingerprint(spec)
        require(fp not in seen[answer.student_id], f"reused student fingerprint: {answer.key}")
        seen[answer.student_id].add(fp)
    require(counts["homework_ch2"] == 12, "wrong homework count")
    require(counts["precheck_ch9"] == 2, "wrong ch9 relapse count")
    require(counts["retest_friday"] == 1, "B alone should relapse on Friday")
    for stage in ("homework_ch2", "retest_friday", "precheck_ch9"):
        require(
            Counter(a.student_id for a in groups[stage]) == Counter(roster.keys()),
            f"missing/duplicate roster coverage: {stage}",
        )
    same = [a.student_answer for a in groups["homework_ch2"] if a.student_id in "ABC"]
    require(len(set(same)) == 1, "A/B/C must share the initial wrong answer")
    # Derive diagnosis from both predictions and actual planned responses; the
    # shared homework wrong answer alone is deliberately insufficient.
    confirmed = {}
    for sid in roster:
        probes = [a for a in groups["diagnosis_ch2"] if a.student_id == sid]
        candidates = {MISS, FORMULA, SLIP}
        for answer in probes:
            item = data.items[answer.item_id]
            candidates &= {
                cause
                for cause in candidates
                if _equivalent(
                    answer.student_answer,
                    item.answer if cause == SLIP else _prediction(item, cause),
                    item.spec.answer_mode,
                    tuple(item.spec.variables),
                )
                is True
            }
        if probes:
            require(len(candidates) == 1, f"ambiguous diagnosis: {sid}")
            confirmed[sid] = candidates.pop()
    require(Counter(confirmed.values()) == causes, "probe-derived diagnoses differ from roster")
    for sid in roster:
        require(
            all(
                _equivalent(
                    a.student_answer,
                    data.items[a.item_id].answer,
                    data.items[a.item_id].spec.answer_mode,
                    tuple(data.items[a.item_id].spec.variables),
                )
                is True
                for a in groups["verification"]
                if a.student_id == sid
            ),
            f"initial verification must pass: {sid}",
        )
        dims = {
            data.items[a.item_id].dimension for a in groups["verification"] if a.student_id == sid
        }
        require(
            {Dimension.CORRECTNESS, Dimension.DISTRACTOR, Dimension.NEAR_TRANSFER} <= dims,
            f"missing initial mastery dimensions: {sid}",
        )
    for sid in data.scenario.escalation_students:
        stage = "b_followup" if sid == "B" else "ch9_followup"
        failures = [a for a in groups[stage] if a.student_id == sid]
        require(
            {a.round for a in failures} == {1, 2} and len(failures) == 2,
            f"two rounds required before escalation: {sid}",
        )
        require(
            all(
                _equivalent(
                    a.student_answer,
                    data.items[a.item_id].answer,
                    data.items[a.item_id].spec.answer_mode,
                    tuple(data.items[a.item_id].spec.variables),
                )
                is False
                for a in failures
            ),
            f"followup must fail: {sid}",
        )
    failed_students = {
        a.student_id
        for stage in ("retest_friday", "precheck_ch9")
        for a in groups[stage]
        if _equivalent(
            a.student_answer,
            data.items[a.item_id].answer,
            data.items[a.item_id].spec.answer_mode,
            tuple(data.items[a.item_id].spec.variables),
        )
        is False
    }
    require(
        failed_students == set(data.scenario.escalation_students),
        "failed followups do not support expected escalation cohort",
    )
    goal_met_expected = len(roster.keys() - failed_students)
    require(goal_met_expected == 37, "plan cannot support 37 goal completions")
    require(
        all(a.operator == "live" for a in data.answers if "ch10" in a.stage), "D must remain live"
    )
    require(
        len(groups["homework_ch10_live"]) == len(groups["diagnosis_ch10_live"]) == 1,
        "D live inputs missing",
    )
    return {
        "status": "input_plan_valid_not_prerun",
        "students": 40,
        "homework_errors": 12,
        "same_cause": 8,
        "individual_followup": 4,
        "ch9_relapses_expected": 2,
        "goal_met_expected": goal_met_expected,
        "escalated_expected": len(failed_students),
        "answer_inputs": len(data.answers),
        "math_items_checked": len(used_items),
        "goal_scope": CHAIN,
        "observed_results": None,
    }
