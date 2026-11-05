"""从《高等数学目录》生成 12 讲的内容骨架：每节一个知识点，先修关系留给教师补充。"""

import re
from pathlib import Path

import yaml

from app.contracts.enums import ExamTag
from app.knowledge.content import LectureDoc, PointDoc, SectionDoc

_NUMERALS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_LECTURE = re.compile(r"^## 第(.+?)讲\s*(.+)$")
_SECTION = re.compile(r"^- 第(.+?)节\s*(.+)$")
_ONLY = re.compile(r"\s*[（(]仅(.+?)要求[）)]\s*$")
_TAG = {"一": ExamTag.MATH1, "二": ExamTag.MATH2, "三": ExamTag.MATH3}
ALL_TAGS = [ExamTag.MATH1, ExamTag.MATH2, ExamTag.MATH3]


def _number(text: str) -> int:
    """一 到 十二。"""
    if text.startswith("十"):
        return 10 + _NUMERALS.get(text[1:], 0)
    return _NUMERALS[text]


def _split_tags(title: str) -> tuple[str, list[ExamTag] | None]:
    match = _ONLY.search(title)
    if match is None:
        return title.strip(), None
    tags = [tag for char, tag in _TAG.items() if f"数学{char}" in match.group(1)]
    return title[: match.start()].strip(), tags


def parse_toc(text: str) -> list[LectureDoc]:
    lectures: list[LectureDoc] = []
    lecture_tags: list[ExamTag] = ALL_TAGS
    for line in text.splitlines():
        if m := _LECTURE.match(line.strip()):
            title, tags = _split_tags(m.group(2))
            lecture_tags = tags or ALL_TAGS
            lectures.append(LectureDoc(lecture=_number(m.group(1)), title=title))
        elif (m := _SECTION.match(line.strip())) and lectures:
            title, tags = _split_tags(m.group(2))
            lecture = lectures[-1]
            section = _number(m.group(1))
            lecture.sections.append(
                SectionDoc(section=section, title=title, exam_tags=tags or lecture_tags)
            )
            lecture.points.append(
                PointDoc(id=f"calc.ch{lecture.lecture}.s{section}", section=section, name=title)
            )
    return lectures


class _Dumper(yaml.SafeDumper):
    """标量列表写成一行，如 ``exam_tags: [math1, math2]``；其余用块格式，方便教师编辑。"""


def _list(dumper: yaml.SafeDumper, data: list) -> yaml.Node:
    flow = all(not isinstance(v, dict | list) for v in data)
    return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=flow)


_Dumper.add_representer(list, _list)


def dump(doc: LectureDoc) -> str:
    data = doc.model_dump(mode="json", exclude_defaults=True)
    for section in data.get("sections", []):
        section.setdefault("exam_tags", [t.value for t in ALL_TAGS])
    return yaml.dump(data, Dumper=_Dumper, allow_unicode=True, sort_keys=False, width=100)


def write_skeleton(toc: Path, directory: Path) -> list[Path]:
    """只写入不存在的讲次文件，已有文件（教师可能已编辑）保持不变。"""
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for doc in parse_toc(toc.read_text(encoding="utf-8")):
        path = directory / f"{doc.lecture:02d}.yaml"
        if not path.exists():
            path.write_text(dump(doc), encoding="utf-8")
            written.append(path)
    return written
