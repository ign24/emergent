"""Tests for file_read and file_write tools."""

from __future__ import annotations

import pytest

from emergent import SafetyViolationError
from emergent.tools.files import file_read, file_write


class TestFileRead:
    async def test_read_existing_file(self, tmp_path, monkeypatch):
        # Create a test file in tmp_path and monkeypatch SANDBOX_ROOT
        import emergent.tools.files as files_module

        monkeypatch.setattr(files_module, "SANDBOX_ROOT", tmp_path)

        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world")

        result = await file_read({"path": "test.txt"})
        assert "hello world" in result

    async def test_file_not_found(self, tmp_path, monkeypatch):
        import emergent.tools.files as files_module

        monkeypatch.setattr(files_module, "SANDBOX_ROOT", tmp_path)

        result = await file_read({"path": "nonexistent.txt"})
        assert "not found" in result.lower() or "FILE_NOT_FOUND" in result

    async def test_path_traversal_blocked(self, tmp_path, monkeypatch):
        import emergent.tools.files as files_module

        monkeypatch.setattr(files_module, "SANDBOX_ROOT", tmp_path)

        with pytest.raises(SafetyViolationError, match="PATH_TRAVERSAL|OUTSIDE_SANDBOX"):
            await file_read({"path": "../../etc/passwd"})

    async def test_sensitive_env_blocked(self, tmp_path, monkeypatch):
        import emergent.tools.files as files_module

        monkeypatch.setattr(files_module, "SANDBOX_ROOT", tmp_path)

        # Create a .env file
        (tmp_path / ".env").write_text("SECRET=value")

        with pytest.raises(SafetyViolationError, match="SENSITIVE_PATH"):
            await file_read({"path": ".env"})

    async def test_ssh_key_blocked(self, tmp_path, monkeypatch):
        import emergent.tools.files as files_module

        monkeypatch.setattr(files_module, "SANDBOX_ROOT", tmp_path)

        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir()
        (ssh_dir / "id_rsa").write_text("PRIVATE KEY")

        with pytest.raises(SafetyViolationError, match="SENSITIVE_PATH"):
            await file_read({"path": ".ssh/id_rsa"})


class TestFileWrite:
    async def test_create_new_file(self, tmp_path, monkeypatch):
        import emergent.tools.files as files_module

        monkeypatch.setattr(files_module, "SANDBOX_ROOT", tmp_path)

        result = await file_write({"path": "new_file.txt", "content": "hello", "mode": "create"})
        assert "created" in result.lower()
        assert (tmp_path / "new_file.txt").read_text() == "hello"

    async def test_create_fails_if_exists(self, tmp_path, monkeypatch):
        import emergent.tools.files as files_module

        monkeypatch.setattr(files_module, "SANDBOX_ROOT", tmp_path)

        (tmp_path / "existing.txt").write_text("old")
        result = await file_write({"path": "existing.txt", "content": "new", "mode": "create"})
        assert "already exists" in result.lower()

    async def test_overwrite_mode(self, tmp_path, monkeypatch):
        import emergent.tools.files as files_module

        monkeypatch.setattr(files_module, "SANDBOX_ROOT", tmp_path)

        (tmp_path / "file.txt").write_text("old content")
        result = await file_write(
            {"path": "file.txt", "content": "new content", "mode": "overwrite"}
        )
        assert "overwritten" in result.lower()
        assert (tmp_path / "file.txt").read_text() == "new content"

    async def test_append_mode(self, tmp_path, monkeypatch):
        import emergent.tools.files as files_module

        monkeypatch.setattr(files_module, "SANDBOX_ROOT", tmp_path)

        (tmp_path / "file.txt").write_text("line1\n")
        result = await file_write({"path": "file.txt", "content": "line2\n", "mode": "append"})
        assert "appended" in result.lower()
        assert (tmp_path / "file.txt").read_text() == "line1\nline2\n"

    async def test_path_traversal_blocked(self, tmp_path, monkeypatch):
        import emergent.tools.files as files_module

        monkeypatch.setattr(files_module, "SANDBOX_ROOT", tmp_path)

        with pytest.raises(SafetyViolationError):
            await file_write({"path": "../../etc/evil.txt", "content": "evil"})
