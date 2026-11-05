"""按错因用模型起草判别追问题，交教师审核（技术文档第 6 节）。

起草的题目必须通过 ``verify_item`` 和 ``check_distinct``，并且目标错因的预测不同于正确答案；
不通过时把原因反馈给模型重试，最多 3 次。通过的题目导出为 YAML，教师审核后移入讲次文件。
"""

from dataclasses import dataclass

from pydantic import BaseModel, Field

from app.contracts.entities import Item
from app.contracts.enums import ItemPurpose, ItemSource
from app.contracts.llm import ChatMessage, LLMClient
from app.contracts.values import SLIP, ItemSpec, LaTeX, Prediction
from app.contracts.verify import MathTools
from app.knowledge.content import Content

MAX_ATTEMPTS = 3

SYSTEM_PROMPT = """你是高等数学教师，为错因诊断设计判别追问题。
判别追问题是一道短小的计算题：持有不同错因的学生会写出不同的答案，学生的答案能证伪部分错因假设。
要求：
- 题目考查同一知识点，比原题更简单或结构更清楚，不要求学生写过程；
- spec 是题目的结构化描述，表达式用 SymPy 语法（如 cos(5*x)**2），程序会按它重算标准答案；
- answer 和各预测答案用 LaTeX；
- predictions 给出目标错因和每个相关错因下学生会写出的答案，不要给偶发失误（slip）写预测，
  它的预测就是正确答案；
- 目标错因的预测答案必须不同于正确答案。"""


class ProbeDraft(BaseModel):
    stem: str = Field(description="题干，公式用 LaTeX")
    spec: ItemSpec
    answer: LaTeX
    predictions: list[Prediction]
    rationale: str = Field(description="为什么这道题能区分这些错因")


@dataclass
class DraftResult:
    item: Item | None
    rationale: str
    failures: list[str]


def _prompt(content: Content, misconception_id: str, failures: list[str]) -> str:
    target = content.misconceptions[misconception_id]
    point = content.points[target.knowledge_point_id]
    siblings = [
        m
        for m in content.misconceptions.values()
        if m.knowledge_point_id == target.knowledge_point_id and m.id != target.id
    ]
    lines = [
        f"知识点：{point.name}（第 {point.lecture} 讲第 {point.section} 节）",
        f"目标错因 {target.id}：{target.description}",
    ]
    lines += [
        f"  典型错例：{e.question}，错答 {e.wrong}，正确 {e.correct}" for e in target.examples
    ]
    if siblings:
        lines.append("同一知识点的其他错因，能区分时一并给出预测：")
        lines += [f"- {m.id}：{m.description}" for m in siblings]
    if failures:
        lines.append("之前的草稿没有通过检查，原因如下，请换一道题：")
        lines += [f"- {f}" for f in failures]
    return "\n".join(lines)


def _review(content: Content, misconception_id: str, draft: ProbeDraft, tools: MathTools) -> str:
    """返回不通过的原因，通过时返回空字符串。"""
    allowed = {
        m.id
        for m in content.misconceptions.values()
        if m.knowledge_point_id == content.misconceptions[misconception_id].knowledge_point_id
    }
    ids = [p.misconception_id for p in draft.predictions]
    if misconception_id not in ids:
        return f"缺少目标错因 {misconception_id} 的预测答案"
    if unknown := set(ids) - allowed:
        return f"预测中出现了不相关的错因：{', '.join(sorted(unknown))}"
    if SLIP in ids:
        return "不要给偶发失误写预测"
    checked = tools.verify_item(draft.spec, draft.answer)
    if not checked.ok:
        return f"标准答案 {draft.answer} 与按 spec 重算的结果 {checked.computed} 不一致"
    distinct = tools.check_distinct(draft.spec, draft.answer, draft.predictions)
    if not distinct.ok:
        return f"各预测答案无法区分：{distinct.detail}"
    if not distinct.separates(misconception_id):
        return "目标错因的预测答案与正确答案等价"
    return ""


async def draft_probe(
    content: Content, misconception_id: str, llm: LLMClient, tools: MathTools
) -> DraftResult:
    target = content.misconceptions[misconception_id]
    failures: list[str] = []
    for _ in range(MAX_ATTEMPTS):
        draft = await llm.generate(
            [
                ChatMessage(role="system", content=SYSTEM_PROMPT),
                ChatMessage(role="user", content=_prompt(content, misconception_id, failures)),
            ],
            ProbeDraft,
        )
        if reason := _review(content, misconception_id, draft, tools):
            failures.append(reason)
            continue
        number = len(target.probe_item_ids) + 1
        item = Item(
            id=f"{misconception_id}.probe{number}",
            knowledge_point_id=target.knowledge_point_id,
            purpose=ItemPurpose.PROBE,
            stem=draft.stem,
            spec=draft.spec,
            answer=draft.answer,
            predictions=draft.predictions,
            fingerprint=tools.fingerprint(draft.spec),
            source=ItemSource.SEED,
        )
        return DraftResult(item, draft.rationale, failures)
    return DraftResult(None, "", failures)
