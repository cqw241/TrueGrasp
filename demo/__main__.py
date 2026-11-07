"""Run from the repository root: python -m demo check|export|batch|check-run."""

import argparse
import csv
import io
import json
import sys
from pathlib import Path

# The backend is deliberately not packaged in T01. Keep this bootstrap in the CLI.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from demo.dataset import ROOT, load_dataset, validate_dataset  # noqa: E402
from demo.observed import RunExport, validate_run  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", type=Path, default=ROOT / "demo/data/scenario.json")
    parser.add_argument("--content", type=Path, default=ROOT / "content/lectures")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check")
    commands.add_parser("export")
    batch = commands.add_parser("batch")
    batch.add_argument("stage")
    batch.add_argument("--include-human", action="store_true")
    commands.add_parser("homework-csv")
    observed = commands.add_parser("check-run")
    observed.add_argument("snapshot", type=Path)
    for command in commands.choices.values():
        command.add_argument("--output", type=Path, help="write UTF-8 output to this file")
    args = parser.parse_args(argv)
    try:
        data = load_dataset(args.scenario, args.content)
        report = validate_dataset(data)
        if args.command == "check":
            output = report
        elif args.command == "export":
            output = data.export()
        elif args.command == "check-run":
            output = validate_run(
                data, RunExport.model_validate_json(args.snapshot.read_text(encoding="utf-8"))
            )
        elif args.command == "homework-csv":
            buffer = io.StringIO()
            writer = csv.writer(buffer, lineterminator="\n")
            writer.writerow(
                [
                    "student_id",
                    "student_name",
                    "item_id",
                    "knowledge_point_id",
                    "stem",
                    "student_answer",
                    "correct_answer",
                ]
            )
            names = {s.id: s.name for s in data.scenario.students}
            for a in data.batch("homework_ch2"):
                item = data.items[a.item_id]
                writer.writerow(
                    [
                        a.student_id,
                        names[a.student_id],
                        item.id,
                        item.knowledge_point_id,
                        item.stem,
                        a.student_answer,
                        item.answer,
                    ]
                )
            if args.output:
                args.output.write_text(buffer.getvalue(), encoding="utf-8")
            else:
                print(buffer.getvalue(), end="")
            return 0
        else:
            if args.stage not in {a.stage for a in data.answers}:
                raise ValueError(f"unknown stage: {args.stage}")
            output = [
                {
                    "key": a.key,
                    "student_id": a.student_id,
                    "teaching_date": a.teaching_date.isoformat(),
                    "context_knowledge_point_id": a.context_knowledge_point_id,
                    "round": a.round,
                    "message": a.message().model_dump(mode="json"),
                }
                for a in data.batch(args.stage, include_human=args.include_human)
            ]
        encoded = json.dumps(output, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.write_text(encoded, encoding="utf-8")
        else:
            print(encoded, end="")
        return 0
    except (ValueError, KeyError, OSError, StopIteration) as exc:
        print(f"T20 validation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
