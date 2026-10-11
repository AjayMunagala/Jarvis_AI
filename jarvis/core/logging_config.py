"""Small structured JSON logging setup with no secret-bearing fields."""

import json
import logging
from datetime import UTC, datetime


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Only copy explicit metadata; arbitrary `extra` values may contain secrets.
        scalar_fields = (
            "environment", "error_type", "execution_id", "intent", "confidence",
            "planned_experts", "successful_experts", "failed_experts", "partial",
            "rounds", "tool_execution_enabled", "expert_id", "error_code", "attempts",
        )
        for key in scalar_fields:
            value = getattr(record, key, None)
            if isinstance(value, (str, int, float, bool)):
                payload[key] = value
        outcomes = getattr(record, "expert_outcomes", None)
        if isinstance(outcomes, list) and all(
            isinstance(item, dict)
            and set(item) <= {"expert_id", "status", "attempts", "error_code"}
            and all(isinstance(value, (str, int, float, bool)) or value is None for value in item.values())
            for item in outcomes
        ):
            payload["expert_outcomes"] = outcomes
        return json.dumps(payload, ensure_ascii=False)


class JsonStreamHandler(logging.StreamHandler):
    """Application-owned JSON handler that leaves host/root handlers intact."""


def configure_logging(level: str) -> None:
    app_logger = logging.getLogger("jarvis")
    app_logger.setLevel(level)
    app_logger.propagate = True
    if not any(isinstance(handler, JsonStreamHandler) for handler in app_logger.handlers):
        handler = JsonStreamHandler()
        handler.setFormatter(JsonFormatter())
        app_logger.addHandler(handler)
