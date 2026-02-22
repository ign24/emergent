"""Structured tracing with structlog — TraceEvent, span context manager."""

from __future__ import annotations

import contextlib
import time
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

import structlog


@dataclass
class TraceEvent:
    trace_id: str
    span_id: str
    event_type: str  # "llm_call" | "tool_exec" | "memory_op" | "context_build" | "error"
    timestamp: float
    duration_ms: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    parent_span_id: str | None = None


def configure_logging(
    log_level: str = "INFO",
    log_format: str = "json",
    log_file: str | None = None,
    max_bytes: int = 10 * 1024 * 1024,  # 10 MB
    backup_count: int = 5,
) -> None:
    """Configure structlog for the application.

    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR).
        log_format: 'json' for structured JSON (production) or 'console' for human-readable.
        log_file: Optional path to write logs. Enables rotation at max_bytes
                  with backup_count files. If None, logs go to stdout only.
        max_bytes: Max size per log file before rotation (default 10MB).
        backup_count: Number of rotated files to keep (default 5).
    """
    import logging
    import logging.handlers
    import sys

    level = getattr(logging, log_level.upper(), logging.INFO)
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.handlers.clear()

    if log_file:
        from pathlib import Path
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        # File handler: all logs at configured level, JSON format
        file_handler: logging.Handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        root_logger.addHandler(file_handler)

        # Stdout handler: WARNING+ only so terminal stays clean
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setLevel(logging.WARNING)
        root_logger.addHandler(stderr_handler)
    else:
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(level)
        root_logger.addHandler(handler)

    # Always use JSON renderer — file logs need it, and stdout WARNING lines
    # benefit from structured output too.
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


@contextlib.asynccontextmanager
async def trace_span(
    trace_id: str,
    event_type: str,
    parent_span_id: str | None = None,
    **metadata: Any,
) -> AsyncGenerator[dict[str, Any], None]:
    """Async context manager that emits a TraceEvent on exit."""
    span_id = str(uuid.uuid4())[:8]
    start = time.monotonic()
    span_data: dict[str, Any] = {"error": None}

    log = structlog.get_logger(__name__).bind(
        trace_id=trace_id,
        span_id=span_id,
        event_type=event_type,
        **metadata,
    )
    log.info(f"{event_type}_start")

    try:
        yield span_data
    except Exception as e:
        span_data["error"] = str(e)
        log.error(f"{event_type}_error", error=str(e))
        raise
    finally:
        duration_ms = (time.monotonic() - start) * 1000
        log.info(
            f"{event_type}_done",
            duration_ms=round(duration_ms, 1),
            error=span_data.get("error"),
            **{k: v for k, v in span_data.items() if k != "error"},
        )
