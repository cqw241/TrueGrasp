"""知识图谱（T04），实现 app.contracts.knowledge.KnowledgeGraph。

内容入库后从业务库加载，用 NetworkX 建图，边的方向是“先修 → 后续”。
加载后不再读库；教师审核通过的错因通过 ``add_adopted_misconception`` 更新共享图谱。
"""

from collections import deque

import networkx as nx
from sqlalchemy import Engine
from sqlmodel import Session, select

from app.contracts.entities import Item, KnowledgePoint, Misconception, StrategyCard
from app.contracts.enums import ItemSource


class NxKnowledgeGraph:
    def __init__(
        self,
        points: list[KnowledgePoint],
        misconceptions: list[Misconception],
        strategies: list[StrategyCard],
        items: list[Item],
    ) -> None:
        self._points = {p.id: p for p in points}
        self._misconceptions = {m.id: m for m in misconceptions}
        self._strategies = list(strategies)
        self._items = list(items)
        self._graph = nx.DiGraph()
        self._graph.add_nodes_from(self._points)
        for p in points:
            self._graph.add_edges_from((pre, p.id) for pre in p.prerequisite_ids)

    @classmethod
    def from_engine(cls, engine: Engine) -> "NxKnowledgeGraph":
        with Session(engine) as session:
            return cls(
                list(session.exec(select(KnowledgePoint))),
                list(session.exec(select(Misconception))),
                list(session.exec(select(StrategyCard))),
                list(session.exec(select(Item).where(Item.source == ItemSource.SEED))),
            )

    def _sorted(self, ids) -> list[KnowledgePoint]:
        points = [self._points[i] for i in ids]
        return sorted(points, key=lambda p: (p.lecture, p.section, p.id))

    def __contains__(self, kp_id: str) -> bool:
        return kp_id in self._points

    def all_points(self) -> list[KnowledgePoint]:
        return self._sorted(self._points)

    def point(self, kp_id: str) -> KnowledgePoint:
        return self._points[kp_id]

    def lecture_points(self, lecture: int) -> list[KnowledgePoint]:
        return self._sorted(i for i, p in self._points.items() if p.lecture == lecture)

    def prerequisites(self, kp_id: str) -> list[KnowledgePoint]:
        return self._sorted(self._graph.predecessors(kp_id))

    def ancestors(self, kp_id: str) -> list[KnowledgePoint]:
        """沿先修边逆向广度优先，距离相同时按讲、节排序。"""
        seen = {kp_id}
        order: list[KnowledgePoint] = []
        frontier = deque([kp_id])
        while frontier:
            layer = [p for p in self.prerequisites(frontier.popleft()) if p.id not in seen]
            for p in layer:
                seen.add(p.id)
                order.append(p)
                frontier.append(p.id)
        return order

    def dependents(self, kp_id: str) -> list[KnowledgePoint]:
        return self._sorted(self._graph.successors(kp_id))

    def path(self, source: str, target: str) -> list[KnowledgePoint]:
        try:
            ids = nx.shortest_path(self._graph, source, target)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return []
        return [self._points[i] for i in ids]

    def misconceptions(self, kp_id: str) -> list[Misconception]:
        return [m for m in self._misconceptions.values() if m.knowledge_point_id == kp_id]

    def add_adopted_misconception(self, misconception: Misconception) -> None:
        self._misconceptions[misconception.id] = misconception

    def misconception(self, misconception_id: str) -> Misconception:
        return self._misconceptions[misconception_id]

    def strategies(self, misconception_id: str) -> list[StrategyCard]:
        mis = self._misconceptions[misconception_id]
        direct = [
            s
            for s in self._strategies
            if s.id in mis.strategy_ids or misconception_id in s.misconception_ids
        ]
        by_family = [s for s in self._strategies if mis.family in s.families and s not in direct]
        return direct + by_family

    def seed_items(self, kp_id: str) -> list[Item]:
        return [i for i in self._items if i.knowledge_point_id == kp_id]
