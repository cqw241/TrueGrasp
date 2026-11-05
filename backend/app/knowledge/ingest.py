"""把通过检查的内容写入业务库。只新增或更新，不删除，避免破坏证据对题目和知识点的引用。"""

from sqlmodel import Session, select

from app.contracts.entities import Item, KnowledgePoint, Misconception, StrategyCard
from app.contracts.enums import ContentSource, ItemSource
from app.knowledge.content import Content, Issue


def ingest(session: Session, content: Content) -> list[Issue]:
    """写入内容，返回库中存在、但这批内容里已经没有的教师内容 ID，供人工确认。"""
    if content.errors:
        raise ValueError("内容未通过检查，不能入库")
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
