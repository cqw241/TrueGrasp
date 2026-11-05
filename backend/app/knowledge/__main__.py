"""内容管线命令行。在 backend 目录运行：

uv run python -m app.knowledge skeleton        从《高等数学目录》生成缺少的讲次文件
uv run python -m app.knowledge check           入库检查，报告具体错误
uv run python -m app.knowledge ingest          检查通过后写入业务库
uv run python -m app.knowledge draft-probe <错因 ID>   起草判别追问题，导出待审核的 YAML
"""

import argparse
import asyncio
import sys
from pathlib import Path

import yaml
from sqlmodel import Session

from app.config import get_settings
from app.db import get_engine, init_db
from app.knowledge.content import Content, check_math, load_dir
from app.knowledge.ingest import ingest
from app.knowledge.skeleton import write_skeleton
from app.verify.tools import SympyMathTools

ROOT = Path(__file__).resolve().parents[3]
CONTENT = ROOT / "content"


def _checked(directory: Path) -> Content:
    content = load_dir(directory)
    check_math(content, SympyMathTools())
    for issue in content.issues:
        print(issue)
    print(
        f"知识点 {len(content.points)}，错因 {len(content.misconceptions)}，"
        f"策略卡 {len(content.strategies)}，题目 {len(content.items)}；"
        f"错误 {len(content.errors)}"
    )
    return content


def _draft(directory: Path, misconception_id: str) -> int:
    from app.knowledge.drafting import draft_probe
    from app.llm.client import OpenAICompatClient

    content = load_dir(directory)
    if misconception_id not in content.misconceptions:
        print(f"错因不存在：{misconception_id}")
        return 1
    result = asyncio.run(
        draft_probe(content, misconception_id, OpenAICompatClient(get_settings()), SympyMathTools())
    )
    for reason in result.failures:
        print(f"未通过：{reason}")
    if result.item is None:
        print("3 次起草都没有通过检查")
        return 1
    item = result.item
    doc = {
        "items": [
            {
                "id": item.id,
                "knowledge_point": item.knowledge_point_id,
                "purpose": item.purpose.value,
                "stem": item.stem,
                "spec": item.spec.model_dump(mode="json", exclude_defaults=True),
                "answer": item.answer,
                "predictions": [p.model_dump() for p in item.predictions],
            }
        ]
    }
    out = directory.parent / "drafts" / f"{item.id}.yaml"
    out.parent.mkdir(parents=True, exist_ok=True)
    header = (
        f"# 模型起草，待教师审核。设计思路：{result.rationale}\n"
        f"# 审核通过后，把题目移入讲次文件，"
        f"并在错因 {misconception_id} 的 probes 中加上 {item.id}\n"
    )
    out.write_text(header + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), "utf-8")
    print(f"已导出：{out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.knowledge")
    parser.add_argument("--content", type=Path, default=CONTENT, help="内容目录")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("skeleton")
    commands.add_parser("check")
    commands.add_parser("ingest")
    draft = commands.add_parser("draft-probe")
    draft.add_argument("misconception_id")
    args = parser.parse_args(argv)
    lectures = args.content / "lectures"

    if args.command == "skeleton":
        for path in write_skeleton(ROOT / "高等数学目录.md", lectures):
            print(f"已生成：{path}")
        return 0
    if args.command == "draft-probe":
        return _draft(lectures, args.misconception_id)

    content = _checked(lectures)
    if content.errors:
        return 1
    if args.command == "ingest":
        engine = get_engine()
        init_db(engine)
        with Session(engine) as session:
            for notice in ingest(session, content):
                print(notice)
            session.commit()
        print("已入库")
    return 0


if __name__ == "__main__":
    sys.exit(main())
