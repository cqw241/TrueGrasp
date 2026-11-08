"""Reproducible numerical evidence for the slope-conflict strategy."""

import math

from app.contracts.entities import Evidence, Item
from app.contracts.enums import Operation
from app.verify.parsing import parse_answer, parse_expr, symbol


def slope_conflict(item: Item | None, error: Evidence) -> str:
    if (
        item is None
        or item.spec is None
        or (item.spec.operation != Operation.DERIVATIVE or len(item.spec.variables) != 1)
    ):
        return ""
    try:
        variable = symbol(item.spec.variables[0])
        expression = parse_expr(item.spec.expr)
        proposed = parse_answer(error.student_answer)
        step = 1e-5
        for point in (0.2, 0.5, 1.0):
            quotient = float(
                (expression.subs(variable, point + step) - expression.subs(variable, point - step))
                / (2 * step)
            )
            prediction = float(proposed.subs(variable, point))
            if all(math.isfinite(x) for x in (quotient, prediction)) and not math.isclose(
                quotient, prediction, rel_tol=1e-3, abs_tol=1e-3
            ):
                return (
                    f"回看原题：{item.stem}。在 {variable}={point} 附近，"
                    f"步长 {step} 的中心差商约为 {quotient:.6g}，"
                    f"原答给出的斜率约为 {prediction:.6g}。"
                    "两者不一致，请思考内层变化率的作用；数值近似不能代替一般证明。"
                )
    except (ValueError, TypeError, ArithmeticError, SyntaxError):
        pass
    return ""
