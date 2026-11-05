"""验证上下文的唯一白名单构造入口，不接受辅导历史。"""

from sqlmodel import Session, select

from app.contracts.agents import ConfirmedMisconception, VerifierInput
from app.contracts.entities import Evidence, Item, Misconception
from app.contracts.enums import Dimension
from app.contracts.values import SLIP


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

    answered_ids = session.exec(
        select(Evidence.item_id).where(Evidence.student_id == student_id)
    ).all()
    item_ids = set(shown_item_ids) | {item_id for item_id in answered_ids if item_id is not None}
    fingerprints = session.exec(
        select(Item.fingerprint).where(Item.id.in_(item_ids), Item.fingerprint.is_not(None))
    ).all()
    return VerifierInput(
        knowledge_point_id=knowledge_point_id,
        dimensions=dimensions,
        confirmed_misconceptions=misconceptions,
        used_fingerprints=sorted(set(fingerprints)),
        context_knowledge_point_id=context_knowledge_point_id,
    )
