"""验证上下文的唯一白名单构造入口，不接受辅导历史。"""

from contextlib import suppress

from sqlmodel import Session, select

from app.contracts.agents import ConfirmedMisconception, VerifierInput
from app.contracts.entities import Evidence, Item, Misconception, Task
from app.contracts.enums import Dimension
from app.contracts.values import SLIP
from app.orchestration.storage import ItemExposure
from app.verify.execution import AsyncMathTools
from app.verify.tools import SympyMathTools


def _fingerprint_inputs(
    session: Session, student_id: str, shown_item_ids: list[str], class_id: str | None = None
) -> tuple[set[str], list]:
    """Read stored fingerprints and the specs that still need recomputing."""
    answered_ids = session.exec(
        select(Evidence.item_id).where(
            Evidence.student_id == student_id, Evidence.class_id == class_id
        )
    ).all()
    exposures = session.exec(
        select(ItemExposure)
        .join(Task, Task.id == ItemExposure.task_id)
        .where(ItemExposure.student_id == student_id, Task.class_id == class_id)
    )
    item_ids = set(shown_item_ids) | {item_id for item_id in answered_ids if item_id is not None}
    fingerprints = set()
    for exposure in exposures:
        item_ids.add(exposure.item_id)
        fingerprints.add(
            exposure.fingerprint.removeprefix(f"{class_id}:") if class_id else exposure.fingerprint
        )
    specs = []
    for item in session.exec(select(Item).where(Item.id.in_(item_ids))):
        if item.fingerprint is not None:
            fingerprints.add(item.fingerprint)
        if item.spec is not None:
            # Existing evidence retains its stored fingerprint across the algorithm change.
            specs.append(item.spec)
    return fingerprints, specs


def used_fingerprints(
    session: Session, student_id: str, shown_item_ids: list[str], class_id: str | None = None
) -> set[str]:
    fingerprints, specs = _fingerprint_inputs(session, student_id, shown_item_ids, class_id)
    tools = SympyMathTools()
    for spec in specs:
        with suppress(ValueError, SyntaxError, TypeError, ArithmeticError, NotImplementedError):
            fingerprints.add(tools.fingerprint(spec))
    return fingerprints


async def used_fingerprints_async(
    session: Session, student_id: str, shown_item_ids: list[str], class_id: str | None = None
) -> set[str]:
    """Async request path: keep DB reads here, time-bound each SymPy fingerprint."""
    fingerprints, specs = _fingerprint_inputs(session, student_id, shown_item_ids, class_id)
    math = AsyncMathTools(SympyMathTools())
    for spec in specs:
        fingerprints.add(await math.fingerprint(spec))
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
    class_id: str | None = None,
) -> VerifierInput:
    misconceptions = []
    for misconception_id in dict.fromkeys(confirmed_misconception_ids):
        if misconception_id == SLIP:
            continue
        misconception = session.get(Misconception, misconception_id)
        if misconception is None or misconception.class_id not in (None, class_id):
            raise ValueError(f"错因不存在：{misconception_id}")
        misconceptions.append(
            ConfirmedMisconception(id=misconception.id, description=misconception.description)
        )

    return VerifierInput(
        knowledge_point_id=knowledge_point_id,
        dimensions=dimensions,
        confirmed_misconceptions=misconceptions,
        used_fingerprints=sorted(used_fingerprints(session, student_id, shown_item_ids, class_id)),
        context_knowledge_point_id=context_knowledge_point_id,
    )
