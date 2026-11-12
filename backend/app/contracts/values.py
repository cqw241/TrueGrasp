"""存进 JSON 列、也在 Agent 之间传递的值对象。

公式约定：
- 面向学生的内容（题干、学生答案、标准答案、预测答案）用 LaTeX 字符串，与 MathLive、KaTeX 一致。
- 给程序重算用的 ItemSpec 用 SymPy 语法字符串（如 ``sin(3*x)**2``），避免 LaTeX 解析的歧义。
"""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, Field, model_validator

from app.contracts.enums import EquivalenceMode, Operation

LaTeX = Annotated[str, Field(description="LaTeX 公式，如 6\\sin(3x)\\cos(3x)")]
SymPyExpr = Annotated[str, Field(description="SymPy 语法表达式，如 sin(3*x)**2")]

SLIP = "slip"
"""“偶发失误”假设的错因 ID。它不在错因库中，预测答案就是正确答案。"""

PENDING_KNOWLEDGE_POINT_ID = "pending.unclassified"
"""学生提问暂时无法归类时使用；不是课程知识点，不计入内容统计和教学目标。"""
PENDING_KNOWLEDGE_POINT_NAME = "待归类（待教师确认）"


class Bound(BaseModel):
    var: str
    lower: SymPyExpr
    upper: SymPyExpr


class ItemSpec(BaseModel):
    """题目的结构化描述，verify_item 按它重新计算标准答案，指纹也由它生成。

    - expression：化简或求值 ``expr``，``variables`` 为其中的自变量。
    - derivative：``expr`` 依次对 ``variables`` 求导，``["x", "x"]`` 表示二阶导，
      ``["x", "y"]`` 表示先对 x 再对 y 的混合偏导。
    - indefinite_integral：对 ``variables[0]`` 积分。
    - definite_integral：一个 ``bounds``。
    - double_integral：两个 ``bounds``，按积分次序由内到外，内层上下限可含外层变量；
      积分区域用直角坐标描述，换元（如极坐标）是学生要做的事。
    """

    operation: Operation
    expr: SymPyExpr
    variables: list[str] = Field(default_factory=list)
    bounds: list[Bound] = Field(default_factory=list)
    limit_point: SymPyExpr | None = None
    limit_direction: Literal["+", "-", "+-"] = "+-"

    @model_validator(mode="after")
    def _check_shape(self) -> Self:
        if self.operation == Operation.LIMIT and (
            len(self.variables) != 1 or self.limit_point is None
        ):
            raise ValueError("limit requires one variable and limit_point")
        need_bounds = {Operation.DEFINITE_INTEGRAL: 1, Operation.DOUBLE_INTEGRAL: 2}
        expected = need_bounds.get(self.operation, 0)
        if len(self.bounds) != expected:
            raise ValueError(f"{self.operation} 需要 {expected} 个 bounds")
        if expected == 0 and self.operation != Operation.EXPRESSION and not self.variables:
            raise ValueError(f"{self.operation} 需要 variables")
        return self

    @property
    def answer_mode(self) -> EquivalenceMode:
        """比较这道题的答案时应该用的等价模式。"""
        if self.operation == Operation.INDEFINITE_INTEGRAL:
            return EquivalenceMode.ANTIDERIVATIVE
        if self.operation in (Operation.DEFINITE_INTEGRAL, Operation.DOUBLE_INTEGRAL):
            return EquivalenceMode.VALUE
        return EquivalenceMode.EXPR


class Prediction(BaseModel):
    """某个错因假设下，学生会写出的答案。"""

    misconception_id: str
    answer: LaTeX


class MisconceptionExample(BaseModel):
    question: str
    wrong: LaTeX
    correct: LaTeX
