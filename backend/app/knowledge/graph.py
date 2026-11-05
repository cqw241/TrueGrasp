"""知识图谱（T04），实现 app.contracts.knowledge.KnowledgeGraph。

内容入库后从业务库加载，用 NetworkX 建图，边的方向是“先修 → 后续”。
"""

from app.contracts.entities import Item, KnowledgePoint, Misconception, StrategyCard


class NxKnowledgeGraph:
    def point(self, kp_id: str) -> KnowledgePoint:
        raise NotImplementedError("T04")

    def lecture_points(self, lecture: int) -> list[KnowledgePoint]:
        raise NotImplementedError("T04")

    def prerequisites(self, kp_id: str) -> list[KnowledgePoint]:
        raise NotImplementedError("T04")

    def ancestors(self, kp_id: str) -> list[KnowledgePoint]:
        raise NotImplementedError("T04")

    def dependents(self, kp_id: str) -> list[KnowledgePoint]:
        raise NotImplementedError("T04")

    def path(self, source: str, target: str) -> list[KnowledgePoint]:
        raise NotImplementedError("T04")

    def misconceptions(self, kp_id: str) -> list[Misconception]:
        raise NotImplementedError("T04")

    def misconception(self, misconception_id: str) -> Misconception:
        raise NotImplementedError("T04")

    def strategies(self, misconception_id: str) -> list[StrategyCard]:
        raise NotImplementedError("T04")

    def seed_items(self, kp_id: str) -> list[Item]:
        raise NotImplementedError("T04")
