"""Emergent — Autonomous Agent Runtime."""

__version__ = "0.1.0"


class EmergentError(Exception):
    """Base exception for all Emergent errors."""


class ToolExecutionError(EmergentError):
    """Raised when a tool fails to execute."""


class SafetyViolationError(EmergentError):
    """Raised when a safety check blocks execution."""


class ContextOverflowError(EmergentError):
    """Raised when context window is exhausted."""


class MaxIterationsError(EmergentError):
    """Raised when the agent loop hits max_iterations."""


class ConfigurationError(EmergentError):
    """Raised on invalid configuration."""


class ConfirmationTimeoutError(EmergentError):
    """Raised when TIER_2 confirmation times out."""


class ConfirmationDeniedError(EmergentError):
    """Raised when user denies a TIER_2 confirmation."""
