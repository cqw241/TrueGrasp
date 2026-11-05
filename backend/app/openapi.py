"""导出 OpenAPI 定义到 backend/openapi.json，前端据此生成 API 客户端。

uv run python -m app.openapi          # 重新生成
uv run python -m app.openapi --check  # CI 用：文件与代码不一致时失败
"""

import json
import sys
from pathlib import Path

from app.main import create_app

OUTPUT = Path(__file__).resolve().parent.parent / "openapi.json"


def render() -> str:
    return json.dumps(create_app().openapi(), ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    content = render()
    if "--check" in sys.argv:
        if not OUTPUT.exists() or OUTPUT.read_text(encoding="utf-8") != content:
            print("openapi.json 已过期，运行 uv run python -m app.openapi", file=sys.stderr)
            return 1
        return 0
    OUTPUT.write_text(content, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
