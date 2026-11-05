"""数学核验工具（T05），实现 app.contracts.verify.MathTools。"""

from app.contracts.enums import EquivalenceMode
from app.contracts.values import ItemSpec, LaTeX, Prediction
from app.contracts.verify import Distinctness, Equivalence, ItemCheck


class SympyMathTools:
    def check_equivalent(
        self, a: LaTeX, b: LaTeX, mode: EquivalenceMode, variables: list[str]
    ) -> Equivalence:
        raise NotImplementedError("T05")

    def verify_item(self, spec: ItemSpec, answer: LaTeX) -> ItemCheck:
        raise NotImplementedError("T05")

    def check_distinct(
        self, spec: ItemSpec, correct: LaTeX, predictions: list[Prediction]
    ) -> Distinctness:
        raise NotImplementedError("T05")

    def fingerprint(self, spec: ItemSpec) -> str:
        raise NotImplementedError("T05")
