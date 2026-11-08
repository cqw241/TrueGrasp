"""Read the diagnosis, including predictions before the student answers."""

from sqlmodel import Session, select

from app.agents.tutor.storage import TutorSession
from app.contracts.api import DiagnosisView, HypothesisView, ProbeRecord
from app.contracts.entities import Evidence, Hypothesis, Item, Misconception, Task
from app.contracts.enums import ContentSource, EvidenceKind
from app.contracts.values import SLIP


def diagnosis(session: Session, task_id: int) -> DiagnosisView | None:
    task = session.get(Task, task_id)
    hypotheses = session.exec(
        select(Hypothesis).where(Hypothesis.task_id == task_id).order_by(Hypothesis.id)
    ).all()
    if task is None or not hypotheses:
        return None
    error = session.get(Evidence, hypotheses[0].error_evidence_id)
    views = []
    for h in hypotheses:
        misconception = session.get(Misconception, h.misconception_id)
        views.append(
            HypothesisView(
                hypothesis_id=h.id,
                misconception_id=h.misconception_id,
                description="偶发失误" if h.misconception_id == SLIP else misconception.description,
                status=h.status,
                unmodeled=misconception is not None
                and misconception.source == ContentSource.UNMODELED,
            )
        )
    row = session.get(TutorSession, task_id)
    probes = []
    answers = {
        e.item_id: e
        for e in session.exec(
            select(Evidence).where(
                Evidence.task_id == task_id, Evidence.kind == EvidenceKind.DIAGNOSIS
            )
        )
    }
    for item_id in row.state["probe_item_ids"] if row else []:
        item = session.get(Item, item_id)
        answer = answers.get(item_id)
        probes.append(
            ProbeRecord(
                item_id=item.id,
                stem=item.stem,
                correct_answer=item.answer,
                predictions=item.predictions,
                student_answer=answer.student_answer if answer else None,
                matched_misconception_id=answer.matched_misconception_id if answer else None,
                judged_by=answer.judged_by if answer else None,
            )
        )
    return DiagnosisView(
        task_id=task_id,
        student_id=task.student_id,
        knowledge_point_id=task.knowledge_point_id,
        error_evidence=error,
        hypotheses=views,
        probes=probes,
    )
