"""Validated application settings."""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="JARVIS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "JARVIS Core"
    environment: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    provider: Literal["mock"] = "mock"
    request_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    max_tool_rounds: int = Field(default=5, ge=1, le=5)
    max_expert_retries: int = Field(default=2, ge=0, le=3)
    max_parallel_experts: int = Field(default=2, ge=1, le=3)
    max_message_length: int = Field(default=4000, ge=1, le=20000)
    max_request_body_bytes: int = Field(default=65536, ge=1024, le=1048576)


@lru_cache
def get_settings() -> Settings:
    """Load settings once; invalid environment values fail at startup."""
    return Settings()
