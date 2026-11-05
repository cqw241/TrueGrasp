"""知识图谱与教学内容的查询契约，由 app.knowledge 实现（T04）。

边的方向是“先修 → 后续”。返回的实体是只读的内容数据，不要修改后写回。
"""

from typing import Protocol

from app.contracts.entities import Item, KnowledgePoint, Misconception, StrategyCard


class KnowledgeGraph(Protocol):
    def point(self, kp_id: str) -> KnowledgePoint: ...

    def lecture_points(self, lecture: int) -> list[KnowledgePoint]:
        """某一讲的全部知识点，按节排序。"""
        ...

    def prerequisites(self, kp_id: str) -> list[KnowledgePoint]:
        """直接先修知识点。章前预检按这个检查。"""
        ...

    def ancestors(self, kp_id: str) -> list[KnowledgePoint]:
        """全部间接先修知识点，由近到远，用于沿先修图回退。"""
        ...

    def dependents(self, kp_id: str) -> list[KnowledgePoint]:
        """直接后续知识点。"""
        ...

    def path(self, source: str, target: str) -> list[KnowledgePoint]:
        """从先修 source 到后续 target 的一条最短路径，含两端；不连通时为空。

        如第 1→2→9 讲：函数复合 → 一元链式法则 → 多元复合求导。
        """
        ...

    def misconceptions(self, kp_id: str) -> list[Misconception]:
        """该知识点的错因，含已入库的未建模错因。"""
        ...

    def misconception(self, misconception_id: str) -> Misconception: ...

    def strategies(self, misconception_id: str) -> list[StrategyCard]:
        """适用于该错因的策略卡：直接关联的在前，按错因族复用的在后。"""
        ...

    def seed_items(self, kp_id: str) -> list[Item]: ...
