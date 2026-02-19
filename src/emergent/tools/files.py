"""File read/write tools, sandboxed to $HOME."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

from emergent import SafetyViolationError

logger = structlog.get_logger(__name__)

SANDBOX_ROOT = Path.home()
MAX_READ_CHARS = 10_000
MAX_WRITE_BYTES = 1_024 * 1_024  # 1MB

# Sensitive paths that should never be read
_SENSITIVE_PATTERNS = [
    ".env",
    "secrets",
    "/etc/shadow",
    "/etc/passwd",
    "/etc/sudoers",
    ".ssh/id_rsa",
    ".ssh/id_ed25519",
    ".ssh/id_ecdsa",
    ".ssh/id_dsa",
    ".ssh/authorized_keys",
    "credentials",
    "config/database",
]

_SENSITIVE_EXTENSIONS = [".pem", ".key", ".p12", ".pfx"]


def _resolve_path(path_str: str) -> Path:
    """Resolve path relative to $HOME, blocking traversal and sensitive files."""
    if path_str.startswith("/"):
        resolved = Path(path_str).resolve()
    else:
        resolved = (SANDBOX_ROOT / path_str).resolve()

    # Block path traversal
    try:
        resolved.relative_to(SANDBOX_ROOT)
    except ValueError:
        raise SafetyViolationError(f"OUTSIDE_SANDBOX: path '{path_str}' resolves outside $HOME")

    # Block '..' traversal attempts
    if ".." in Path(path_str).parts:
        raise SafetyViolationError(f"PATH_TRAVERSAL: '..' not allowed in path '{path_str}'")

    # Check sensitive paths
    path_lower = str(resolved).lower()
    rel_str = str(resolved.relative_to(SANDBOX_ROOT)).lower()

    for sensitive in _SENSITIVE_PATTERNS:
        if sensitive.lower() in path_lower:
            logger.warning("sensitive_path_blocked", path=str(resolved), pattern=sensitive)
            raise SafetyViolationError(f"SENSITIVE_PATH: '{resolved.name}' is a sensitive file")

    if resolved.suffix.lower() in _SENSITIVE_EXTENSIONS:
        raise SafetyViolationError(f"SENSITIVE_PATH: extension '{resolved.suffix}' is blocked")

    return resolved


async def file_read(tool_input: dict[str, Any]) -> str:
    """Read a file from $HOME sandbox."""
    path_str = str(tool_input.get("path", ""))
    max_chars = int(tool_input.get("max_chars", MAX_READ_CHARS))
    max_chars = min(max_chars, MAX_READ_CHARS)

    if not path_str:
        return "Error: path is required"

    resolved = _resolve_path(path_str)

    if not resolved.exists():
        return f"Error: FILE_NOT_FOUND: '{resolved}' does not exist"

    if not resolved.is_file():
        return f"Error: '{resolved}' is not a file"

    try:
        size_bytes = resolved.stat().st_size
        content = resolved.read_text(errors="replace")
    except PermissionError:
        raise SafetyViolationError(f"PERMISSION_DENIED: cannot read '{resolved}'")

    truncated = False
    if len(content) > max_chars:
        content = content[:max_chars] + "\n[... file truncated]"
        truncated = True

    logger.info("file_read", path=str(resolved), size_bytes=size_bytes, truncated=truncated)
    return content


async def file_write(tool_input: dict[str, Any]) -> str:
    """Write/create a file in $HOME sandbox."""
    path_str = str(tool_input.get("path", ""))
    content = str(tool_input.get("content", ""))
    mode = str(tool_input.get("mode", "create"))

    if mode not in ("create", "overwrite", "append"):
        return "Error: mode must be 'create', 'overwrite', or 'append'"

    if not path_str:
        return "Error: path is required"

    if len(content.encode()) > MAX_WRITE_BYTES:
        return f"Error: content exceeds max size of {MAX_WRITE_BYTES // 1024}KB"

    resolved = _resolve_path(path_str)

    # Create parent directories if needed
    resolved.parent.mkdir(parents=True, exist_ok=True)

    try:
        if mode == "create":
            if resolved.exists():
                return "Error: file already exists. Use mode='overwrite' to replace it."
            resolved.write_text(content)
            action = "created"
        elif mode == "overwrite":
            resolved.write_text(content)
            action = "overwritten"
        elif mode == "append":
            with open(resolved, "a") as f:
                f.write(content)
            action = "appended"
        else:
            action = "unknown"
    except PermissionError:
        raise SafetyViolationError(f"PERMISSION_DENIED: cannot write '{resolved}'")

    logger.info("file_write", path=str(resolved), mode=mode, bytes_written=len(content.encode()))
    return f"File {action}: {resolved} ({len(content.encode())} bytes)"


FILE_READ_DEFINITION = {
    "name": "file_read",
    "description": (
        "Read the content of a file. Path is relative to $HOME. "
        "Sensitive files (.env, .ssh keys, secrets) are blocked. "
        "Output is truncated at 10,000 chars."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path relative to $HOME (e.g., 'Documents/notes.txt')",
            },
            "max_chars": {
                "type": "integer",
                "description": "Max characters to return. Default 10000.",
                "default": 10000,
            },
        },
        "required": ["path"],
    },
}

FILE_WRITE_DEFINITION = {
    "name": "file_write",
    "description": (
        "Create or write a file in $HOME. Requires user confirmation if file already exists. "
        "Mode: 'create' (fails if exists), 'overwrite' (replaces), 'append' (adds to end). "
        "Max content size: 1MB."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path relative to $HOME",
            },
            "content": {
                "type": "string",
                "description": "Content to write",
            },
            "mode": {
                "type": "string",
                "enum": ["create", "overwrite", "append"],
                "description": "Write mode. Default: 'create'",
                "default": "create",
            },
        },
        "required": ["path", "content"],
    },
}
