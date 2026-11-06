"""T18 acceptance: isolation, quality gates, all dimensions and independent judgements."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.agents.verifier import NoValidItemError, create_verifier
from app.agents.verifier.agent import ItemDraft, ItemReview, ModelReview
from app.config import Settings
from app.contracts.agents import ConfirmedMisconception, VerifierInput
from app.contracts.entities import Item, KnowledgePoint, Misconception, StrategyCard
from app.contracts.enums import Dimension, ItemPurpose, ItemSource, JudgedBy, Operation
from app.contracts.values import ItemSpec, Prediction
from app.contracts.verify import Equivalence
from app.db import make_engine
from app.knowledge.content import load
from app.knowledge.graph import NxKnowledgeGraph
from app.llm.client import LLMOutputError
from app.main import create_app
from app.verify.tools import SympyMathTools

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


class ScriptedLLM:
    def __init__(self, *replies, approve_reviews=False):
        self.approve_reviews = approve_reviews
        self.replies = list(replies)
        self.calls = []

    async def generate(self, messages, output, tools=None):
        self.calls.append((messages, output, tools))
        if (
            output is ItemReview
            and self.approve_reviews
            and (not self.replies or not isinstance(self.replies[0], ItemReview))
        ):
            return ItemReview(valid=True, reason="预设语义审题通过")
        assert self.replies, "Unexpected model fallback"
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        assert isinstance(reply, output)
        return reply


def draft(number=2, *, wrong=False, predictions=None, spec=True):
    return ItemDraft(
        stem=f"求 $x^{number}$ 的导数并按题意作答",
        spec=ItemSpec(operation=Operation.DERIVATIVE, expr=f"x**{number}", variables=["x"])
        if spec
        else None,
        answer="0" if wrong else f"{number}x^{{{number - 1}}}",
        predictions=predictions or [],
    )


def question(number=2, dimension=Dimension.CORRECTNESS, *, predictions=None, spec=True):
    return Item.model_validate(
        {
            "id": f"seed-{number}-{dimension}",
            "knowledge_point_id": "kp",
            "purpose": ItemPurpose.VERIFICATION,
            "source": ItemSource.SEED,
            "dimension": dimension,
            "rubric": [],
            **draft(number, predictions=predictions, spec=spec).model_dump(),
        }
    )


def request(*dimensions, used=(), misconception="miss", context=None):
    return VerifierInput(
        knowledge_point_id="kp",
        dimensions=list(dimensions or [Dimension.CORRECTNESS]),
        confirmed_misconceptions=[
            ConfirmedMisconception(id=misconception, description="漏乘内层导数")
        ]
        if misconception
        else [],
        used_fingerprints=list(used),
        context_knowledge_point_id=context,
    )


@pytest.fixture
def graph():
    return NxKnowledgeGraph(
        points=[
            KnowledgePoint(id="kp", name="链式法则", lecture=2, section=1),
            KnowledgePoint(
                id="later", name="多元链式法则", lecture=9, section=1, prerequisite_ids=["kp"]
            ),
        ],
        misconceptions=[
            Misconception(
                id="miss", knowledge_point_id="kp", family="chain", description="漏乘内层导数"
            )
        ],
        strategies=[
            StrategyCard(
                id="layer", families=["chain"], rubric=["识别复合层次", "解释内层变化率因子"]
            )
        ],
        items=[question(7), question(8)],
    )


async def test_six_dimensions_and_public_rubric_without_mutating_input(graph):
    llm = ScriptedLLM(
        draft(2),
        draft(3, predictions=[Prediction(misconception_id="miss", answer="x^2")]),
        draft(4),
        draft(5),
        draft(6),
        draft(9),
        approve_reviews=True,
    )
    inp = request(*list(Dimension), context="later")
    before = inp.model_dump()
    verifier = create_verifier(llm=llm, knowledge=graph)
    items = await verifier.compose(inp)
    assert [i.dimension for i in items] == list(Dimension)
    assert len({i.fingerprint for i in items}) == 6
    assert all(i.source == ItemSource.GENERATED for i in items)
    assert all(i.purpose == ItemPurpose.VERIFICATION for i in items)
    assert items[3].rubric == ["识别复合层次", "解释内层变化率因子"]
    assert inp.model_dump() == before
    for messages, output, tools in llm.calls:
        if output is not ItemDraft:
            continue
        payload = json.loads(messages[1].content)
        assert set(payload["input"]) == set(VerifierInput.model_fields)
        assert "student_id" not in payload
        assert tools


@pytest.mark.parametrize("nested", [False, True])
async def test_reject_bypassed_whitelist_before_model_call(graph, nested):
    inp = request()
    if nested:
        inp.confirmed_misconceptions[0] = inp.confirmed_misconceptions[0].model_copy(
            update={"tutor_history": "SECRET"}
        )
    else:
        inp = inp.model_copy(update={"student_id": "SECRET"})
    llm = ScriptedLLM()
    with pytest.raises((ValidationError, ValueError)):
        await create_verifier(llm=llm, knowledge=graph).compose(inp)
    assert not llm.calls


async def test_three_failed_generations_then_unused_seed_rechecked(graph):
    tools = SympyMathTools()
    first, second = graph.seed_items("kp")
    first.fingerprint = "do-not-trust-authored-fingerprint"
    snapshot = [i.model_dump() for i in graph.seed_items("kp")]
    used = tools.fingerprint(first.spec)
    llm = ScriptedLLM(
        draft(wrong=True),
        LLMOutputError("schema exhausted"),
        draft(wrong=True),
        approve_reviews=True,
    )
    items = await create_verifier(llm=llm, knowledge=graph).compose(request(used=[used]))
    assert len(llm.calls) == 4
    assert llm.calls[-1][1] is ItemReview
    assert items[0].spec == second.spec
    assert items[0].fingerprint == tools.fingerprint(second.spec)
    assert items[0].id != second.id
    assert items[0].source == ItemSource.SEED
    assert [i.model_dump() for i in graph.seed_items("kp")] == snapshot
    assert json.loads(llm.calls[2][0][1].content)["failures"]


async def test_duplicate_generation_in_same_batch_cannot_be_relabelled(graph):
    llm = ScriptedLLM(draft(2), draft(2), draft(2), draft(3), approve_reviews=True)
    items = await create_verifier(llm=llm, knowledge=graph).compose(
        request(Dimension.CORRECTNESS, Dimension.RETENTION)
    )
    assert len(llm.calls) == 6
    assert items[0].fingerprint != items[1].fingerprint


@pytest.mark.parametrize("prediction", [None, "2x", r"\frac{", "unknown"])
async def test_antidistractor_quality_never_uses_inappropriate_seed(graph, prediction):
    predictions = (
        []
        if prediction is None
        else [
            Prediction(
                misconception_id="other" if prediction == "unknown" else "miss",
                answer="x" if prediction == "unknown" else prediction,
            )
        ]
    )
    llm = ScriptedLLM(*(draft(2, predictions=predictions) for _ in range(3)))
    with pytest.raises(NoValidItemError, match="distractor"):
        await create_verifier(llm=llm, knowledge=graph).compose(request(Dimension.DISTRACTOR))
    assert len(llm.calls) == 3


async def test_exhausted_seeds_do_not_repeat_or_invent_success(graph):
    tools = SympyMathTools()
    used = [tools.fingerprint(i.spec) for i in graph.seed_items("kp")]
    llm = ScriptedLLM(*(draft(wrong=True) for _ in range(3)))
    with pytest.raises(NoValidItemError):
        await create_verifier(llm=llm, knowledge=graph).compose(request(used=used))


@pytest.mark.parametrize(
    "dimensions,context,misconception",
    [
        ([Dimension.CROSS_CHAPTER], None, "miss"),
        ([Dimension.CROSS_CHAPTER], "kp", "miss"),
        ([Dimension.EXPLANATION], None, None),
        ([Dimension.RETENTION, Dimension.RETENTION], None, "miss"),
    ],
)
async def test_invalid_requests_fail_before_generation(graph, dimensions, context, misconception):
    llm = ScriptedLLM()
    with pytest.raises(ValueError):
        await create_verifier(llm=llm, knowledge=graph).compose(
            request(*dimensions, context=context, misconception=misconception)
        )
    assert not llm.calls


async def test_tool_correct_wrong_and_misconception_without_model(graph):
    verifier = create_verifier(llm=ScriptedLLM(), knowledge=graph)
    item = question(3, predictions=[Prediction(misconception_id="miss", answer="x^2")])
    good = await verifier.judge(item, "3x^2")
    wrong = await verifier.judge(item, "0")
    relapse = await verifier.judge(item, "x^2")
    assert good.is_correct is True and good.matched_misconception_id is None
    assert wrong.is_correct is False and wrong.matched_misconception_id is None
    assert relapse.is_correct is False and relapse.matched_misconception_id == "miss"
    assert all(j.judged_by == JudgedBy.TOOL and j.reason for j in [good, wrong, relapse])


async def test_antiderivative_constants_use_spec_mode(graph):
    item = question()
    item.spec = ItemSpec(operation=Operation.INDEFINITE_INTEGRAL, expr="2*x", variables=["x"])
    item.answer = "x^2"
    result = await create_verifier(llm=ScriptedLLM(), knowledge=graph).judge(item, "x^2+7")
    assert result.is_correct is True and result.judged_by == JudgedBy.TOOL


@pytest.mark.parametrize(
    "scores,overall,expected",
    [
        ([True, True], True, True),
        ([True, False], True, False),
        ([True], True, None),
        ([True, None], True, None),
        ([True, True], None, None),
    ],
)
async def test_explanation_is_model_graded_by_every_strategy_criterion(
    graph, scores, overall, expected
):
    item = question(2, Dimension.EXPLANATION)
    item.rubric = ["识别复合层次", "解释内层变化率因子"]
    llm = ScriptedLLM(
        ModelReview(
            is_correct=overall,
            rubric_results=scores,
            matched_misconception_id="INVENTED",
            reason="按要点评阅",
        )
    )
    result = await create_verifier(llm=llm, knowledge=graph).judge(item, "2x")
    assert result.is_correct is expected
    assert result.judged_by == JudgedBy.MODEL
    assert result.matched_misconception_id is None
    assert json.loads(llm.calls[0][0][1].content)["rubric"] == item.rubric


async def test_parse_failure_model_fallback_and_prediction_allowlist(graph):
    llm = ScriptedLLM(
        ModelReview(
            is_correct=False, matched_misconception_id="miss", reason="文字作答描述了漏乘因子"
        )
    )
    item = question(predictions=[Prediction(misconception_id="miss", answer="x")])
    result = await create_verifier(llm=llm, knowledge=graph).judge(item, "我只对外层求导")
    assert result.is_correct is False
    assert result.matched_misconception_id == "miss"
    assert result.judged_by == JudgedBy.MODEL


async def test_model_cannot_overturn_known_tool_result(graph):
    class PartialTools(SympyMathTools):
        def check_equivalent(self, a, b, mode, variables):
            if b == "unparseable":
                return Equivalence(equivalent=None, method="none")
            return super().check_equivalent(a, b, mode, variables)

    item = question(predictions=[Prediction(misconception_id="miss", answer="unparseable")])
    llm = ScriptedLLM(ModelReview(is_correct=True, reason="模型声称正确"))
    result = await create_verifier(llm=llm, knowledge=graph, tools=PartialTools()).judge(item, "0")
    assert result.is_correct is False
    assert result.judged_by == JudgedBy.MODEL
    assert json.loads(llm.calls[0][0][1].content)["tool_is_correct"] is False


async def test_noncomputational_item_requires_separate_positive_review(graph):
    generated = ItemDraft(
        stem="举出通项趋零但级数发散的反例并说明理由",
        spec=None,
        answer="调和级数：通项趋零，以分组法说明部分和无界",
    )
    llm = ScriptedLLM(
        generated,
        ItemReview(valid=None, reason="无法确认"),
        generated,
        ItemReview(valid=True, reason="调和级数确实反驳必要条件充分化"),
    )
    (item,) = await create_verifier(llm=llm, knowledge=graph).compose(
        request(Dimension.EXPLANATION)
    )
    assert item.spec is None and item.fingerprint
    assert len(llm.calls) == 4
    assert llm.calls[1][1] is ItemReview
    (partial_sum_tool,) = llm.calls[1][2]
    value = partial_sum_tool.handler(partial_sum_tool.args(expr="1/n", variable="n", terms=3))
    assert value["partial_sum"] == pytest.approx(11 / 6)


async def test_unreviewable_seed_is_rejected_after_generation_failure(graph):
    graph._items = [question(7, spec=False)]
    llm = ScriptedLLM(
        *(draft(wrong=True) for _ in range(3)),
        ItemReview(valid=False, reason="种子题没有明确数学对象"),
    )
    with pytest.raises(NoValidItemError):
        await create_verifier(llm=llm, knowledge=graph).compose(request())
    assert llm.calls[-1][1] is ItemReview


@pytest.mark.parametrize(
    "misconception_id", ["calc.ch2.chain.miss_inner", "calc.ch2.chain.formula_sub"]
)
async def test_real_content_A_B_independent_verification_produces_initial_dimensions(
    misconception_id,
):
    content = load(sorted((Path(__file__).parents[2] / "content" / "lectures").glob("*.yaml")))
    graph = NxKnowledgeGraph(
        list(content.points.values()),
        list(content.misconceptions.values()),
        list(content.strategies.values()),
        list(content.items.values()),
    )
    llm = ScriptedLLM(
        ItemDraft(
            stem=r"求 $\sin^3(2x)$ 的导数",
            spec=ItemSpec(operation=Operation.DERIVATIVE, expr="sin(2*x)**3", variables=["x"]),
            answer=r"6\sin^2(2x)\cos(2x)",
        ),
        ItemDraft(
            stem=r"求 $\cos^2(x^2)$ 的导数",
            spec=ItemSpec(operation=Operation.DERIVATIVE, expr="cos(x**2)**2", variables=["x"]),
            answer=r"-4x\cos(x^2)\sin(x^2)",
            predictions=[
                Prediction(misconception_id=misconception_id, answer=r"-2\cos(x^2)\sin(x^2)")
            ],
        ),
        ItemDraft(
            stem=r"求 $\sin(e^x)$ 的导数，先识别不同层次的复合关系",
            spec=ItemSpec(operation=Operation.DERIVATIVE, expr="sin(exp(x))", variables=["x"]),
            answer=r"e^x\cos(e^x)",
        ),
        approve_reviews=True,
    )
    verifier = create_verifier(llm=llm, knowledge=graph)
    inp = VerifierInput(
        knowledge_point_id="calc.ch2.chain_rule_multi",
        dimensions=[Dimension.CORRECTNESS, Dimension.DISTRACTOR, Dimension.NEAR_TRANSFER],
        confirmed_misconceptions=[
            ConfirmedMisconception(
                id=misconception_id, description=graph.misconception(misconception_id).description
            )
        ],
        used_fingerprints=[],
    )
    items = await verifier.compose(inp)
    results = [await verifier.judge(item, item.answer) for item in items]
    assert all(r.is_correct and r.judged_by == JudgedBy.TOOL for r in results)
    assert {i.dimension for i in items} == {
        Dimension.CORRECTNESS,
        Dimension.DISTRACTOR,
        Dimension.NEAR_TRANSFER,
    }
    # These are evidence inputs for T19, not permission for this agent to mutate mastery.
    assert not hasattr(verifier, "session")


async def test_tool_unknown_generation_requires_model_review(graph):
    from app.contracts.verify import ItemCheck

    class UnsupportedTools(SympyMathTools):
        def verify_item(self, spec, answer):
            return ItemCheck(ok=None, detail="工具不支持该题型")

    llm = ScriptedLLM(draft(2), ItemReview(valid=True, reason="独立审题确认答案"))
    (item,) = await create_verifier(llm=llm, knowledge=graph, tools=UnsupportedTools()).compose(
        request()
    )
    assert item.fingerprint
    assert [call[1] for call in llm.calls] == [ItemDraft, ItemReview]


async def test_text_fingerprint_rejects_whitespace_variant(graph):
    original = ItemDraft(stem="解释调和级数的分组反例", answer="分组可见部分和无界")
    llm = ScriptedLLM(original, ItemReview(valid=True, reason="反例有效"))
    (item,) = await create_verifier(llm=llm, knowledge=graph).compose(
        request(Dimension.EXPLANATION)
    )
    variant = original.model_copy(update={"stem": " 解释 调和级数的分组反例\n"})
    llm = ScriptedLLM(*(variant for _ in range(3)))
    with pytest.raises(NoValidItemError):
        await create_verifier(llm=llm, knowledge=graph).compose(
            request(Dimension.EXPLANATION, used=[item.fingerprint])
        )


async def test_cross_chapter_seed_comes_from_context_and_attributes_target(graph):
    seed = question(7, Dimension.CROSS_CHAPTER)
    seed.knowledge_point_id = "later"
    graph._items = [seed]
    snapshot = seed.model_dump()
    llm = ScriptedLLM(*(draft(wrong=True) for _ in range(3)), approve_reviews=True)
    (item,) = await create_verifier(llm=llm, knowledge=graph).compose(
        request(Dimension.CROSS_CHAPTER, context="later")
    )
    assert item.knowledge_point_id == "kp" and item.source == ItemSource.SEED
    assert seed.model_dump() == snapshot


@pytest.mark.parametrize("explicit", [False, True])
async def test_application_assembles_verifier_and_owns_client(monkeypatch, tmp_path, explicit):
    from app.agents.verifier import IndependentVerifier

    clients = []

    class Client:
        def __init__(self, settings):
            self.closed = False
            clients.append(self)

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr("app.main.get_settings", lambda: Settings(llm_api_key="test-only"))
    monkeypatch.setattr("app.main.OpenAICompatClient", Client)
    engine = make_engine(f"sqlite:///{tmp_path / 'business.db'}")
    app = create_app(
        engine=engine,
        checkpoint_path=str(tmp_path / "checkpoints.db"),
        orchestration_dependencies={"verifier": None} if explicit else None,
    )
    try:
        async with app.router.lifespan_context(app):
            verifier = app.state.orchestrator.verifier
            if explicit:
                assert verifier is None and not clients
            else:
                assert isinstance(verifier, IndependentVerifier)
                assert verifier.llm is clients[0] and not clients[0].closed
        assert all(client.closed for client in clients)
    finally:
        engine.dispose()


@pytest.mark.parametrize("cross_chapter", [False, True])
async def test_verified_math_still_requires_independent_semantic_review(graph, cross_chapter):
    # Both candidates pass SymPy: the failure is in the question's meaning/context.
    candidate = draft(2)
    if cross_chapter:
        dimension = Dimension.CROSS_CHAPTER
        context = "later"
        rejection = "只是 x² 的一元求导，没有多元链式法则的后续章节情境"
    else:
        dimension = Dimension.CORRECTNESS
        context = None
        candidate = candidate.model_copy(update={"stem": "求 x^3 的导数"})
        rejection = "题干求 x³ 的导数，spec 却为 x²，二者不一致"
    assert SympyMathTools().verify_item(candidate.spec, candidate.answer).ok is True
    graph._items = []
    llm = ScriptedLLM(
        *(
            reply
            for _ in range(3)
            for reply in [candidate, ItemReview(valid=False, reason=rejection)]
        )
    )
    with pytest.raises(NoValidItemError):
        await create_verifier(llm=llm, knowledge=graph).compose(request(dimension, context=context))
    reviews = [call for call in llm.calls if call[1] is ItemReview]
    assert len(reviews) == 3
    for messages, _, _ in reviews:
        # Fresh review: no drafts, retry reasons, or generating conversation included.
        assert [message.role for message in messages] == ["system", "user"]
        payload = json.loads(messages[1].content)
        assert "failures" not in payload and "input" not in payload
        assert payload["tool_check"]["ok"] is True
        assert payload["tool_check"]["computed"] == "2 x"
        assert payload["knowledge_point"] == {"id": "kp", "name": "链式法则", "lecture": 2}
        assert payload["stem"] == candidate.stem
        assert payload["spec"]["expr"] == "x**2"
        assert payload["dimension"] == dimension.value
        if cross_chapter:
            assert payload["context"] == {"id": "later", "name": "多元链式法则", "lecture": 9}
        else:
            assert payload["context"] is None


async def test_wrong_math_cannot_be_rescued_by_positive_semantic_review(graph):
    graph._items = []
    llm = ScriptedLLM(
        *(draft(wrong=True) for _ in range(3)), ItemReview(valid=True, reason="不应调用该审查")
    )
    with pytest.raises(NoValidItemError):
        await create_verifier(llm=llm, knowledge=graph).compose(request())
    assert [call[1] for call in llm.calls] == [ItemDraft] * 3
    assert len(llm.replies) == 1


async def test_verified_seed_is_also_rejected_for_semantic_mismatch(graph):
    seed = question(2)
    seed.stem = "求 x^3 的导数"
    graph._items = [seed]
    llm = ScriptedLLM(
        *(draft(wrong=True) for _ in range(3)),
        ItemReview(valid=False, reason="种子题题干与 spec 不一致"),
    )
    with pytest.raises(NoValidItemError):
        await create_verifier(llm=llm, knowledge=graph).compose(request())
    assert llm.calls[-1][1] is ItemReview
    assert seed.fingerprint is None
