"""Deterministic math checks. An inconclusive check is never a negative verdict."""

import contextlib
import hashlib
import json
import math
import random

import sympy as sp
from sympy.calculus.util import continuous_domain

from app.contracts.enums import EquivalenceMode, Operation
from app.contracts.values import SLIP, ItemSpec, LaTeX, Prediction
from app.contracts.verify import AnswerGroup, Distinctness, Equivalence, ItemCheck
from app.verify.parsing import parse_answer, parse_expr, symbol


def _real_value(expr: sp.Expr) -> float | None:
    value = expr.evalf(30)
    if value.is_real is not True or value.is_finite is not True:
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _numeric(a: sp.Expr, b: sp.Expr) -> Equivalence:
    variables = sorted(a.free_symbols | b.free_symbols, key=str)
    rng = random.Random(0)
    domains = {}
    for var in variables:
        domain = sp.S.Reals
        for expr in (a, b):
            if expr.free_symbols <= {var}:
                with contextlib.suppress(NotImplementedError):
                    domain &= continuous_domain(expr, var, sp.S.Reals)
        domains[var] = domain

    valid = 0
    for _ in range(128 if variables else 1):
        substitutions = {}
        for var in variables:
            domain = domains[var]
            intervals = domain.args if isinstance(domain, sp.Union) else (domain,)
            intervals = [part for part in intervals if isinstance(part, sp.Interval)]
            interval = rng.choice(intervals) if intervals else sp.Interval(-10, 10)
            lower = float(interval.start) if interval.start.is_finite else None
            upper = float(interval.end) if interval.end.is_finite else None
            if lower is None:
                lower = upper - 20 if upper is not None else -10
            if upper is None:
                upper = lower + 20
            point = lower + (upper - lower) * rng.uniform(0.01, 0.99)
            substitutions[var] = point
        try:
            left, right = _real_value(a.subs(substitutions)), _real_value(b.subs(substitutions))
        except (TypeError, ValueError, ArithmeticError):
            continue
        if left is None or right is None:
            continue
        valid += 1
        if not math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-10):
            return Equivalence(equivalent=False, method="numeric", detail="A real counterexample")
        if valid >= (8 if variables else 1):
            return Equivalence(
                equivalent=True, method="numeric", detail=f"Matched at {valid} real domain points"
            )
    return Equivalence(equivalent=None, method="none", detail="Insufficient real domain samples")


def _compare(a: sp.Expr, b: sp.Expr, mode: EquivalenceMode, variables: list[str]) -> Equivalence:
    if mode == EquivalenceMode.VALUE:
        if a.free_symbols or b.free_symbols:
            return Equivalence(equivalent=None, method="none", detail="Value contains free symbols")
        return _numeric(a, b)
    if mode == EquivalenceMode.ANTIDERIVATIVE:
        if not variables:
            raise ValueError("Antiderivative comparison requires an integration variable")
        results = [
            _compare(sp.diff(a, symbol(var)), sp.diff(b, symbol(var)), EquivalenceMode.EXPR, [])
            for var in dict.fromkeys(variables)
        ]
        if any(result.equivalent is False for result in results):
            return next(result for result in results if result.equivalent is False)
        if any(result.equivalent is None for result in results):
            return next(result for result in results if result.equivalent is None)
        return Equivalence(equivalent=True, method="symbolic", detail="Derivatives agree")
    difference = sp.trigsimp(sp.simplify(a - b))
    if difference == 0:
        return Equivalence(
            equivalent=True, method="symbolic", detail="Difference simplifies to zero"
        )
    if difference.is_number and difference.is_zero is False:
        return Equivalence(
            equivalent=False, method="symbolic", detail="Nonzero constant difference"
        )
    return _numeric(a, b)


def _compute(spec: ItemSpec) -> sp.Expr:
    expr = parse_expr(spec.expr)
    match spec.operation:
        case Operation.LIMIT:
            point = {"oo": sp.oo, "-oo": -sp.oo}.get(spec.limit_point)
            if point is None:
                point = parse_expr(spec.limit_point)
            expr = sp.limit(expr, symbol(spec.variables[0]), point, dir=spec.limit_direction)
        case Operation.DERIVATIVE:
            expr = sp.diff(expr, *(symbol(var) for var in spec.variables))
        case Operation.INDEFINITE_INTEGRAL:
            if len(spec.variables) != 1:
                raise ValueError("Indefinite integral requires exactly one variable")
            expr = sp.integrate(expr, symbol(spec.variables[0]))
        case Operation.DEFINITE_INTEGRAL | Operation.DOUBLE_INTEGRAL:
            bound_symbols = [symbol(bound.var) for bound in spec.bounds]
            if len(set(bound_symbols)) != len(bound_symbols):
                raise ValueError("Integration variables must be distinct")
            for index, bound in enumerate(spec.bounds):
                lower, upper = parse_expr(bound.lower), parse_expr(bound.upper)
                if (lower.free_symbols | upper.free_symbols) & set(bound_symbols[: index + 1]):
                    raise ValueError("Integral bounds depend on an inner integration variable")
                expr = sp.integrate(expr, (bound_symbols[index], lower, upper))
    if expr.has(sp.Integral, sp.Derivative, sp.Piecewise, sp.zoo, sp.oo, -sp.oo, sp.nan):
        raise ValueError("SymPy could not produce a finite explicit result")
    return expr


class SympyMathTools:
    def check_equivalent(
        self, a: LaTeX, b: LaTeX, mode: EquivalenceMode, variables: list[str]
    ) -> Equivalence:
        try:
            return _compare(parse_answer(a), parse_answer(b), mode, variables)
        except (ValueError, SyntaxError, TypeError, ArithmeticError, NotImplementedError) as exc:
            return Equivalence(equivalent=None, method="none", detail=str(exc))

    def verify_item(self, spec: ItemSpec, answer: LaTeX) -> ItemCheck:
        try:
            computed = _compute(spec)
            result = _compare(computed, parse_answer(answer), spec.answer_mode, spec.variables)
            return ItemCheck(
                ok=result.equivalent, computed=sp.latex(computed), detail=result.detail
            )
        except (ValueError, SyntaxError, TypeError, ArithmeticError, NotImplementedError) as exc:
            return ItemCheck(ok=None, detail=str(exc))

    def check_distinct(
        self, spec: ItemSpec, correct: LaTeX, predictions: list[Prediction]
    ) -> Distinctness:
        checked = self.verify_item(spec, correct)
        if checked.ok is not True:
            return Distinctness(
                ok=None, detail="Correct answer was not verified: " + checked.detail
            )
        groups = [AnswerGroup(answer=correct, misconception_ids=[SLIP], is_correct=True)]
        seen = set()
        for prediction in predictions:
            if prediction.misconception_id in seen:
                return Distinctness(ok=None, detail="Duplicate misconception ID")
            seen.add(prediction.misconception_id)
            for group in groups:
                result = self.check_equivalent(
                    prediction.answer, group.answer, spec.answer_mode, spec.variables
                )
                if result.equivalent is None:
                    return Distinctness(ok=None, detail="A prediction could not be compared")
                if result.equivalent:
                    if prediction.misconception_id == SLIP and not group.is_correct:
                        return Distinctness(ok=None, detail="SLIP must predict the correct answer")
                    if prediction.misconception_id not in group.misconception_ids:
                        group.misconception_ids.append(prediction.misconception_id)
                    break
            else:
                if prediction.misconception_id == SLIP:
                    return Distinctness(ok=None, detail="SLIP must predict the correct answer")
                groups.append(
                    AnswerGroup(
                        answer=prediction.answer,
                        misconception_ids=[prediction.misconception_id],
                        is_correct=False,
                    )
                )
        candidate_groups = sum(bool(group.misconception_ids) for group in groups)
        return Distinctness(ok=candidate_groups >= 2, groups=groups)

    def fingerprint(self, spec: ItemSpec) -> str:
        canonical = spec.model_dump(mode="json")
        canonical["expr"] = sp.srepr(sp.simplify(parse_expr(spec.expr)))
        for bound in canonical["bounds"]:
            for key in ("lower", "upper"):
                bound[key] = sp.srepr(sp.simplify(parse_expr(bound[key])))
        if spec.operation == Operation.EXPRESSION:
            canonical["variables"] = sorted(set(spec.variables))
        encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()

    def partial_sum(self, expr: str, variable: str, terms: int, start: int = 1) -> float:
        """Finite numerical evidence only; this does not decide series convergence."""
        if not 1 <= terms <= 10000:
            raise ValueError("terms must be between 1 and 10000")
        term, index = parse_expr(expr), symbol(variable)
        values = [_real_value(term.subs(index, n)) for n in range(start, start + terms)]
        if any(value is None for value in values):
            raise ValueError("Partial sum contains an undefined or non-real term")
        return math.fsum(values)
