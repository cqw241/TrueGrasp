import math

import pytest

from app.contracts.enums import EquivalenceMode as Mode
from app.contracts.values import ItemSpec, Prediction
from app.verify.tools import SympyMathTools

tools = SympyMathTools()


@pytest.mark.parametrize(
    "a,b,mode,variables,expected",
    [
        (r"6\sin(3x)\cos(3x)", r"3\sin(6x)", Mode.EXPR, ["x"], True),
        (r"2\pi/3", r"\pi/2", Mode.VALUE, [], False),
        ("x^2+C", "x^2+7", Mode.ANTIDERIVATIVE, ["x"], True),
        ("x^2", "x^3", Mode.ANTIDERIVATIVE, ["x"], False),
        ("x+", "x", Mode.EXPR, ["x"], None),
        (r"\sqrt{x^2}", "x", Mode.EXPR, ["x"], False),
        ("x", "1", Mode.VALUE, [], None),
    ],
)
def test_equivalence(a, b, mode, variables, expected):
    assert tools.check_equivalent(a, b, mode, variables).equivalent is expected


@pytest.mark.parametrize(
    "spec,answer",
    [
        (dict(operation="derivative", expr="sin(3*x)**2", variables=["x"]), r"3\sin(6x)"),
        (dict(operation="derivative", expr="x**2*y**3", variables=["x", "y"]), "6xy^2"),
        (dict(operation="indefinite_integral", expr="2*x", variables=["x"]), "x^2+C"),
        (
            dict(
                operation="definite_integral",
                expr="x",
                bounds=[dict(var="x", lower="0", upper="2")],
            ),
            "2",
        ),
        (
            dict(
                operation="double_integral",
                expr="x+y",
                bounds=[dict(var="x", lower="0", upper="y"), dict(var="y", lower="0", upper="1")],
            ),
            r"\frac{1}{2}",
        ),
        (
            dict(
                operation="double_integral",
                expr="x**2+y**2",
                bounds=[
                    dict(var="y", lower="-sqrt(1-x**2)", upper="sqrt(1-x**2)"),
                    dict(var="x", lower="-1", upper="1"),
                ],
            ),
            r"\pi/2",
        ),
        (dict(operation="limit", expr="sin(x)/x", variables=["x"], limit_point="0"), "1"),
        (dict(operation="limit", expr="(1+1/x)**x", variables=["x"], limit_point="oo"), "e"),
    ],
)
def test_recompute(spec, answer):
    assert tools.verify_item(ItemSpec(**spec), answer).ok is True


def test_distinct_and_uncertain():
    spec = ItemSpec(operation="derivative", expr="sin(3*x)", variables=["x"])
    result = tools.check_distinct(
        spec, r"3\cos(3x)", [Prediction(misconception_id="formula", answer=r"\cos(3x)")]
    )
    assert result.ok and result.separates("formula")
    assert "slip" in result.groups[0].misconception_ids
    assert (
        tools.check_distinct(
            spec, r"3\cos(3x)", [Prediction(misconception_id="bad", answer="x+")]
        ).ok
        is None
    )


def test_partial_sum_and_fingerprint():
    assert tools.partial_sum("1/n**2", "n", 3) == pytest.approx(1 + 1 / 4 + 1 / 9)
    assert tools.partial_sum("(-1)**n", "n", 4) == 0
    with pytest.raises(ValueError):
        tools.partial_sum("1/n", "n", 3, start=0)
    a = ItemSpec(operation="derivative", expr="x*x", variables=["x"])
    b = a.model_copy(update={"expr": "x**2"})
    assert tools.fingerprint(a) == tools.fingerprint(b)
    assert math.isfinite(tools.partial_sum("1/n", "n", 100))


def test_reject_python_syntax():
    spec = ItemSpec(operation="expression", expr="__import__('os').getcwd()")
    assert tools.verify_item(spec, "1").ok is None
