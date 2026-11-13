"""Explicit maintenance migration for students created before class ownership."""

import argparse
import json

from sqlalchemy import Engine, text, update
from sqlmodel import Session, select

from app.classroom import (
    ClassMastery,
    Classroom,
    Enrollment,
    StudentPlacement,
    active_class,
    write_lock,
)
from app.contracts.entities import Event, Evidence, Mastery, Student, Task
from app.db import get_engine
from app.ledger.service import install_evidence_guards
from app.orchestration.storage import QuestionPhoto


def migrate_student(engine: Engine, class_id: str, student_id: str) -> dict[str, int | str]:
    """Attach one legacy student to one class and scope only that student's unassigned rows.

    The operator chooses both sides explicitly. Global content and records whose
    owner is ambiguous remain unassigned and inaccessible to teachers.
    """
    with Session(engine) as session:
        write_lock(session)
        group = session.get(Classroom, class_id)
        if group is None:
            raise ValueError("班级不存在")
        student = session.get(Student, student_id)
        if student is None:
            raise ValueError("学生不存在")

        current_class = active_class(session, student_id)
        if current_class not in (None, class_id):
            raise ValueError("学生已在另一个有效班级，请先显式退班")
        placement = session.get(StudentPlacement, student_id)
        if placement is not None and placement.teacher_id != group.teacher_id:
            raise ValueError("学生已归属其他教师，拒绝自动迁移")
        if placement is None:
            session.add(StudentPlacement(student_id=student_id, teacher_id=group.teacher_id))

        member = session.get(Enrollment, (class_id, student_id))
        if member is None:
            member = Enrollment(class_id=class_id, student_id=student_id)
        member.status = "active"
        session.add(member)
        session.flush()

        tasks = list(
            session.exec(select(Task).where(Task.student_id == student_id, Task.class_id.is_(None)))
        )
        for task in tasks:
            task.class_id = class_id
            session.add(task)

        events = list(
            session.exec(
                select(Event).where(Event.student_id == student_id, Event.class_id.is_(None))
            )
        )
        for event in events:
            event.class_id = class_id
            session.add(event)

        photos = list(
            session.exec(
                select(QuestionPhoto).where(
                    QuestionPhoto.student_id == student_id,
                    QuestionPhoto.class_id.is_(None),
                )
            )
        )
        for photo in photos:
            photo.class_id = class_id
            session.add(photo)

        mastery_count = 0
        for old in session.exec(select(Mastery).where(Mastery.student_id == student_id)):
            if session.get(ClassMastery, (class_id, student_id, old.knowledge_point_id)) is None:
                session.add(
                    ClassMastery(
                        class_id=class_id,
                        student_id=student_id,
                        knowledge_point_id=old.knowledge_point_id,
                        state=old.state,
                        evidence_ids=old.evidence_ids,
                        updated_on=old.updated_on,
                    )
                )
                mastery_count += 1

        evidence_count = session.execute(
            select(Evidence.id).where(
                Evidence.student_id == student_id,
                Evidence.class_id.is_(None),
            )
        ).all()
        if evidence_count:
            # The ledger remains append-only outside this explicit, transactional
            # scope migration. DDL rolls back with the transaction on failure.
            session.execute(text("DROP TRIGGER IF EXISTS evidence_no_update"))
            session.execute(
                update(Evidence)
                .where(
                    Evidence.student_id == student_id,
                    Evidence.class_id.is_(None),
                )
                .values(class_id=class_id)
            )
            install_evidence_guards(session.connection())

        session.commit()
        return {
            "class_id": class_id,
            "student_id": student_id,
            "teacher_id": group.teacher_id,
            "tasks": len(tasks),
            "events": len(events),
            "question_photos": len(photos),
            "evidence": len(evidence_count),
            "mastery_rows": mastery_count,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="将一名旧学生及其未分配历史显式迁移到指定班级")
    parser.add_argument("--class-id", required=True)
    parser.add_argument("--student-id", required=True)
    args = parser.parse_args(argv)

    engine = get_engine()
    try:
        result = migrate_student(engine, args.class_id, args.student_id)
    except ValueError as exc:
        parser.error(str(exc))
    finally:
        engine.dispose()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
