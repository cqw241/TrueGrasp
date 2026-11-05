"""数学核验工具的契约（技术文档第 3 节），由 app.verify 实现（T05）。

能用工具判定的题目，以工具结果为准；工具判不了时返回 None，由调用方改为模型评阅，
并把证据的判定方式记为 JudgedBy.MODEL。
"""

from typing import Literal, Protocol

from pydantic import BaseModel

from app.contracts.enums import EquivalenceMode
from app.contracts.values import ItemSpec, LaTeX, Prediction


class Equivalence(BaseModel):
    equivalent: bool | None  # None 表示解析失败或无法判定
    method: Literal["symbolic", "numeric", "none"]
    detail: str = ""


class ItemCheck(BaseModel):
    ok: bool | None  # 重算结果与模型给出的答案是否一致；None 表示工具算不出
    computed: LaTeX | None = None  # 工具重算的标准答案
    detail: str = ""


class Distinctness(BaseModel):
    ok: bool | None
    # 互相等价的一对：错因 ID 或 "correct"（正确答案）
    collisions: list[tuple[str, str]] = []
    detail: str = ""


class MathTools(Protocol):
    def check_equivalent(
        self, a: LaTeX, b: LaTeX, mode: EquivalenceMode, variables: list[str]
    ) -> Equivalence:
        """判断 a 与 b 是否等价。``variables`` 是自变量，antiderivative 模式对它求导。

        expr 模式先符号化简，失败时在定义域内取 8 个以上的点做数值比较。
        """
        ...

    def verify_item(self, spec: ItemSpec, answer: LaTeX) -> ItemCheck:
        """按 spec 重新计算标准答案，并与 answer 比较，比较模式为 spec.answer_mode。"""
        ...

    def check_distinct(
        self, spec: ItemSpec, correct: LaTeX, predictions: list[Prediction]
    ) -> Distinctness:
        """追问题是否有区分度：各预测两两不等价，且都不等价于正确答案。

        SLIP 的预测就是正确答案，不参与比较。
        """
        ...

    def fingerprint(self, spec: ItemSpec) -> str:
        """题目指纹：spec 规范化后的哈希，用于题目去重。"""
        ...
