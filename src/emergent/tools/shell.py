"""Shell execution tool with safety classification."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any

import structlog

from emergent import SafetyViolationError

logger = structlog.get_logger(__name__)

MAX_COMMAND_LENGTH = 500
MAX_OUTPUT_CHARS = 10_000
MAX_STDERR_CHARS = 2_000


def _hash_command(cmd: str) -> str:
    return hashlib.sha256(cmd.encode()).hexdigest()[:16]


async def shell_execute(tool_input: dict[str, Any]) -> str:
    """Execute a bash command and return stdout/stderr."""
    command = str(tool_input.get("command", "")).strip()
    timeout_seconds = int(tool_input.get("timeout_seconds", 30))
    timeout_seconds = max(1, min(timeout_seconds, 120))  # clamp to [1, 120]

    if not command:
        return json.dumps({"error": "Empty command"})

    if len(command) > MAX_COMMAND_LENGTH:
        raise SafetyViolationError(f"COMMAND_TOO_LONG: command exceeds {MAX_COMMAND_LENGTH} chars")

    cmd_hash = _hash_command(command)
    log = logger.bind(command_hash=cmd_hash, command_preview=command[:50])
    log.info("shell_exec_start", timeout_seconds=timeout_seconds)

    start = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_seconds
        )
    except TimeoutError:
        log.warning("shell_exec_timeout", timeout_seconds=timeout_seconds)
        return json.dumps(
            {
                "error": f"Command timed out after {timeout_seconds}s",
                "exit_code": -1,
                "duration_ms": round((time.monotonic() - start) * 1000),
            }
        )

    duration_ms = round((time.monotonic() - start) * 1000)

    stdout = stdout_bytes.decode(errors="replace")
    stderr = stderr_bytes.decode(errors="replace")
    exit_code = proc.returncode or 0

    truncated = False
    if len(stdout) > MAX_OUTPUT_CHARS:
        stdout = stdout[:MAX_OUTPUT_CHARS] + "\n[... output truncated]"
        truncated = True

    if len(stderr) > MAX_STDERR_CHARS:
        stderr = stderr[:MAX_STDERR_CHARS] + "\n[... stderr truncated]"

    log.info(
        "shell_exec_done",
        exit_code=exit_code,
        stdout_len=len(stdout),
        stderr_len=len(stderr),
        truncated=truncated,
        duration_ms=duration_ms,
    )

    result: dict[str, Any] = {
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": exit_code,
        "duration_ms": duration_ms,
        "truncated": truncated,
    }

    # Format as readable text for Claude
    parts: list[str] = []
    if stdout:
        parts.append(stdout)
    if stderr:
        parts.append(f"[stderr]: {stderr}")
    if exit_code != 0:
        parts.append(f"[exit code: {exit_code}]")
    if truncated:
        parts.append("[output was truncated]")

    return "\n".join(parts) if parts else "(no output)"


TOOL_DEFINITION = {
    "name": "shell_execute",
    "description": (
        "Execute a bash command on the host system and return stdout/stderr. "
        "Read-only commands (ls, cat, ps, grep, df, docker ps, git status, etc.) "
        "are executed automatically. "
        "Write commands (kill, rm, mv, docker restart, pip install, etc.) require user confirmation. "
        "Destructive commands (sudo, rm -rf, curl|bash) are always blocked. "
        "Output is truncated at 10,000 chars. Timeout: 30s default, max 120s."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The bash command to execute. Max 500 chars.",
                "maxLength": 500,
            },
            "timeout_seconds": {
                "type": "integer",
                "description": "Command timeout in seconds. Default 30, max 120.",
                "default": 30,
                "maximum": 120,
            },
        },
        "required": ["command"],
    },
}
