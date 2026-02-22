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

    async def test_timeout_kills_subprocess(self, monkeypatch):
        import emergent.tools.shell as shell_module

        class DummyProc:
            def __init__(self) -> None:
                self.killed = False
                self.waited = False

            async def communicate(self):
                return b"", b""

            def kill(self) -> None:
                self.killed = True

            async def wait(self) -> None:
                self.waited = True

        proc = DummyProc()

        async def _fake_create_subprocess_shell(*args, **kwargs):
            return proc

        async def _fake_wait_for(awaitable, timeout):
            awaitable.close()
            raise TimeoutError

        monkeypatch.setattr(
            shell_module.asyncio,
            "create_subprocess_shell",
            _fake_create_subprocess_shell,
        )
        monkeypatch.setattr(shell_module.asyncio, "wait_for", _fake_wait_for)

        await shell_execute({"command": "sleep 5", "timeout_seconds": 1})

        assert proc.killed is True
        assert proc.waited is True

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
