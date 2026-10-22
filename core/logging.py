from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import datetime, timezone

from config import Config

# Context variable to correlate logs across async tasks
request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


class JSONFormatter(logging.Formatter):
    """Formats log records as structured single-line JSON."""

    def format(self, record: logging.LogRecord) -> str:
        log_payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id_ctx.get(),
        }
        if record.exc_info:
            log_payload["exception"] = self.formatException(record.exc_info)
        if hasattr(record, "extra_fields") and isinstance(record.extra_fields, dict):
            log_payload.update(record.extra_fields)
        return json.dumps(log_payload, default=str)


class TextFormatter(logging.Formatter):
    """Formats log records for human readability during development."""

    def format(self, record: logging.LogRecord) -> str:
        req_id = request_id_ctx.get()
        req_str = f" [{req_id}]" if req_id != "-" else ""
        record.msg = f"{record.msg}{req_str}"
        return super().format(record)


def setup_logging() -> None:
    """Configures root logger with proper log level and format."""
    log_level = getattr(logging, Config.LOG_LEVEL.upper(), logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # Remove existing handlers to prevent duplicate lines
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)

    if Config.LOG_FORMAT.lower() == "json" and Config.APP_ENV != "development":
        formatter = JSONFormatter()
    else:
        formatter = TextFormatter(
            fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # Suppress verbose noisy third-party loggers
    logging.getLogger("rasterio").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)  # Handled by our custom access logger

