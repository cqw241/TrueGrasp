"""验证上下文的唯一白名单构造入口，不接受辅导历史。"""

from contextlib import suppress

from sqlmodel import Session, select

from app.contracts.agents import ConfirmedMisconception, VerifierInput
from app.contracts.entities import Evidence, Item, Misconception
from app.contracts.enums import Dimension
from app.contracts.values import SLIP
from app.orchestration.storage import ItemExposure
from app.verify.tools import SympyMathTools


def used_fingerprints(session: Session, student_id: str, shown_item_ids: list[str]) -> set[str]:
    answered_ids = session.exec(
        select(Evidence.item_id).where(Evidence.student_id == student_id)
    ).all()
    exposures = session.exec(select(ItemExposure).where(ItemExposure.student_id == student_id))
    item_ids = set(shown_item_ids) | {item_id for item_id in answered_ids if item_id is not None}
    fingerprints = set()
    for exposure in exposures:
        item_ids.add(exposure.item_id)
        fingerprints.add(exposure.fingerprint)
    tools = SympyMathTools()
    for item in session.exec(select(Item).where(Item.id.in_(item_ids))):
        if item.fingerprint is not None:
            fingerprints.add(item.fingerprint)
        if item.spec is not None:
            # Existing evidence retains its stored fingerprint across the algorithm change.
            with suppress(ValueError, SyntaxError, TypeError, ArithmeticError, NotImplementedError):
                fingerprints.add(tools.fingerprint(item.spec))
    return fingerprints


def build_verifier_input(
    session: Session,
    *,
    student_id: str,
    knowledge_point_id: str,
    dimensions: list[Dimension],
    confirmed_misconception_ids: list[str],
    shown_item_ids: list[str],
    context_knowledge_point_id: str | None = None,
) -> VerifierInput:
    misconceptions = []
    for misconception_id in dict.fromkeys(confirmed_misconception_ids):
        if misconception_id == SLIP:
            continue
        misconception = session.get(Misconception, misconception_id)
        if misconception is None:
            raise ValueError(f"错因不存在：{misconception_id}")
        misconceptions.append(
            ConfirmedMisconception(id=misconception.id, description=misconception.description)
        )

    return VerifierInput(
        knowledge_point_id=knowledge_point_id,
        dimensions=dimensions,
        confirmed_misconceptions=misconceptions,
        used_fingerprints=sorted(used_fingerprints(session, student_id, shown_item_ids)),
        context_knowledge_point_id=context_knowledge_point_id,
    )
