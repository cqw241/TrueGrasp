"""T04 验收：内容骨架、入库检查、依赖图查询与判别追问题起草。

核验工具（T05）和模型接入（T06）尚未实现，测试用按字符串比较的假工具和预设回复的假模型。
"""

from pathlib import Path

import pytest
from httpx2 import ASGITransport, AsyncClient
from sqlmodel import Session

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
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


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


def test_book_skeleton_and_template_pass_checks():
    content = load([*LECTURES, CONTENT / "template.yaml"])
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


def test_math_checks_are_skipped_until_t05():
    from app.verify.tools import SympyMathTools

    content = load([*LECTURES, CONTENT / "template.yaml"])
    check_math(content, SympyMathTools())
    assert content.errors == []
    assert [i.level for i in content.issues] == ["notice"]


def test_graph_paths_and_lookups(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'business.db'}")
    init_db(engine)
    content = load([*LECTURES, CONTENT / "template.yaml"])
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
    assert [i.id for i in graph.seed_items("calc.ch2.chain_rule_multi")][-1].endswith("seed1")


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


async def test_draft_probe_retries_until_distinct():
    content = load([*LECTURES, CONTENT / "template.yaml"])
    llm = FakeLLM(
        [
            probe("sin(3*x)", r"3\cos(3x)", r"3\cos(3x)"),  # 目标错因的预测等于正确答案
            probe("cos(5*x)**2", r"-10\cos(5x)\sin(5x)", r"-2\cos(5x)\sin(5x)"),
        ]
    )
    result = await draft_probe(content, "calc.ch2.chain.miss_inner", llm, TEMPLATE_TOOLS)

    assert result.item is not None
    assert result.item.spec.expr == "cos(5*x)**2"
    assert result.item.id == "calc.ch2.chain.miss_inner.probe2"
    assert TEMPLATE_TOOLS.check_distinct(
        result.item.spec, result.item.answer, result.item.predictions
    ).separates("calc.ch2.chain.miss_inner")
    assert len(result.failures) == 1  # 第一稿只能分出正确答案组，被退回
    assert result.failures[0] in llm.prompts[1]
