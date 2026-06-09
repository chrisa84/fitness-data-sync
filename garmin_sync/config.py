from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    garmin_email: str = ""
    garmin_password: SecretStr = SecretStr("")
    garmin_token_path: Path = Path.home() / ".garmin_tokens"
    garmin_db_path: Path = Path("garmin_sync.db")
    garmin_request_delay_seconds: float = 1.0
    garmin_max_retries: int = 3
    garmin_backoff_base_seconds: float = 2.0
    garmin_backfill_page_size: int = 100
    log_level: str = "INFO"


def load_config(env_file: str | None = None) -> Config:
    if env_file:
        return Config(_env_file=env_file)
    return Config()
