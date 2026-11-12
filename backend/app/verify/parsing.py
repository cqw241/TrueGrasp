"""Parse the two formula formats at the input boundary without evaluating Python code."""

import ast
import re

import sympy as sp
from sympy.parsing.latex import parse_latex
from sympy.parsing.latex.errors import LaTeXParsingError

FUNCTIONS = {
    name: getattr(sp, name)
    for name in (
        "sin",
        "cos",
        "tan",
        "cot",
        "sec",
        "csc",
        "asin",
        "acos",
        "atan",
        "sinh",
        "cosh",
        "tanh",
        "exp",
        "log",
        "sqrt",
        "Abs",
    )
}
FUNCTIONS["ln"] = sp.log
CONSTANTS = {"pi": sp.pi, "E": sp.E, "e": sp.E}


def symbol(name: str) -> sp.Symbol:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", name):
        raise ValueError("Invalid variable name")
    return sp.Symbol(name, real=True)


def _scalar(expr: sp.Expr) -> sp.Expr:
    if not isinstance(expr, sp.Expr) or expr.has(sp.Integral, sp.Derivative, sp.Sum, sp.Product):
        raise ValueError("Expected an explicit scalar expression")
    if expr.has(sp.zoo, sp.oo, -sp.oo, sp.nan, sp.I):
        raise ValueError("Non-finite or complex expression")
    return expr


def expression_syntax(source: str) -> ast.expr:
    if len(source) > 4096:
        raise ValueError("Expression is too long")
    tree = ast.parse(source.strip(), mode="eval")
    if sum(1 for _ in ast.walk(tree)) > 256:
        raise ValueError("Expression is too complex")
    return tree.body


def parse_expr(source: str, *, preserve_structure: bool = False) -> sp.Expr:
    """ItemSpec uses a small arithmetic/function subset of SymPy syntax, never eval."""
    expression = expression_syntax(source)
    evaluate = not preserve_structure

    def visit(node: ast.AST) -> sp.Expr:
        match node:
            case ast.Constant(value=value) if type(value) in (int, float):
                return sp.Rational(str(value))
            case ast.Name(id=name):
                return CONSTANTS[name] if name in CONSTANTS else symbol(name)
            case ast.UnaryOp(op=ast.UAdd(), operand=operand):
                return visit(operand)
            case ast.UnaryOp(op=ast.USub(), operand=operand):
                return sp.Mul(-1, visit(operand), evaluate=evaluate)
            case ast.BinOp(left=left, op=op, right=right):
                a, b = visit(left), visit(right)
                match op:
                    case ast.Add():
                        return sp.Add(a, b, evaluate=evaluate)
                    case ast.Sub():
                        return sp.Add(a, sp.Mul(-1, b, evaluate=evaluate), evaluate=evaluate)
                    case ast.Mult():
                        return sp.Mul(a, b, evaluate=evaluate)
                    case ast.Div():
                        return sp.Mul(a, sp.Pow(b, -1, evaluate=evaluate), evaluate=evaluate)
                    case ast.Pow():
                        if b.is_number and abs(b) > 1000:
                            raise ValueError("Exponent is too large")
                        return sp.Pow(a, b, evaluate=evaluate)
            case ast.Call(func=ast.Name(id=name), args=args, keywords=[]) if name in FUNCTIONS:
                return FUNCTIONS[name](*(visit(arg) for arg in args), evaluate=evaluate)
        raise ValueError("Unsupported expression syntax")

    return _scalar(visit(expression))


def parse_answer(source: str) -> sp.Expr:
    """Strict LaTeX parsing: incomplete formulas must not silently lose their suffix."""
    text = source.strip()
    for opening, closing in (("$$", "$$"), ("$", "$"), (r"\(", r"\)"), (r"\[", r"\]")):
        if text.startswith(opening) and text.endswith(closing):
            text = text[len(opening) : -len(closing)].strip()
            break
    if not text or len(text) > 4096:
        raise ValueError("Empty or excessively long formula")
    # Accept explicit SymPy notation as a convenience, without passing it to eval.
    if "\\" not in text and (
        "**" in text or re.search(r"\b(?:pi|sin|cos|tan|exp|log|sqrt)\b", text)
    ):
        try:
            return parse_expr(text)
        except (ValueError, SyntaxError, TypeError):
            pass
    text = text.replace(r"\dfrac", r"\frac").replace(r"\tfrac", r"\frac")
    try:
        expr = parse_latex(text, strict=True)
    except LaTeXParsingError as exc:
        raise ValueError(str(exc)) from exc
    expr = expr.xreplace({s: CONSTANTS.get(s.name, symbol(s.name)) for s in expr.free_symbols})
    return _scalar(expr)
