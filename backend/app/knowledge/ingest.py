"""把通过检查的内容写入业务库。只新增或更新，不删除，避免破坏证据对题目和知识点的引用。"""

from sqlmodel import Session, select

from app.contracts.entities import Evidence, Item, KnowledgePoint, Misconception, StrategyCard
from app.contracts.enums import ContentSource, ItemSource
from app.knowledge.content import Content, Issue
from app.orchestration.storage import ItemExposure, init_storage

# 改动这些字段会改变题目本身；证据只记题目 ID，已被引用的题目不能改
_ITEM_FIELDS = {
    "knowledge_point_id",
    "purpose",
    "dimension",
    "stem",
    "spec",
    "answer",
    "predictions",
}


class IngestError(ValueError):
    def __init__(self, issues: list[Issue]) -> None:
        super().__init__("\n".join(map(str, issues)))
        self.issues = issues


def _changed_referenced_items(session: Session, content: Content) -> list[Issue]:
    referenced = set(session.exec(select(Evidence.item_id).where(Evidence.item_id.is_not(None))))
    referenced |= set(session.exec(select(ItemExposure.item_id)))
    issues = []
    for item_id in sorted(referenced & set(content.items)):
        stored = session.get(Item, item_id)
        new = content.items[item_id]
        if stored.model_dump(include=_ITEM_FIELDS) != new.model_dump(include=_ITEM_FIELDS):
            issues.append(
                Issue(
                    content.origin.get(item_id, ""),
                    item_id,
                    "题目已被学生作答或展示，不能修改内容；请改用新的题目 ID",
                )
            )
    return issues


def ingest(session: Session, content: Content) -> list[Issue]:
    """写入内容，返回库中存在、但这批内容里已经没有的教师内容 ID，供人工确认。

    已被证据引用或已向学生展示的题目，内容改动时整批拒绝入库，抛出 IngestError。
    """
    if content.errors:
        raise IngestError(content.errors)
    init_storage(session.get_bind())
    if conflicts := _changed_referenced_items(session, content):
        raise IngestError(conflicts)
    for table in (content.points, content.misconceptions, content.strategies, content.items):
        for entity in table.values():
            session.merge(entity)
        session.flush()

    notices = []
    stale_queries = [
        (KnowledgePoint, select(KnowledgePoint.id), content.points),
        (
            Misconception,
            select(Misconception.id).where(Misconception.source == ContentSource.TEACHER),
            content.misconceptions,
        ),
        (StrategyCard, select(StrategyCard.id), content.strategies),
        (Item, select(Item.id).where(Item.source == ItemSource.SEED), content.items),
    ]
    for entity, query, current in stale_queries:
        for stale in sorted(set(session.exec(query)) - set(current)):
            notices.append(
                Issue("", stale, f"{entity.__tablename__} 已不在内容文件中，未删除", "notice")
            )
    return notices
