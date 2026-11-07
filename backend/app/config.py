from datetime import date
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """从环境变量或 backend/.env 读取配置，字段说明见 .env.example。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    llm_base_url: str = "https://api.deepseek.com"
    llm_api_key: str = ""
    llm_model: str = "deepseek-flash"
    llm_response_format: Literal["json_object", "json_schema"] = "json_object"
    llm_timeout: float = Field(default=60, gt=0)
    llm_validation_retries: int = Field(default=2, ge=0, le=5)
    llm_max_tool_rounds: int = Field(default=8, ge=1, le=32)
    vision_llm_base_url: str = ""
    vision_llm_api_key: str = ""
    vision_llm_model: str = ""

    teaching_clock_mode: Literal["demo", "real"] = "demo"
    teaching_clock_initial_date: date = date(2026, 9, 7)

    database_url: str = "sqlite:///./data/truegrasp.db"
    checkpoint_db_path: str = "./data/checkpoints.db"


@lru_cache
def get_settings() -> Settings:
    return Settings()
