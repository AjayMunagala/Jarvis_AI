"""Provider selection kept separate from API routes."""

from .base import IntelligenceProvider
from .mock_provider import MockProvider


def build_provider(provider_name: str) -> IntelligenceProvider:
    if provider_name == "mock":
        return MockProvider()
    raise ValueError(f"Unsupported provider: {provider_name}")
