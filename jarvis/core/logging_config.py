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
        # Only copy an explicit safe allowlist; arbitrary `extra` values may be secrets.
        for key in ("environment", "error_type"):
            value = getattr(record, key, None)
            if isinstance(value, (str, int, float, bool)):
                payload[key] = value
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
