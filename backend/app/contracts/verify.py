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


class AnswerGroup(BaseModel):
    """预测答案相同的一组候选假设。"""

    answer: LaTeX
    misconception_ids: list[str]  # 正确答案组含 SLIP
    is_correct: bool


class Distinctness(BaseModel):
    ok: bool | None  # 至少分成两组；None 表示有预测无法判定
    groups: list[AnswerGroup] = []
    detail: str = ""

    def separates(self, misconception_id: str) -> bool:
        """该错因的预测是否不同于正确答案。抗诱错题要求目标错因满足这一条。"""
        return any(
            misconception_id in g.misconception_ids and not g.is_correct for g in self.groups
        )


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
        """把候选假设按预测答案分组，与正确答案等价的归入正确答案组（SLIP 总在这一组）。

        至少分成两组即有区分度：不论学生怎么答，至少能排除一个候选。允许几个候选预测相同，
        如演示中“求 sin(3x) 的导数”，漏乘内层导数与偶发失误都预测 3cos(3x)，这道题只用来
        分出“公式代换”，剩下两个由第二道追问题区分。
        """
        ...

    def fingerprint(self, spec: ItemSpec) -> str:
        """题目指纹：spec 规范化后的哈希，用于题目去重。"""
        ...

    def partial_sum(self, expr: str, variable: str, terms: int, start: int = 1) -> float:
        """有限项数值和，仅供模型评阅参考，不判定级数收敛性。"""
        ...
