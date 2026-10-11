"""Validated application settings."""

from functools import lru_cache
from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr, model_validator
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
    provider: Literal["mock", "openai"] = "mock"
    openai_api_key: SecretStr | None = None
    openai_base_url: AnyHttpUrl = "https://api.openai.com/v1"
    openai_model: str = Field(default="gpt-4o-mini", min_length=1, max_length=100)
    provider_connect_timeout_seconds: float = Field(default=2.0, gt=0, le=15)
    provider_read_timeout_seconds: float = Field(default=30.0, gt=0, le=60)
    request_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    max_tool_rounds: int = Field(default=5, ge=1, le=5)
    max_expert_retries: int = Field(default=2, ge=0, le=3)
    max_parallel_experts: int = Field(default=2, ge=1, le=3)
    max_message_length: int = Field(default=4000, ge=1, le=20000)
    max_request_body_bytes: int = Field(default=65536, ge=1024, le=1048576)

    @model_validator(mode="after")
    def validate_provider_configuration(self) -> "Settings":
        if self.provider == "openai":
            key = self.openai_api_key.get_secret_value() if self.openai_api_key else ""
            if not key.strip():
                raise ValueError("JARVIS_OPENAI_API_KEY is required when JARVIS_PROVIDER=openai.")
        if self.openai_base_url.username or self.openai_base_url.password:
            raise ValueError("JARVIS_OPENAI_BASE_URL must not contain credentials.")
        if self.provider == "openai" and self.openai_base_url.scheme != "https":
            raise ValueError("JARVIS_OPENAI_BASE_URL must use HTTPS.")
        if self.openai_base_url.query or self.openai_base_url.fragment:
            raise ValueError("JARVIS_OPENAI_BASE_URL must not contain a query or fragment.")
        if not self.openai_model.strip():
            raise ValueError("JARVIS_OPENAI_MODEL must not be blank.")
        return self


@lru_cache
def get_settings() -> Settings:
    """Load settings once; invalid environment values fail at startup."""
    return Settings()
