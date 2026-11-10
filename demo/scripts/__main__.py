"""Run with the backend Python from the repository root: python -m demo.scripts --help."""

import argparse
import getpass
import json
import os
import sqlite3
import sys
from contextlib import suppress
from datetime import date
from pathlib import Path

import httpx
from sqlalchemy.exc import SQLAlchemyError

# The backend is intentionally not installed as a package.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from demo.dataset import load_dataset  # noqa: E402
from demo.scripts import snapshot  # noqa: E402
from demo.scripts.online import STAGES, DemoAPI, DemoError  # noqa: E402


def emit(value) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    print(json.dumps(value, ensure_ascii=False), flush=True)


def parse(argv: list[str] | None):
    parser = argparse.ArgumentParser(prog="python -m demo.scripts")
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="backend origin")
    parser.add_argument("--wait", type=float, default=600, help="long operation timeout, seconds")
    parser.add_argument(
        "--username", default=os.environ.get("TRUEGRASP_DEMO_USER"), help="teacher account"
    )
    parser.add_argument("--password-stdin", action="store_true", help="read password from stdin")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="read clock, class data and teacher interface status")
    commands.add_parser("goal", help="create or reuse the T20 goal using T16")
    commands.add_parser("clock")
    commands.add_parser("advance").add_argument("date", type=date.fromisoformat)
    commands.add_parser("lecture").add_argument("number", type=int, choices=range(1, 13))
    commands.add_parser("import-ch2").add_argument("--student", action="append", default=[])
    commands.add_parser("import-d-live").add_argument("answer", help="D's human-entered LaTeX")
    batch = commands.add_parser("batch")
    batch.add_argument("stage", choices=STAGES)
    batch.add_argument("--student", action="append", default=[])
    save = commands.add_parser("save", help="save both SQLite files with backend stopped")
    save.add_argument("business", type=Path)
    save.add_argument("checkpoints", type=Path)
    save.add_argument("output", type=Path)
    save.add_argument("--revision", required=True, help="deployed Git commit")
    restore = commands.add_parser("restore", help="restore both files into a new directory")
    restore.add_argument("snapshot", type=Path)
    restore.add_argument("output", type=Path)
    capture = commands.add_parser(
        "capture", help="export actual records from a stopped business DB"
    )
    capture.add_argument("business", type=Path)
    capture.add_argument("output", type=Path)
    capture.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    if not 0 < args.wait < float("inf"):
        parser.error("--wait must be a finite positive number")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse(argv)
    try:
        match args.command:
            case "save":
                emit(
                    snapshot.save(
                        args.business, args.checkpoints, args.output, revision=args.revision
                    )
                )
            case "restore":
                emit(snapshot.restore(args.snapshot, args.output))
            case "capture":
                actual, summary = snapshot.capture(args.business, args.run_id)
                with args.output.open("x", encoding="utf-8") as file:
                    file.write(actual.model_dump_json(indent=2) + "\n")
                emit({"checkpoint": actual.checkpoint, **summary})
            case _:
                if not args.username:
                    raise DemoError("在线操作需要教师账号：--username 或 TRUEGRASP_DEMO_USER")
                password = (
                    sys.stdin.readline().rstrip("\r\n")
                    if args.password_stdin
                    else getpass.getpass("教师密码: ")
                )
                if not password:
                    raise DemoError("教师密码不能为空")
                with httpx.Client(base_url=args.url, timeout=15, trust_env=False) as client:
                    api = DemoAPI(client, args.wait)
                    api.login(args.username, password)
                    try:
                        return online(api, args)
                    finally:
                        with suppress(httpx.HTTPError):
                            client.post("/api/auth/logout")
    except (
        DemoError,
        EOFError,
        OSError,
        ValueError,
        httpx.HTTPError,
        sqlite3.Error,
        SQLAlchemyError,
    ) as exc:
        print(f"T14: {exc}", file=sys.stderr)
        return 1
    return 0


def online(api: DemoAPI, args: argparse.Namespace) -> int:
    match args.command:
        case "status":
            failures = 0
            for route in (
                "/health",
                "/clock",
                "/goals",
                "/escalations",
                "/class/overview",
                "/class/issues",
                "/class/heatmap",
            ):
                try:
                    data = api.request("GET", route)
                    if route == "/class/heatmap":
                        data = {
                            "lectures": sorted({cell["lecture"] for cell in data}),
                            "points": len(data),
                            "assessed_points": sum(cell["assessed"] > 0 for cell in data),
                        }
                    emit({"route": route, "data": data})
                except DemoError as exc:
                    failures += 1
                    emit({"route": route, "error": str(exc)})
            return int(bool(failures))
        case "clock":
            emit(api.clock())
        case "advance":
            emit(api.advance(args.date))
        case "lecture":
            emit(api.request("POST", "/clock/lecture", json={"lecture": args.number}))
        case "goal":
            emit(api.goal(load_dataset()))
        case "import-ch2":
            emit(api.import_homework(load_dataset(), args.student))
        case "import-d-live":
            emit(api.import_homework(load_dataset(), [], live_answer=args.answer))
        case "batch":
            return int(bool(api.batch(load_dataset(), args.stage, args.student, emit)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
