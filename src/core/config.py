"""Application configuration via pydantic-settings."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "development"
    app_name: str = "youtube-automation-engine"
    log_level: str = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    database_url: str = Field(
        default="postgresql+asyncpg://youtube:change_me@localhost:5432/youtube_automation"
    )
    redis_url: str = "redis://localhost:6379/0"

    s3_endpoint_url: str = "http://localhost:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "youtube-artifacts"
    s3_region: str = "us-east-1"

    wavespeed_api_key: str = ""
    llm_base_url: str = "https://llm.wavespeed.ai/v1"
    llm_model: str = "google/gemini-2.5-flash"

    kokoro_voice: str = "am_michael"
    kokoro_lang: str = "a"
    chroma_persist_dir: str = "/data/chroma"

    youtube_client_secrets_file: str = "/secrets/client_secret.json"
    youtube_token_file: str = "/secrets/youtube_token.json"
    youtube_privacy_status: str = "private"

    runpod_api_key: str = ""
    runpod_endpoint_id: str = ""
    runpod_enabled: bool = False

    discord_webhook_url: str = ""
    job_webhook_url: str = ""
    api_key: str = ""
    secret_encryption_key: str = "dev-only-change-me"
    max_job_attempts: int = 3
    main_queue_name: str = "YouTube_Shorts"

    @property
    def llm_api_key(self) -> str:
        """Alias used across services — always WaveSpeed/OpenRouter key."""
        return self.wavespeed_api_key

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in {"production", "prod"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
