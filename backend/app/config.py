from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """从环境变量或 backend/.env 读取配置，字段说明见 .env.example。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    llm_base_url: str = "https://api.deepseek.com"
    llm_api_key: str = ""
    llm_model: str = "deepseek-flash"

    database_url: str = "sqlite:///./data/truegrasp.db"
    checkpoint_db_path: str = "./data/checkpoints.db"


@lru_cache
def get_settings() -> Settings:
    return Settings()
