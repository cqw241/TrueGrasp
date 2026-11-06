"""在 backend 目录运行 python -m app.ingest --help。"""

import argparse
import asyncio
import base64
import json
import mimetypes
import sys
from datetime import date
from pathlib import Path

from openai import OpenAIError
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.db import get_engine, init_db
from app.ingest.models import ImageHomework
from app.ingest.service import ConfirmationRequired, HomeworkImporter, IngestError
from app.llm.client import LLMOutputError, OpenAICompatClient


def image_batch(path: Path) -> list[ImageHomework]:
    """清单中的本地图片相对清单路径解析，也支持 vision 契约中的 URL。"""
    batch = TypeAdapter(list[ImageHomework]).validate_json(path.read_text(encoding="utf-8-sig"))
    for entry in batch:
        urls = []
        for source in entry.images:
            if source.startswith(("http://", "https://", "data:image/")):
                urls.append(source)
                continue
            image_path = path.parent / source
            mime = mimetypes.guess_type(image_path.name)[0]
            if mime not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
                raise IngestError(f"不支持的图片类型：{image_path}")
            encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
            urls.append(f"data:{mime};base64,{encoded}")
        entry.images = urls
    return batch


async def _run(args: argparse.Namespace) -> int:
    engine = get_engine()
    try:
        init_db(engine)
        async with OpenAICompatClient(get_settings()) as llm:
            importer = HomeworkImporter(engine, llm)
            if args.command == "csv":
                result = await importer.import_csv(args.path.read_bytes(), args.teaching_date)
            else:
                try:
                    result = await importer.import_images(
                        image_batch(args.path), args.teaching_date
                    )
                except ConfirmationRequired as exc:
                    drafts = [draft.model_dump(mode="json") for draft in exc.drafts]
                    if args.review_output:
                        args.review_output.write_text(
                            json.dumps(drafts, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8",
                        )
                    else:
                        print(json.dumps(drafts, ensure_ascii=False, indent=2))
                    print(str(exc), file=sys.stderr)
                    return 2
        print(result.model_dump_json())
        return 0
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.ingest")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("csv", "images"):
        sub = commands.add_parser(command)
        sub.add_argument("path", type=Path, help="UTF-8 CSV 或图片 JSON 清单")
        sub.add_argument("--teaching-date", required=True, type=date.fromisoformat)
        if command == "images":
            sub.add_argument("--review-output", type=Path, help="保存需要确认的转写结果")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except (
        IngestError,
        ValidationError,
        OSError,
        OpenAIError,
        LLMOutputError,
        IntegrityError,
    ) as exc:
        print(f"导入失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
