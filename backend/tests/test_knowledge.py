"""T04 验收：内容骨架、入库检查、依赖图查询与判别追问题起草。

数学内容用真实核验工具检查；起草的重试逻辑使用预设回复，不调用远程模型。
"""

from pathlib import Path

import pytest
import yaml
from httpx2 import ASGITransport, AsyncClient
from sqlmodel import Session

from app.contracts.enums import ItemPurpose
from app.contracts.values import ItemSpec, Prediction
from app.contracts.verify import AnswerGroup, Distinctness, ItemCheck
from app.db import init_db, make_engine
from app.knowledge.content import check_math, load
from app.knowledge.drafting import ProbeDraft, draft_probe
from app.knowledge.graph import NxKnowledgeGraph
from app.knowledge.ingest import ingest
from app.main import create_app

CONTENT = Path(__file__).resolve().parents[2] / "content"
LECTURES = sorted((CONTENT / "lectures").glob("*.yaml"))
REFINED_POINTS = {
    "calc.ch1.function",
    "calc.ch1.composition",
    "calc.ch2.derivative_rules",
    "calc.ch2.chain_rule",
    "calc.ch2.chain_rule_multi",
    "calc.ch2.implicit_diff",
    "calc.ch9.partial_derivative",
    "calc.ch9.multivar_chain",
    "calc.ch4.substitution",
    "calc.ch5.substitution",
    "calc.ch5.integration_by_parts",
    "calc.ch10.double_integral_cartesian",
    "calc.ch10.polar",
}
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def template_content(tmp_path):
    """模板是独立示例，不能与已经复制进讲次文件的同 ID 内容重复加载。"""
    skeletons = []
    for path in LECTURES:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        skeleton = {k: v for k, v in doc.items() if k in {"lecture", "title", "sections", "points"}}
        skeletons.append(write(tmp_path, path.name, yaml.safe_dump(skeleton, allow_unicode=True)))
    return load([*skeletons, CONTENT / "template.yaml"])


class FakeTools:
    """答案按字符串比较；``answers`` 是 spec.expr 到标准答案的映射。"""

    def __init__(self, answers: dict[str, str]):
        self.answers = answers

    def fingerprint(self, spec: ItemSpec) -> str:
        return spec.expr

    def verify_item(self, spec: ItemSpec, answer: str) -> ItemCheck:
        computed = self.answers[spec.expr]
        return ItemCheck(ok=computed == answer, computed=computed)

    def check_distinct(self, spec: ItemSpec, correct: str, predictions: list[Prediction]):
        groups = {correct: AnswerGroup(answer=correct, misconception_ids=["slip"], is_correct=True)}
        for p in predictions:
            groups.setdefault(
                p.answer, AnswerGroup(answer=p.answer, misconception_ids=[], is_correct=False)
            ).misconception_ids.append(p.misconception_id)
        ok = len(groups) >= 2
        return Distinctness(ok=ok, groups=list(groups.values()), detail="" if ok else "只有一组")


TEMPLATE_TOOLS = FakeTools(
    {
        "sin(3*x)": r"3\cos(3x)",
        "cos(5*x)**2": r"-10\cos(5x)\sin(5x)",
        "sin(2*x)**3": r"6\sin^2(2x)\cos(2x)",
    }
)


def write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_book_skeleton_and_template_pass_checks(template_content):
    content = template_content
    check_math(content, TEMPLATE_TOOLS)
    assert content.errors == []
    assert len(content.sections) == 75
    assert {p.lecture for p in content.points.values()} == set(range(1, 13))


def test_check_reports_specific_errors(tmp_path):
    bad = write(
        tmp_path,
        "bad.yaml",
        r"""
lecture: 1
sections: [{section: 1, title: 映射与函数}]
points:
- {id: a, section: 1, name: A, prerequisites: [b]}
- {id: b, section: 1, name: B, prerequisites: [a]}
- {id: c, section: 1, name: C, prerequisites: [missing]}
- {id: c, section: 1, name: C2}
misconceptions:
- {id: m1, knowledge_point: a, family: f, description: d, probes: [p1], strategies: [nope]}
- {id: m2, knowledge_point: a, family: f, description: d, probes: [p1]}
items:
- id: p1
  knowledge_point: a
  purpose: probe
  stem: 求 $\sin(3x)$ 的导数
  spec: {operation: derivative, expr: sin(3*x), variables: [x]}
  answer: '\cos(3x)'
  predictions: [{misconception_id: m1, answer: '\cos(3x)'}]
""",
    )
    content = load([bad])
    check_math(content, TEMPLATE_TOOLS)
    messages = {(i.ref, i.message.split("：")[0]) for i in content.errors}
    assert all(i.file == "bad.yaml" for i in content.errors)
    assert ("c", "ID 重复，已在 bad.yaml 中定义") in messages
    assert ("c", "引用的先修知识点不存在") in messages
    assert any(ref in ("a", "b") and m == "先修关系成环" for ref, m in messages)
    assert ("m1", "引用的策略卡不存在") in messages
    assert ("p1", "缺少错因 m2 的预测答案") in messages
    assert ("p1", r"标准答案与工具重算结果 3\cos(3x) 不一致") in messages
    assert ("p1", "追问题没有区分度") in messages


def test_real_math_checks_validate_teacher_template(template_content):
    from app.verify.tools import SympyMathTools

    for content in (template_content, load(LECTURES)):
        check_math(content, SympyMathTools())
        assert content.issues == []


def test_math_check_reports_invalid_specs_and_missing_quality_requirements():
    from app.verify.tools import SympyMathTools

    content = load(LECTURES)
    malformed = content.items["calc.ch2.chain_multi.seed1"]
    malformed.spec.expr = "sin("
    distractor = content.items["calc.ch2.chain_rule_multi.seed_square_inner"]
    distractor.predictions[0].answer = distractor.answer
    explanation = content.items["calc.ch12.s1.seed_term_zero_counterexample"]
    explanation.rubric = [" "]
    check_math(content, SympyMathTools())
    assert {(i.file, i.ref) for i in content.errors} == {
        ("02.yaml", malformed.id),
        ("02.yaml", distractor.id),
        ("12.yaml", explanation.id),
    }


def test_c2_diagnostic_coverage_and_fresh_seeds():
    """每个精修错因可被追问证伪，验证回退题不重复教学中使用的题。"""
    from app.verify.tools import SympyMathTools

    content = load(LECTURES)
    assert content.errors == []
    tools = SympyMathTools()
    graph = NxKnowledgeGraph(
        list(content.points.values()),
        list(content.misconceptions.values()),
        list(content.strategies.values()),
        list(content.items.values()),
    )
    teaching_fingerprints = {
        tools.fingerprint(i.spec)
        for i in content.items.values()
        if i.spec and i.purpose != ItemPurpose.VERIFICATION
    }
    seed_fingerprints = set()
    for kp in sorted(REFINED_POINTS):
        misconceptions = graph.misconceptions(kp)
        assert 2 <= len(misconceptions) <= 4, kp
        for mis in misconceptions:
            assert mis.examples and mis.recurs_in, mis.id
            cards = graph.strategies(mis.id)
            assert cards and all(s.principles and s.constraints and s.rubric for s in cards), mis.id
            assert any(
                tools.check_distinct(probe.spec, probe.answer, probe.predictions).separates(mis.id)
                for probe in (content.items[pid] for pid in mis.probe_item_ids)
            ), mis.id
        seeds = [i for i in graph.seed_items(kp) if i.purpose == ItemPurpose.VERIFICATION]
        assert {i.dimension.value for i in seeds} >= {"correctness", "distractor", "near_transfer"}
        assert any(
            {p.misconception_id for p in seed.predictions} == {m.id for m in misconceptions}
            for seed in seeds
            if seed.dimension.value == "distractor"
        ), kp
        for seed in seeds:
            assert seed.spec is not None, seed.id
            fingerprint = tools.fingerprint(seed.spec)
            assert fingerprint not in teaching_fingerprints | seed_fingerprints, seed.id
            seed_fingerprints.add(fingerprint)
            if seed.dimension.value == "distractor":
                assert seed.predictions, seed.id
                distinct = tools.check_distinct(seed.spec, seed.answer, seed.predictions)
                assert all(distinct.separates(p.misconception_id) for p in seed.predictions), (
                    seed.id
                )

    for mid in ("calc.ch2.chain.miss_inner", "calc.ch2.chain.formula_sub"):
        assert "calc.ch9.multivar_chain" in content.misconceptions[mid].recurs_in
    series = [i for i in content.items.values() if i.knowledge_point_id.startswith("calc.ch12.")]
    assert series and all(i.spec is None and i.rubric for i in series)


def test_graph_paths_and_lookups(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'business.db'}")
    init_db(engine)
    content = load(LECTURES)
    with Session(engine) as session:
        ingest(session, content)
        session.commit()
    graph = NxKnowledgeGraph.from_engine(engine)

    ids = [p.id for p in graph.path("calc.ch1.composition", "calc.ch9.multivar_chain")]
    assert ids == ["calc.ch1.composition", "calc.ch2.chain_rule", "calc.ch9.multivar_chain"]
    lectures = [p.lecture for p in graph.path("calc.ch4.substitution", "calc.ch10.polar")]
    assert lectures == [4, 5, 10]
    assert graph.path("calc.ch9.multivar_chain", "calc.ch1.composition") == []

    ancestors = [p.id for p in graph.ancestors("calc.ch2.chain_rule_multi")]
    assert ancestors[0] == "calc.ch2.chain_rule"
    assert "calc.ch1.function" in ancestors
    assert "calc.ch2.chain_rule_multi" in {p.id for p in graph.dependents("calc.ch2.chain_rule")}

    assert [s.id for s in graph.strategies("calc.ch2.chain.formula_sub")] == [
        "numeric_slope_conflict"
    ]
    assert len(graph.misconceptions("calc.ch2.chain_rule_multi")) == 2
    assert "calc.ch2.chain_multi.seed1" in {
        i.id for i in graph.seed_items("calc.ch2.chain_rule_multi")
    }


async def test_knowledge_endpoints(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'business.db'}")
    init_db(engine)
    with Session(engine) as session:
        ingest(session, load(LECTURES))
        session.commit()
    app = create_app(engine=engine, checkpoint_path=str(tmp_path / "cp.db"))
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            path = await client.get(
                "/api/knowledge/path",
                params={"source": "calc.ch1.composition", "target": "calc.ch9.multivar_chain"},
            )
            assert [p["lecture"] for p in path.json()] == [1, 2, 9]
            points = await client.get("/api/knowledge/points", params={"lecture": 10})
            assert "calc.ch10.polar" in [p["id"] for p in points.json()]
            missing = await client.get(
                "/api/knowledge/path", params={"source": "nope", "target": "calc.ch10.polar"}
            )
            assert missing.status_code == 404


class FakeLLM:
    def __init__(self, drafts: list[ProbeDraft]):
        self.drafts = drafts
        self.prompts: list[str] = []

    async def generate(self, messages, output, tools=None):
        self.prompts.append(messages[-1].content)
        return self.drafts.pop(0)


def probe(expr: str, answer: str, predicted: str) -> ProbeDraft:
    return ProbeDraft(
        stem=f"求 ${expr}$ 的导数",
        spec=ItemSpec(operation="derivative", expr=expr, variables=["x"]),
        answer=answer,
        predictions=[Prediction(misconception_id="calc.ch2.chain.miss_inner", answer=predicted)],
        rationale="漏乘内层导数时系数不同",
    )


async def test_draft_probe_retries_until_distinct(template_content):
    content = template_content
    llm = FakeLLM(
        [
            probe("sin(3*x)", r"3\cos(3x)", r"3\cos(3x)"),  # 目标错因的预测等于正确答案
            probe("cos(5*x)**2", r"-10\cos(5x)\sin(5x)", r"-2\cos(5x)\sin(5x)"),
        ]
    )
    taken = {"calc.ch2.chain.miss_inner.probe1"}  # 已有一份待审稿
    result = await draft_probe(content, "calc.ch2.chain.miss_inner", llm, TEMPLATE_TOOLS, taken)

    assert result.item is not None
    assert result.item.spec.expr == "cos(5*x)**2"
    assert result.item.id == "calc.ch2.chain.miss_inner.probe2"
    assert TEMPLATE_TOOLS.check_distinct(
        result.item.spec, result.item.answer, result.item.predictions
    ).separates("calc.ch2.chain.miss_inner")
    assert len(result.failures) == 1  # 第一稿只能分出正确答案组，被退回
    assert result.failures[0] in llm.prompts[1]


@pytest.mark.parametrize(
    ("field", "value"),
    [("stem", "求 $\\sin^3(4x)$ 的导数"), ("rubric", ["写出外层导数即可得分"])],
)
def test_ingest_rejects_changing_answered_item(tmp_path, field, value):
    from datetime import date

    from app.contracts.entities import Evidence, Student
    from app.contracts.enums import EvidenceKind, JudgedBy
    from app.knowledge.ingest import IngestError

    engine = make_engine(f"sqlite:///{tmp_path / 'business.db'}")
    init_db(engine)
    content = load(LECTURES)
    with Session(engine) as session:
        ingest(session, content)
        session.add(Student(id="A", name="学生 A"))
        session.flush()
        session.add(
            Evidence(
                student_id="A",
                knowledge_point_id="calc.ch2.chain_rule_multi",
                kind=EvidenceKind.VERIFICATION,
                item_id="calc.ch2.chain_multi.seed1",
                student_answer=r"6\sin^2(2x)\cos(2x)",
                is_correct=True,
                judged_by=JudgedBy.TOOL,
                teaching_date=date(2026, 9, 7),
            )
        )
        session.commit()

    ingest(Session(engine), content)  # 内容不变时可以重复入库
    changed = load(LECTURES)
    setattr(changed.items["calc.ch2.chain_multi.seed1"], field, value)
    with Session(engine) as session, pytest.raises(IngestError) as exc:
        ingest(session, changed)
    assert [i.ref for i in exc.value.issues] == ["calc.ch2.chain_multi.seed1"]


def test_missing_content_directory_is_an_error(tmp_path):
    from app.knowledge.content import load_dir

    assert load_dir(tmp_path / "nope").errors[0].message == "内容目录不存在"
