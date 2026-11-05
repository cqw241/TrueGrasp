"""教学内容的 YAML 格式、加载与入库检查（技术文档第 6 节）。

内容放在仓库根目录的 ``content/lectures/``，每讲一个文件，文件里有 ``sections``、``points``、
``misconceptions``、``strategies``、``items`` 五部分，除 ``lecture`` 外都可以省略。
ID 全书唯一，可以引用其他讲的内容。字段说明见 ``content/README.md``。
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import networkx as nx
import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from app.contracts.entities import Item, KnowledgePoint, Misconception, StrategyCard
from app.contracts.enums import ContentSource, Dimension, ExamTag, ItemPurpose, ItemSource
from app.contracts.values import SLIP, ItemSpec, LaTeX, MisconceptionExample, Prediction
from app.contracts.verify import MathTools


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SectionDoc(_Strict):
    section: int
    title: str
    exam_tags: list[ExamTag] = [ExamTag.MATH1, ExamTag.MATH2, ExamTag.MATH3]


class PointDoc(_Strict):
    id: str
    section: int
    name: str
    prerequisites: list[str] = []
    exam_tags: list[ExamTag] | None = None  # 省略时沿用所在节的标签


class MisconceptionDoc(_Strict):
    id: str
    knowledge_point: str
    family: str
    description: str
    triggers: list[str] = []
    examples: list[MisconceptionExample] = []
    probes: list[str] = []  # 判别追问题的题目 ID
    strategies: list[str] = []
    recurs_in: list[str] = []


class StrategyDoc(_Strict):
    id: str
    misconceptions: list[str] = []
    families: list[str] = []
    principles: list[str]
    constraints: list[str] = []
    rubric: list[str] = []


class ItemDoc(_Strict):
    id: str
    knowledge_point: str
    purpose: Literal[ItemPurpose.PROBE, ItemPurpose.PRACTICE, ItemPurpose.VERIFICATION]
    dimension: Dimension | None = None
    stem: str
    spec: ItemSpec | None = None
    answer: LaTeX
    predictions: list[Prediction] = []
    rubric: list[str] = []


class LectureDoc(_Strict):
    lecture: int
    title: str = ""
    sections: list[SectionDoc] = []
    points: list[PointDoc] = []
    misconceptions: list[MisconceptionDoc] = []
    strategies: list[StrategyDoc] = []
    items: list[ItemDoc] = []


@dataclass(frozen=True)
class Issue:
    file: str
    ref: str  # 出错的条目 ID，文件级错误为空
    message: str
    level: Literal["error", "notice"] = "error"

    def __str__(self) -> str:
        where = f"{self.file} {self.ref}".strip()
        return f"[{self.level}] {where}：{self.message}"


@dataclass
class Content:
    """一批内容转换成的实体。``origin`` 记录每个 ID 来自哪个文件，用于报错。"""

    points: dict[str, KnowledgePoint] = field(default_factory=dict)
    misconceptions: dict[str, Misconception] = field(default_factory=dict)
    strategies: dict[str, StrategyCard] = field(default_factory=dict)
    items: dict[str, Item] = field(default_factory=dict)
    sections: dict[tuple[int, int], SectionDoc] = field(default_factory=dict)
    origin: dict[str, str] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "error"]

    def _issue(self, ref: str, message: str, level: Literal["error", "notice"] = "error"):
        self.issues.append(Issue(self.origin.get(ref, ""), ref, message, level))


def load(paths: Iterable[Path]) -> Content:
    """读取 YAML 文件并转换为实体，同时做格式检查和引用检查，不调用核验工具。"""
    content = Content()
    for path in sorted(paths):
        try:
            doc = LectureDoc.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (yaml.YAMLError, ValidationError) as exc:
            content.issues.append(Issue(path.name, "", f"格式错误：{exc}"))
            continue
        _add(content, path.name, doc)
    _check_references(content)
    return content


def load_dir(directory: Path) -> Content:
    paths = list(directory.glob("*.yaml"))
    if not paths:
        content = Content()
        state = "没有 YAML 文件" if directory.is_dir() else "不存在"
        content.issues.append(Issue(str(directory), "", f"内容目录{state}"))
        return content
    return load(paths)


def _register(content: Content, table: dict, file: str, key: str, value: object) -> bool:
    if key in table:
        content.issues.append(Issue(file, key, f"ID 重复，已在 {content.origin[key]} 中定义"))
        return False
    table[key] = value
    content.origin[key] = file
    return True


def _add(content: Content, file: str, doc: LectureDoc) -> None:
    for s in doc.sections:
        key = (doc.lecture, s.section)
        if key in content.sections:
            content.issues.append(Issue(file, f"第 {s.section} 节", "节号重复"))
        content.sections[key] = s

    for p in doc.points:
        section = content.sections.get((doc.lecture, p.section))
        if section is None:
            content.issues.append(Issue(file, p.id, f"第 {doc.lecture} 讲没有第 {p.section} 节"))
            continue
        point = KnowledgePoint(
            id=p.id,
            lecture=doc.lecture,
            section=p.section,
            name=p.name,
            prerequisite_ids=p.prerequisites,
            exam_tags=p.exam_tags if p.exam_tags is not None else section.exam_tags,
        )
        _register(content, content.points, file, p.id, point)

    for m in doc.misconceptions:
        mis = Misconception(
            id=m.id,
            knowledge_point_id=m.knowledge_point,
            family=m.family,
            description=m.description,
            triggers=m.triggers,
            examples=m.examples,
            probe_item_ids=m.probes,
            strategy_ids=m.strategies,
            recurs_in=m.recurs_in,
            source=ContentSource.TEACHER,
        )
        _register(content, content.misconceptions, file, m.id, mis)

    for s in doc.strategies:
        if not s.misconceptions and not s.families:
            content.issues.append(Issue(file, s.id, "策略卡至少要关联一个错因或错因族"))
        card = StrategyCard(
            id=s.id,
            misconception_ids=s.misconceptions,
            families=s.families,
            principles=s.principles,
            constraints=s.constraints,
            rubric=s.rubric,
        )
        _register(content, content.strategies, file, s.id, card)

    for i in doc.items:
        if i.purpose == ItemPurpose.PROBE and (i.spec is None or not i.predictions):
            content.issues.append(Issue(file, i.id, "判别追问题需要 spec 和 predictions"))
        if (i.purpose == ItemPurpose.VERIFICATION) != (i.dimension is not None):
            content.issues.append(Issue(file, i.id, "只有验证题填写 dimension，且必须填写"))
        item = Item(
            id=i.id,
            knowledge_point_id=i.knowledge_point,
            purpose=i.purpose,
            dimension=i.dimension,
            stem=i.stem,
            spec=i.spec,
            answer=i.answer,
            predictions=i.predictions,
            rubric=i.rubric,
            source=ItemSource.SEED,
        )
        _register(content, content.items, file, i.id, item)


def _check_references(content: Content) -> None:
    def missing(ref: str, kind: str, ids: Iterable[str], table: dict) -> None:
        for target in ids:
            if target not in table:
                content._issue(ref, f"引用的{kind}不存在：{target}")

    graph = nx.DiGraph()
    for p in content.points.values():
        missing(p.id, "先修知识点", p.prerequisite_ids, content.points)
        graph.add_node(p.id)
        graph.add_edges_from((pre, p.id) for pre in p.prerequisite_ids if pre in content.points)
    for cycle in nx.simple_cycles(graph):
        content._issue(cycle[0], f"先修关系成环：{' → '.join([*cycle, cycle[0]])}")

    for m in content.misconceptions.values():
        missing(m.id, "知识点", [m.knowledge_point_id], content.points)
        missing(m.id, "知识点", m.recurs_in, content.points)
        missing(m.id, "策略卡", m.strategy_ids, content.strategies)
        for probe_id in m.probe_item_ids:
            probe = content.items.get(probe_id)
            if probe is None:
                content._issue(m.id, f"引用的追问题不存在：{probe_id}")
            elif probe.purpose != ItemPurpose.PROBE:
                content._issue(m.id, f"{probe_id} 不是判别追问题")
            elif m.id not in {pred.misconception_id for pred in probe.predictions}:
                content._issue(probe_id, f"缺少错因 {m.id} 的预测答案")

    for s in content.strategies.values():
        missing(s.id, "错因", s.misconception_ids, content.misconceptions)

    for i in content.items.values():
        missing(i.id, "知识点", [i.knowledge_point_id], content.points)
        ids = [p.misconception_id for p in i.predictions if p.misconception_id != SLIP]
        missing(i.id, "错因", ids, content.misconceptions)


def check_math(content: Content, tools: MathTools) -> None:
    """用核验工具验算种子题、检查追问题的区分度，并为有 spec 的题目生成指纹。"""
    try:
        for item in content.items.values():
            if item.spec is None:
                continue
            item.fingerprint = tools.fingerprint(item.spec)
            checked = tools.verify_item(item.spec, item.answer)
            if checked.ok is None:
                content._issue(item.id, f"核验工具无法重算标准答案：{checked.detail}")
            elif not checked.ok:
                content._issue(item.id, f"标准答案与工具重算结果 {checked.computed} 不一致")
            if item.purpose == ItemPurpose.PROBE and item.predictions:
                distinct = tools.check_distinct(item.spec, item.answer, item.predictions)
                if not distinct.ok:
                    content._issue(item.id, f"追问题没有区分度：{distinct.detail}")
    except NotImplementedError:
        content.issues.append(
            Issue("", "", "核验工具尚未实现（T05），跳过 verify_item 与 check_distinct", "notice")
        )
