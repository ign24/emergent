"""Tests for shell_execute tool."""

from __future__ import annotations

import pytest

from emergent import SafetyViolationError
from emergent.tools.shell import shell_execute


class TestShellExecute:
    async def test_simple_command(self):
        result = await shell_execute({"command": "echo hello"})
        assert "hello" in result

    async def test_exit_code_in_output(self):
        result = await shell_execute({"command": "exit 1"})
        assert "exit code" in result.lower() or result == "(no output)"

    async def test_command_too_long(self):
        with pytest.raises(SafetyViolationError, match="COMMAND_TOO_LONG"):
            await shell_execute({"command": "a" * 501})

    async def test_empty_command_returns_error(self):
        result = await shell_execute({"command": ""})
        assert "error" in result.lower()

    async def test_output_truncation(self):
        # Generate > 10K chars of output
        result = await shell_execute({"command": "python3 -c \"print('x' * 20000)\""})
        assert "[... output truncated]" in result
        assert len(result) <= 10_100  # small buffer over the limit

    async def test_timeout_respected(self):
        result = await shell_execute({"command": "sleep 5", "timeout_seconds": 1})
        assert "timed out" in result.lower() or "timeout" in result.lower()

    async def test_stderr_captured(self):
        result = await shell_execute({"command": "ls /nonexistent_path_xyz"})
        assert (
            "stderr" in result.lower()
            or "no such file" in result.lower()
            or "cannot access" in result.lower()
        )

    async def test_multiline_output(self):
        result = await shell_execute({"command": "printf 'line1\\nline2\\nline3'"})
        assert "line1" in result
        assert "line2" in result
