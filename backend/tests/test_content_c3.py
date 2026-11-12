"""C3：全书目录的每一节有错因，每个知识点有可检索的验证种子题。"""

import json
from pathlib import Path

import pytest

from app.agents.verifier.agent import IndependentVerifier, ModelReview
from app.contracts.enums import Dimension, ItemPurpose, JudgedBy
from app.knowledge.content import load_dir
from app.knowledge.graph import NxKnowledgeGraph
from app.knowledge.skeleton import parse_toc
from app.verify.tools import SympyMathTools

ROOT = Path(__file__).resolve().parents[2]


def test_full_book_has_diagnostic_content_and_verification_seeds():
    content = load_dir(ROOT / "content" / "lectures")
    assert not content.errors
    expected_sections = {
        (lecture.lecture, section.section): section
        for lecture in parse_toc((ROOT / "高等数学目录.md").read_text(encoding="utf-8"))
        for section in lecture.sections
    }
    assert content.sections == expected_sections
    graph = NxKnowledgeGraph(
        list(content.points.values()),
        list(content.misconceptions.values()),
        list(content.strategies.values()),
        list(content.items.values()),
    )
    covered_sections = set()
    for point in graph.all_points():
        misconceptions = graph.misconceptions(point.id)
        assert misconceptions, f"{point.id} 缺少可检索的错因"
        covered_sections.add((point.lecture, point.section))
        for misconception in misconceptions:
            assert misconception.examples, misconception.id
            assert all(
                e.question.strip() and e.wrong.strip() and e.correct.strip()
                for e in misconception.examples
            ), misconception.id
            cards = graph.strategies(misconception.id)
            assert cards and all(c.principles and c.constraints and c.rubric for c in cards), (
                misconception.id
            )
        seeds = [
            item for item in graph.seed_items(point.id) if item.purpose == ItemPurpose.VERIFICATION
        ]
        assert seeds, f"{point.id} 缺少验证种子题"
        for seed in seeds:
            assert seed.stem.strip() and seed.answer.strip(), seed.id
            assert seed.spec or (seed.dimension == Dimension.EXPLANATION and seed.rubric), seed.id
        if point.lecture > 1:
            assert graph.prerequisites(point.id), f"{point.id} 缺少先修关系"
    assert covered_sections == set(expected_sections)


@pytest.mark.anyio
async def test_log_domain_error_requires_rubric_review():
    content = load_dir(ROOT / "content" / "lectures")
    seed = content.items["calc.ch4.s1.c3_seed1"]
    payloads = []

    class DomainReviewer:
        async def generate(self, messages, output, tools=None):
            assert output is ModelReview
            payload = json.loads(messages[-1].content)
            payloads.append(payload)
            return ModelReview(
                is_correct=False,
                rubric_results=[False] * len(payload["rubric"]),
                reason="ln(x-2) 在题设 x<2 时没有实数定义，不能只比较形式导数。",
            )

    verifier = IndependentVerifier(DomainReviewer(), SympyMathTools(), None)
    result = await verifier.judge(seed, r"\ln(x-2)")
    assert result.is_correct is False
    assert result.judged_by == JudgedBy.MODEL
    [payload] = payloads
    assert payload["tool_is_correct"] is None
    assert payload["rubric"] and any("定义域" in item for item in payload["rubric"])
