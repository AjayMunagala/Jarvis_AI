"""Validated application settings."""

from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

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
    persistence_enabled: bool = False
    persistence_database_url: SecretStr | None = None
    persistence_connect_timeout_seconds: int = Field(default=3, ge=1, le=15)
    persistence_operation_timeout_seconds: int = Field(default=5, ge=1, le=60)
    local_auth_token: SecretStr | None = None
    owner_id: UUID | None = None
    memory_retrieval_enabled: bool = False
    memory_retrieval_limit: int = Field(default=3, ge=1, le=5)

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
        if self.persistence_enabled:
            database_url = (
                self.persistence_database_url.get_secret_value()
                if self.persistence_database_url else ""
            )
            if not database_url.strip():
                raise ValueError(
                    "JARVIS_PERSISTENCE_DATABASE_URL is required when JARVIS_PERSISTENCE_ENABLED=true."
                )
            try:
                parsed_database_url = urlsplit(database_url)
                valid_database_url = (
                    parsed_database_url.scheme in {"postgresql", "postgres"}
                    and bool(parsed_database_url.hostname)
                    and parsed_database_url.path not in {"", "/"}
                    and not parsed_database_url.fragment
                )
            except ValueError:
                valid_database_url = False
            if not valid_database_url:
                raise ValueError(
                    "JARVIS_PERSISTENCE_DATABASE_URL must include a PostgreSQL host and database name."
                )
        # Any configured non-mock provider can send request content outside the
        # process, so even stateless provider use requires the existing local
        # bearer identity. No database is required by this condition.
        if self.persistence_enabled or self.provider != "mock":
            token = self.local_auth_token.get_secret_value() if self.local_auth_token else ""
            if (
                len(token) < 32
                or token != token.strip()
                or any(not 0x21 <= ord(char) <= 0x7E for char in token)
            ):
                raise ValueError(
                    "JARVIS_LOCAL_AUTH_TOKEN must be configured as at least 32 visible ASCII characters when persistence is enabled or a non-mock provider is selected."
                )
            if self.owner_id is None:
                raise ValueError("JARVIS_OWNER_ID is required when persistence is enabled or a non-mock provider is selected.")
            if self.owner_id.int == 0:
                raise ValueError("JARVIS_OWNER_ID must not be the nil UUID.")
        if self.memory_retrieval_enabled and not self.persistence_enabled:
            raise ValueError(
                "JARVIS_MEMORY_RETRIEVAL_ENABLED=true requires JARVIS_PERSISTENCE_ENABLED=true."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """Load settings once; invalid environment values fail at startup."""
    return Settings()
