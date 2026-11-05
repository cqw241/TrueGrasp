"""Expose deterministic checks through the common Tool contract."""

from pydantic import BaseModel

from app.contracts.enums import EquivalenceMode
from app.contracts.llm import Tool
from app.contracts.values import ItemSpec, Prediction
from app.contracts.verify import MathTools


class EquivalentArgs(BaseModel):
    a: str
    b: str
    mode: EquivalenceMode
    variables: list[str]


class ItemArgs(BaseModel):
    spec: ItemSpec
    answer: str


class DistinctArgs(BaseModel):
    spec: ItemSpec
    correct: str
    predictions: list[Prediction]


def math_tools(math: MathTools) -> list[Tool]:
    return [
        Tool(
            "check_equivalent",
            "Compare mathematical answers",
            EquivalentArgs,
            lambda a: math.check_equivalent(a.a, a.b, a.mode, a.variables),
        ),
        Tool(
            "verify_item",
            "Recompute a problem's answer",
            ItemArgs,
            lambda a: math.verify_item(a.spec, a.answer),
        ),
        Tool(
            "check_distinct",
            "Group predictions and check distinctness",
            DistinctArgs,
            lambda a: math.check_distinct(a.spec, a.correct, a.predictions),
        ),
    ]
