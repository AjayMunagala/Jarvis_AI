"""Validated application settings."""

from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, model_validator
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
    # Context assembly bounds use characters, not provider tokens: tokenization
    # differs by provider, so these are conservative, provider-neutral limits.
    document_retrieval_limit: int = Field(default=2, ge=0, le=5)
    context_history_messages: int = Field(default=6, ge=0, le=20)
    context_excerpt_max_chars: int = Field(default=1500, ge=200, le=4000)
    context_max_chars: int = Field(default=12000, ge=2000, le=40000)
    verification_execution_enabled: bool = False
    verification_timeout_seconds: float = Field(default=30.0, ge=1.0, le=120.0)
    verification_max_output_bytes: int = Field(default=32768, ge=1024, le=131072)
    # Browser intelligence (v2.6). Off by default; consequential actions are never executed.
    browser_enabled: bool = False
    browser_provider: Literal["mock", "http", "playwright"] = "http"
    browser_allow_private_network: bool = False
    browser_allowed_domains: str = Field(default="", max_length=2000)
    browser_blocked_domains: str = Field(default="", max_length=2000)
    browser_max_sessions: int = Field(default=2, ge=1, le=4)
    browser_session_ttl_seconds: int = Field(default=600, ge=60, le=3600)
    browser_max_navigations: int = Field(default=15, ge=1, le=50)
    browser_max_actions: int = Field(default=40, ge=1, le=100)
    browser_navigation_timeout_seconds: float = Field(default=15.0, ge=1.0, le=60.0)
    browser_max_page_bytes: int = Field(default=2_000_000, ge=65_536, le=5_000_000)
    browser_max_text_chars: int = Field(default=20_000, ge=1_000, le=50_000)
    browser_max_elements: int = Field(default=150, ge=10, le=300)
    browser_checks_enabled: bool = False
    # Read-only tool runtime (v2.7). Off by default; the orchestrator never calls tools in v2.7.
    tools_enabled: bool = False
    tools_max_concurrent: int = Field(default=2, ge=1, le=4)
    tools_max_timeout_seconds: float = Field(default=20.0, ge=1.0, le=30.0)
    tools_max_output_bytes: int = Field(default=32_768, ge=1_024, le=131_072)
    tools_max_argument_bytes: int = Field(default=8_192, ge=512, le=65_536)
    # Action loop (v2.8): chat may propose read-only tool calls; trusted code validates each one.
    chat_tools_enabled: bool = False
    chat_tool_max_calls: int = Field(default=4, ge=1, le=8)
    # Approval gateway (v2.9): pending requests expire; approval executes nothing.
    approvals_ttl_seconds: int = Field(default=900, ge=60, le=86_400)
    # Workflow execution (v4.4): per-step timeout, overall run deadline, and concurrently active runs.
    workflow_step_timeout_seconds: float = Field(default=30.0, ge=1.0, le=600.0)
    workflow_run_timeout_seconds: float = Field(default=300.0, ge=5.0, le=3_600.0)
    workflow_max_active_runs: int = Field(default=4, ge=1, le=16)
    # Background scheduler runner (v4.5). Off by default (execution-disabled defaults); when on, it delivers due
    # reminders, runs due jobs once each, and holds non-urgent notifications during quiet hours ("22:00-07:00",
    # local time = UTC + the offset).
    scheduler_runner_enabled: bool = False
    scheduler_tick_seconds: float = Field(default=30.0, ge=5.0, le=3_600.0)
    scheduler_quiet_hours: str | None = None
    scheduler_utc_offset_minutes: int = Field(default=0, ge=-720, le=840)
    # Voice (v3.0): offer optional browser-native voice controls. The owner must still opt in
    # per page session; the server never receives audio.
    voice_enabled: bool = True

    @field_validator("scheduler_quiet_hours")
    @classmethod
    def validate_quiet_hours(cls, value: str | None) -> str | None:
        from jarvis.scheduler.service import parse_quiet_hours

        parse_quiet_hours(value)  # raises a clear ValueError for a malformed value
        return value or None

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
        if self.browser_enabled:
            # The browser makes outbound requests on the owner's behalf, so it is
            # never available anonymously.
            token = self.local_auth_token.get_secret_value() if self.local_auth_token else ""
            if len(token) < 32 or self.owner_id is None or self.owner_id.int == 0:
                raise ValueError(
                    "JARVIS_BROWSER_ENABLED=true requires JARVIS_LOCAL_AUTH_TOKEN and JARVIS_OWNER_ID."
                )
        if self.browser_allow_private_network and self.environment == "production":
            raise ValueError("JARVIS_BROWSER_ALLOW_PRIVATE_NETWORK must not be enabled in production.")
        if self.tools_enabled:
            token = self.local_auth_token.get_secret_value() if self.local_auth_token else ""
            if len(token) < 32 or self.owner_id is None or self.owner_id.int == 0:
                raise ValueError("JARVIS_TOOLS_ENABLED=true requires JARVIS_LOCAL_AUTH_TOKEN and JARVIS_OWNER_ID.")
        if self.chat_tools_enabled and not self.tools_enabled:
            raise ValueError("JARVIS_CHAT_TOOLS_ENABLED=true requires JARVIS_TOOLS_ENABLED=true.")
        if self.browser_checks_enabled and not self.browser_enabled:
            raise ValueError("JARVIS_BROWSER_CHECKS_ENABLED=true requires JARVIS_BROWSER_ENABLED=true.")
        return self


@lru_cache
def get_settings() -> Settings:
    """Load settings once; invalid environment values fail at startup."""
    return Settings()
