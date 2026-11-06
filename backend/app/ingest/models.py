"""作业导入的输入与模型输出；这些是模块内部格式，不改变 HTTP contracts。"""

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.enums import EquivalenceMode
from app.contracts.values import ItemSpec


class HomeworkRow(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    student_id: str = Field(min_length=1)
    item_id: str = Field(min_length=1)
    student_answer: str = Field(min_length=1)
    student_name: str | None = None
    stem: str | None = None
    correct_answer: str | None = None
    knowledge_point_id: str | None = None


class ImageHomework(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    student_id: str = Field(min_length=1)
    item_id: str = Field(min_length=1)
    images: list[str] = Field(min_length=1)
    student_name: str | None = None
    stem: str | None = None
    correct_answer: str | None = None
    knowledge_point_id: str | None = None
    confirmed_answer: str | None = Field(default=None, min_length=1)


class ImageDraftRow(HomeworkRow):
    """An OCR review draft may be empty; confirmed import rows still cannot be."""

    student_answer: str = ""


class ImageDraft(BaseModel):
    row: ImageDraftRow
    doubts: list[str]
    requires_confirmation: bool


class HomeworkAnalysis(BaseModel):
    """模型只提出初步错因，不确认假设或迁移掌握状态。"""

    knowledge_point_id: str
    is_correct: bool | None
    misconception_id: str | None
    reason: str

    spec: ItemSpec | None = Field(default=None, description="从题干提取，使用 SymPy 语法")
    answer_mode: EquivalenceMode | None = Field(
        default=None, description="不定积分必须为 antiderivative；无法判断题型时为 null"
    )
    variables: list[str] = Field(default_factory=list, description="积分变量或表达式自变量")
