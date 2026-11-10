"""Offline, paired SQLite snapshots. The backend must be stopped for both operations."""

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path
from shutil import copyfile
from tempfile import TemporaryDirectory

from sqlmodel import Session, create_engine, select

from app.clock.storage import PrecheckContext, TeachingClockState
from app.contracts.entities import Evidence, Item, Mastery, Student, Task
from app.orchestration.storage import PendingWork

from ..observed import RunExport

BUSINESS = "truegrasp.db"
CHECKPOINTS = "checkpoints.db"
TABLES = {
    BUSINESS: {"evidence", "student", "task", "teaching_clock_state", "orchestration_work"},
    CHECKPOINTS: {"checkpoints", "writes"},
}


def _read(path: Path):
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)


def _hash(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def _check(db: sqlite3.Connection, name: str) -> None:
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not TABLES[name] <= tables:
        raise ValueError(f"{name} 缺少 SQLite 表：{sorted(TABLES[name] - tables)}")
    if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise ValueError(f"{name} SQLite 完整性检查失败")


def save(business: Path, checkpoints: Path, output: Path, *, revision: str) -> dict:
    if output.exists() or business.resolve() == checkpoints.resolve():
        raise ValueError("需要两个不同的源数据库和一个全新的快照目录")
    output.parent.mkdir(parents=True, exist_ok=True)
    with (
        TemporaryDirectory(dir=output.parent) as temp,
        closing(_read(business)) as business_db,
        closing(_read(checkpoints)) as checkpoint_db,
    ):
        stage = Path(temp) / "pair"
        stage.mkdir()
        sources = {BUSINESS: business_db, CHECKPOINTS: checkpoint_db}
        versions = {
            name: db.execute("PRAGMA data_version").fetchone() for name, db in sources.items()
        }
        files = {}
        for name, source in sources.items():
            with closing(sqlite3.connect(stage / name)) as destination:
                source.backup(destination)
                destination.execute("PRAGMA journal_mode=DELETE")
                _check(destination, name)
            files[name] = _hash(stage / name)
        if any(
            db.execute("PRAGMA data_version").fetchone() != versions[name]
            for name, db in sources.items()
        ):
            raise ValueError("复制期间有数据库被写入；停机后重试")
        manifest = {"format": 1, "revision": revision, "sha256": files}
        (stage / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        stage.rename(output)
    return manifest


def restore(snapshot: Path, output: Path) -> dict:
    if output.exists():
        raise ValueError("恢复目标必须是新目录，请为每次试录保留独立数据库")
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != 1 or set(manifest.get("sha256", {})) != set(TABLES):
        raise ValueError("快照清单格式错误")
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=output.parent) as temp:
        stage = Path(temp) / "restored"
        stage.mkdir()
        for name in TABLES:
            copyfile(snapshot / name, stage / name)
            if _hash(stage / name) != manifest["sha256"][name]:
                raise ValueError(f"{name} 与快照清单不匹配")
            with closing(_read(stage / name)) as db:
                _check(db, name)
        stage.rename(output)
    return manifest


def capture(business: Path, run_id: str) -> tuple[RunExport, dict]:
    """Read actual rows. Only label the accepted capture boundary at lecture 9."""
    engine = create_engine("sqlite://", creator=lambda: _read(business))
    try:
        with Session(engine) as session:
            clock = session.get(TeachingClockState, 1)
            evidence = list(session.exec(select(Evidence).order_by(Evidence.id)))
            tasks = list(session.exec(select(Task).order_by(Task.id)))
            contexts = list(
                session.exec(select(PrecheckContext).where(PrecheckContext.lecture == 9))
            )
            pending = list(session.exec(select(PendingWork.key).where(PendingWork.pending)))
            at_boundary = (
                clock is not None
                and clock.lecture == 9
                and date(2026, 10, 28) <= clock.teaching_date < date(2026, 11, 2)
                and all(e.teaching_date < date(2026, 11, 2) for e in evidence)
                and all(t.created_on < date(2026, 11, 2) for t in tasks)
                and not pending
            )
            actual = RunExport(
                run_id=run_id,
                checkpoint="after_ch9_followup_before_ch10_live" if at_boundary else "partial",
                students=list(session.exec(select(Student).order_by(Student.id))),
                evidence=evidence,
                mastery=list(session.exec(select(Mastery))),
                tasks=tasks,
                items=list(session.exec(select(Item).order_by(Item.id))),
                precheck_task_ids=sorted(c.task_id for c in contexts),
            )
            summary = {
                "clock": clock.model_dump(mode="json") if clock else None,
                "students": len(actual.students),
                "tasks": len(tasks),
                "evidence": len(evidence),
                "pending_work": sorted(pending),
            }
        return actual, summary
    finally:
        engine.dispose()
