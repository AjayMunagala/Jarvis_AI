"""Optional conversation persistence interfaces and PostgreSQL adapter."""

from jarvis.persistence.base import ConversationRepository, PersistenceError

__all__ = ["ConversationRepository", "PersistenceError"]
