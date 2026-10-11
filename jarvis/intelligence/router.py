"""Provider selection kept separate from API routes."""

from jarvis.core.config import Settings

from .base import IntelligenceProvider
from .mock_provider import MockProvider
from .openai_provider import OpenAIProvider


def build_provider(provider_name: str, *, settings: Settings | None = None) -> IntelligenceProvider:
    if provider_name == "mock":
        return MockProvider()
    if provider_name == "openai":
        if settings is None or settings.openai_api_key is None:
            raise ValueError("OpenAI provider configuration is required.")
        return OpenAIProvider(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            model=settings.openai_model,
            connect_timeout_seconds=settings.provider_connect_timeout_seconds,
            read_timeout_seconds=settings.provider_read_timeout_seconds,
        )
    raise ValueError(f"Unsupported provider: {provider_name}")
