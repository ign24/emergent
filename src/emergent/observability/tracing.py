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


def configure_logging(log_level: str = "INFO", log_format: str = "json") -> None:
    """Configure structlog for the application."""
    import logging
    import sys

    logging.basicConfig(
        stream=sys.stdout,
        level=getattr(logging, log_level.upper(), logging.INFO),
    )

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if log_format == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=True))

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
